-- Simple 3-stage pipeline WITH JOB MULTIPLIER
-- Planner outputs tasks → Multiple script-kiddies (one per task) → Verifier

-- Delete existing
DELETE FROM pipeline_templates WHERE template_id = 'simple-plan-code-verify';

-- Create template
INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at)
VALUES (
    'simple-plan-code-verify',
    'Simple: Plan → Code → Verify (with multiplier)',
    'Planner breaks down work into tasks, spawns multiple script-kiddies (one per task), verifier checks all outputs',
    datetime('now'),
    datetime('now')
);

-- Stage 1: Planning
INSERT INTO template_stages (template_stage_id, template_id, name, stage_order)
VALUES ('stage-plan', 'simple-plan-code-verify', 'Plan', 1);

INSERT INTO template_jobs (
    template_job_id, template_stage_id, agent_type, name,
    prompt_template, command_template, max_iterations, timeout_seconds, artifact_strategy
) VALUES (
    'job-planner',
    'stage-plan',
    'planner',
    'Planner',
    'You are a planning agent. Break down this task into simple, independent coding tasks.

Original request: {{original_prompt}}

Output a JSON list of tasks, where each task is a string describing one coding job.
Example:
["Create a function to calculate fibonacci numbers", "Write a function to reverse a string"]

Output ONLY the JSON array, nothing else.',
    'python agents/simple_agent.py --agent-type planner --job-id {{job_id}}',
    10,
    300,
    '{"type": "stdout_final"}'
);

-- Stage 2: Coding (MULTIPLIER TEMPLATE - spawns dynamically)
INSERT INTO template_stages (template_stage_id, template_id, name, stage_order)
VALUES ('stage-code', 'simple-plan-code-verify', 'Code', 2);

INSERT INTO template_jobs (
    template_job_id, template_stage_id, agent_type, name,
    prompt_template, command_template, max_iterations, timeout_seconds,
    artifact_strategy, job_multiplier
) VALUES (
    'job-script-kiddie',
    'stage-code',
    'dev',
    'Script-Kiddie',
    'You are a simple code generator. Write ONLY code to stdout, no explanations.

Task: {{item}}

Output the complete, working code. Nothing else - just the code.',
    'python agents/simple_agent.py --agent-type script-kiddie --job-id {{job_id}}',
    10,
    300,
    '{"type": "stdout_final"}',
    '{"source_template_job_id": "job-planner", "parse_strategy": "json_array", "artifact_name": "final_output.txt", "prompt_template": "You are a simple code generator. Write ONLY code to stdout, no explanations.\\n\\nTask: {{item}}\\n\\nOutput the complete, working code. Nothing else - just the code."}'
);

-- Stage 3: Verification
INSERT INTO template_stages (template_stage_id, template_id, name, stage_order)
VALUES ('stage-verify', 'simple-plan-code-verify', 'Verify', 3);

INSERT INTO template_jobs (
    template_job_id, template_stage_id, agent_type, name,
    prompt_template, command_template, max_iterations, timeout_seconds, artifact_strategy
) VALUES (
    'job-verifier',
    'stage-verify',
    'verifier',
    'Verifier',
    'You are a code verifier. Check if the code meets the requirements.

Original request: {{original_prompt}}

Code to verify: (see artifacts from previous jobs)

Output:
PASS - if code is correct
FAIL - <reason> - if code has issues

Be specific about what is wrong.',
    'python agents/simple_agent.py --agent-type verifier --job-id {{job_id}}',
    10,
    300,
    '{"type": "stdout_final"}'
);

-- Dependencies
-- Script-kiddies will depend on planner (set automatically via multiplier)
-- Verifier depends on script-kiddie template (waits for all spawned jobs)
INSERT INTO template_job_dependencies (template_job_id, depends_on_template_job_id, dependency_type)
VALUES ('job-verifier', 'job-script-kiddie', 'success');
