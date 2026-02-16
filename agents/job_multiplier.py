"""
Job Multiplier - Dynamic job spawning based on artifact content.

When a job completes, Clowder can parse its artifact and spawn multiple
child jobs based on the parsed content.

Example: Planner outputs ["task1", "task2", "task3"]
         → Spawns 3 script-kiddie jobs, one per task
"""

import json
import uuid
from typing import Optional


def parse_artifact_content(content: str, parse_strategy: str) -> list[str]:
    """
    Parse artifact content into list of items.

    Args:
        content: Artifact content
        parse_strategy: How to parse:
            - "json_array": Parse as JSON array
            - "line_delimited": Split by newlines
            - "comma_separated": Split by commas

    Returns:
        List of parsed items
    """
    if not content:
        return []

    if parse_strategy == "json_array":
        try:
            items = json.loads(content.strip())
            if isinstance(items, list):
                return [str(item) for item in items]
            else:
                return [str(items)]  # Single item, wrap in list
        except json.JSONDecodeError:
            print(f"Warning: Failed to parse as JSON, treating as single item")
            return [content]

    elif parse_strategy == "line_delimited":
        return [line.strip() for line in content.split('\n') if line.strip()]

    elif parse_strategy == "comma_separated":
        return [item.strip() for item in content.split(',') if item.strip()]

    else:
        # Unknown strategy, treat as single item
        return [content]


def spawn_multiplied_jobs(
    db_conn,
    source_job_id: str,
    template_job: dict,
    multiplier_config: dict,
    pipeline_id: str,
    stage_id: str,
    original_prompt: str,
    workspace_path: str,
    timestamp_fn
) -> list[str]:
    """
    Spawn multiple jobs based on source job's artifact.

    Args:
        db_conn: Database connection
        source_job_id: Job whose artifact we're parsing
        template_job: Template job definition (from template_jobs table)
        multiplier_config: Multiplier configuration
        pipeline_id: Pipeline ID
        stage_id: Stage ID
        original_prompt: Original pipeline prompt
        workspace_path: Workspace path
        timestamp_fn: Function to get timestamp

    Returns:
        List of spawned job IDs
    """
    # Get tasks from source job - either from action args or artifact
    source_type = multiplier_config.get("source_type", "artifact")  # "artifact" or "action"

    if source_type == "action":
        # Extract from finish action's args
        action_log = db_conn.execute("""
            SELECT llm_response FROM action_history
            WHERE job_id = ?
            ORDER BY iteration DESC
            LIMIT 1
        """, (source_job_id,)).fetchone()

        if not action_log:
            print(f"Warning: No action history for job {source_job_id[:8]}")
            return []

        llm_response = json.loads(action_log['llm_response'])
        # Find finish action and extract tasks from args
        tasks_content = None
        for action in llm_response.get('actions', []):
            if action.get('tool') == 'finish':
                tasks_content = json.dumps(action.get('args', {}).get('tasks', []))
                break

        if not tasks_content:
            print(f"Warning: No tasks found in finish action for job {source_job_id[:8]}")
            return []
    else:
        # Original artifact-based approach
        artifact_name = multiplier_config.get("artifact_name", "final_output.txt")
        artifact = db_conn.execute("""
            SELECT content FROM artifacts
            WHERE job_id = ? AND name = ?
            ORDER BY created_at DESC
            LIMIT 1
        """, (source_job_id, artifact_name)).fetchone()

        if not artifact or not artifact['content']:
            print(f"Warning: No artifact found for job {source_job_id[:8]}, name={artifact_name}")
            return []

        tasks_content = artifact['content']

    # Parse tasks
    parse_strategy = multiplier_config.get("parse_strategy", "json_array")
    items = parse_artifact_content(tasks_content, parse_strategy)

    if not items:
        print(f"Warning: Artifact parsing returned no items")
        return []

    # Get prompt template
    prompt_template = multiplier_config.get("prompt_template", "{{item}}")

    spawned_job_ids = []

    # Create one job per item
    for idx, item in enumerate(items):
        job_id = str(uuid.uuid4())

        # Replace placeholders in prompt
        prompt = prompt_template.replace("{{item}}", item)
        prompt = prompt.replace("{{original_prompt}}", original_prompt)
        prompt = prompt.replace("{{index}}", str(idx))

        # Handle command template if present
        command = None
        if template_job.get('command_template'):
            command = template_job['command_template'].replace('{{job_id}}', job_id)
            command = command.replace('{{prompt}}', prompt)
            command = command.replace('{{agent_type}}', template_job['agent_type'])

        # Get artifact strategy and retry strategy
        artifact_strategy = template_job.get('artifact_strategy')
        artifact_strategy_json = artifact_strategy if artifact_strategy else None
        retry_strategy = template_job.get('retry_strategy')
        retry_strategy_json = retry_strategy if retry_strategy else None

        # Create job
        db_conn.execute("""
            INSERT INTO jobs (
                job_id, pipeline_id, stage_id, agent_type, prompt, original_prompt, command,
                max_iterations, timeout_seconds, allowed_paths,
                artifact_strategy, retry_strategy, template_job_id, parent_job_id,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (
            job_id,
            pipeline_id,
            stage_id,
            template_job['agent_type'],
            prompt,
            prompt,  # original_prompt starts as same as prompt
            command,
            template_job['max_iterations'],
            template_job['timeout_seconds'],
            f'["{workspace_path}"]',
            artifact_strategy_json,
            retry_strategy_json,
            template_job['template_job_id'],  # Track template source
            source_job_id,  # Track parent job
            timestamp_fn(),
            timestamp_fn(),
        ))

        # Add dependency on source job
        db_conn.execute("""
            INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type)
            VALUES (?, ?, 'success')
        """, (job_id, source_job_id))

        spawned_job_ids.append(job_id)

    db_conn.commit()

    print(f"Spawned {len(spawned_job_ids)} jobs from {source_job_id[:8]} (multiplier)")

    return spawned_job_ids


def check_and_spawn_multiplied_jobs(db_conn, completed_job_id: str, timestamp_fn):
    """
    Check if any jobs are waiting to be spawned based on this job's completion.

    This should be called after a job completes successfully.

    Args:
        db_conn: Database connection
        completed_job_id: Job that just completed
        timestamp_fn: Function to get timestamp

    Returns:
        Number of jobs spawned
    """
    # Get pipeline and stage info for completed job
    completed_job = db_conn.execute("""
        SELECT pipeline_id, stage_id FROM jobs WHERE job_id = ?
    """, (completed_job_id,)).fetchone()

    if not completed_job:
        return 0

    pipeline_id = completed_job['pipeline_id']

    # Get pipeline info
    pipeline = db_conn.execute("""
        SELECT original_prompt FROM pipelines WHERE pipeline_id = ?
    """, (pipeline_id,)).fetchone()

    original_prompt = pipeline['original_prompt'] if pipeline else ""

    # Find template jobs that have multiplier config pointing to this job
    # We need to check if the completed job was instantiated from a template job,
    # and if there are other template jobs that reference it

    # For now, we'll use a simpler approach: check all template jobs in the same template
    # and see if any have a multiplier that references the completed job's template source

    # Get template_id for this pipeline
    template_id = db_conn.execute("""
        SELECT template_id FROM pipelines WHERE pipeline_id = ?
    """, (pipeline_id,)).fetchone()

    if not template_id or not template_id['template_id']:
        return 0

    # Get the template_job_id of the completed job
    completed_job_template = db_conn.execute("""
        SELECT template_job_id FROM jobs WHERE job_id = ?
    """, (completed_job_id,)).fetchone()

    if not completed_job_template or not completed_job_template['template_job_id']:
        return 0  # Job wasn't from a template

    completed_template_job_id = completed_job_template['template_job_id']

    # Find template jobs with multiplier config that references this template job
    template_jobs = db_conn.execute("""
        SELECT tj.*, ts.stage_order
        FROM template_jobs tj
        JOIN template_stages ts ON tj.template_stage_id = ts.template_stage_id
        WHERE ts.template_id = ? AND tj.job_multiplier IS NOT NULL
    """, (template_id['template_id'],)).fetchall()

    total_spawned = 0

    for template_job in template_jobs:
        multiplier_config = json.loads(template_job['job_multiplier'])
        source_template_job_id = multiplier_config.get('source_template_job_id')

        if not source_template_job_id:
            continue

        # Check if this multiplier references the completed job's template
        if source_template_job_id != completed_template_job_id:
            continue

        # Check if we've already spawned jobs for this multiplier + source job combo
        already_spawned = db_conn.execute("""
            SELECT COUNT(*) as count FROM jobs
            WHERE parent_job_id = ? AND template_job_id = ?
        """, (completed_job_id, template_job['template_job_id'])).fetchone()

        if already_spawned['count'] > 0:
            # Already spawned
            continue

        # Spawn jobs
        stage_id = db_conn.execute("""
            SELECT stage_id FROM stages
            WHERE pipeline_id = ? AND stage_order = ?
        """, (pipeline_id, template_job['stage_order'])).fetchone()

        if not stage_id:
            continue

        spawned = spawn_multiplied_jobs(
            db_conn=db_conn,
            source_job_id=completed_job_id,
            template_job=dict(template_job),
            multiplier_config=multiplier_config,
            pipeline_id=pipeline_id,
            stage_id=stage_id['stage_id'],
            original_prompt=original_prompt,
            workspace_path="./",  # TODO: Get from pipeline config
            timestamp_fn=timestamp_fn
        )

        total_spawned += len(spawned)

    return total_spawned
