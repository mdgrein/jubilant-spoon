"""
Pipeline template management.
Create pipelines from templates with customization.
"""

import uuid
from typing import Optional
from datetime import datetime, timezone
from db import ClowderDB


class TemplateManager:
    """Manage pipeline templates and instantiation."""

    def __init__(self, db: ClowderDB):
        self.db = db

    def list_templates(self) -> list[dict]:
        """List all available templates."""
        rows = self.db.conn.execute("""
            SELECT
                t.template_id,
                t.name,
                t.description,
                COUNT(DISTINCT ts.template_stage_id) as stage_count,
                COUNT(DISTINCT tj.template_job_id) as job_count
            FROM pipeline_templates t
            LEFT JOIN template_stages ts ON t.template_id = ts.template_id
            LEFT JOIN template_jobs tj ON ts.template_stage_id = tj.template_stage_id
            GROUP BY t.template_id
            ORDER BY t.name
        """).fetchall()

        return [dict(row) for row in rows]

    def get_template(self, template_id: str) -> Optional[dict]:
        """Get template details with stages and jobs."""
        # Get template
        template_row = self.db.conn.execute("""
            SELECT * FROM pipeline_templates WHERE template_id = ?
        """, (template_id,)).fetchone()

        if not template_row:
            return None

        template = dict(template_row)

        # Get stages
        stage_rows = self.db.conn.execute("""
            SELECT * FROM template_stages
            WHERE template_id = ?
            ORDER BY stage_order
        """, (template_id,)).fetchall()

        stages = []
        for stage_row in stage_rows:
            stage = dict(stage_row)

            # Get jobs for this stage
            job_rows = self.db.conn.execute("""
                SELECT * FROM template_jobs
                WHERE template_stage_id = ?
            """, (stage['template_stage_id'],)).fetchall()

            stage['jobs'] = [dict(job_row) for job_row in job_rows]
            stages.append(stage)

        template['stages'] = stages

        # Get dependencies
        dep_rows = self.db.conn.execute("""
            SELECT * FROM template_job_dependencies
            WHERE template_job_id IN (
                SELECT template_job_id FROM template_jobs
                WHERE template_stage_id IN (
                    SELECT template_stage_id FROM template_stages
                    WHERE template_id = ?
                )
            )
        """, (template_id,)).fetchall()

        template['dependencies'] = [dict(dep_row) for dep_row in dep_rows]

        return template

    def instantiate_template(
        self,
        template_id: str,
        original_prompt: str,
        workspace_path: str,
        excluded_stage_ids: Optional[list[str]] = None,
        excluded_job_ids: Optional[list[str]] = None,
    ) -> str:
        """
        Create a pipeline from a template.

        Args:
            template_id: Template to use
            original_prompt: User's prompt (replaces {{original_prompt}})
            workspace_path: Allowed workspace path
            excluded_stage_ids: Template stage IDs to exclude
            excluded_job_ids: Template job IDs to exclude

        Returns:
            Pipeline ID
        """
        excluded_stage_ids = excluded_stage_ids or []
        excluded_job_ids = excluded_job_ids or []

        # Get template
        template = self.get_template(template_id)
        if not template:
            raise ValueError(f"Template {template_id} not found")

        # Create pipeline
        pipeline_id = str(uuid.uuid4())
        self.db.conn.execute("""
            INSERT INTO pipelines (
                pipeline_id, template_id, original_prompt, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', ?, ?)
        """, (
            pipeline_id,
            template_id,
            original_prompt,
            self._timestamp(),
            self._timestamp(),
        ))

        # Map template IDs to real IDs
        stage_map = {}  # template_stage_id -> stage_id
        job_map = {}  # template_job_id -> job_id

        # Create stages (excluding user-removed ones)
        for stage in template['stages']:
            if stage['template_stage_id'] in excluded_stage_ids:
                continue

            stage_id = str(uuid.uuid4())
            stage_map[stage['template_stage_id']] = stage_id

            self.db.conn.execute("""
                INSERT INTO stages (
                    stage_id, pipeline_id, name, stage_order, status, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
            """, (
                stage_id,
                pipeline_id,
                stage['name'],
                stage['stage_order'],
                self._timestamp(),
            ))

            # Create jobs for this stage (excluding user-removed ones)
            for job in stage['jobs']:
                if job['template_job_id'] in excluded_job_ids:
                    continue

                job_id = str(uuid.uuid4())
                job_map[job['template_job_id']] = job_id

                # Replace {{original_prompt}} in prompt template
                prompt = job['prompt_template'].replace('{{original_prompt}}', original_prompt) if job['prompt_template'] else ''

                # Handle custom command if specified
                command = None
                if job.get('command_template'):
                    # Replace placeholders in command template
                    command = job['command_template'].replace('{{job_id}}', job_id)
                    command = command.replace('{{prompt}}', prompt)
                    command = command.replace('{{agent_type}}', job['agent_type'])

                # Get artifact strategy if defined
                artifact_strategy = job.get('artifact_strategy')
                artifact_strategy_json = artifact_strategy if artifact_strategy else None

                # Get retry strategy if defined
                retry_strategy = job.get('retry_strategy')
                retry_strategy_json = retry_strategy if retry_strategy else None

                self.db.conn.execute("""
                    INSERT INTO jobs (
                        job_id, pipeline_id, stage_id, agent_type, prompt, original_prompt, command,
                        max_iterations, timeout_seconds, allowed_paths,
                        artifact_strategy, retry_strategy, template_job_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """, (
                    job_id,
                    pipeline_id,
                    stage_id,
                    job['agent_type'],
                    prompt,
                    prompt,  # original_prompt starts as same as prompt
                    command,
                    job['max_iterations'],
                    job['timeout_seconds'],
                    f'["{workspace_path}"]',
                    artifact_strategy_json,
                    retry_strategy_json,
                    job['template_job_id'],  # Track which template this came from
                    self._timestamp(),
                    self._timestamp(),
                ))

        # Create dependencies (only for jobs that weren't excluded)
        for dep in template['dependencies']:
            if dep['template_job_id'] in excluded_job_ids:
                continue
            if dep['depends_on_template_job_id'] in excluded_job_ids:
                continue

            # Map template IDs to real IDs
            job_id = job_map.get(dep['template_job_id'])
            depends_on_job_id = job_map.get(dep['depends_on_template_job_id'])

            if job_id and depends_on_job_id:
                self.db.conn.execute("""
                    INSERT INTO job_dependencies (
                        job_id, depends_on_job_id, dependency_type
                    ) VALUES (?, ?, ?)
                """, (
                    job_id,
                    depends_on_job_id,
                    dep['dependency_type'],
                ))

        self.db.conn.commit()

        return pipeline_id

    def _timestamp(self) -> str:
        """Get ISO8601 timestamp."""
        return datetime.now(timezone.utc).isoformat()
