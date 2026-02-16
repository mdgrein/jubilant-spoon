-- Simple 3-stage pipeline using REAL HARNESS
-- Uses the proven harness.py that works with WSL + Ollama

-- Delete existing
DELETE FROM template_job_dependencies WHERE template_job_id IN (
    SELECT template_job_id FROM template_jobs WHERE template_stage_id IN (
        SELECT template_stage_id FROM template_stages WHERE template_id='simple-plan-code-verify'
    )
);
DELETE FROM template_jobs WHERE template_stage_id IN (
    SELECT template_stage_id FROM template_stages WHERE template_id='simple-plan-code-verify'
);
DELETE FROM template_stages WHERE template_id='simple-plan-code-verify';
DELETE FROM pipeline_templates WHERE template_id='simple-plan-code-verify';

-- Create template
INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at)
VALUES (
    'simple-plan-code-verify',
    'Simple: Plan → Code → Verify (real harness)',
    'Uses working harness.py with WSL Ollama',
    datetime('now'),
    datetime('now')
);

-- Stage 1: Planning
INSERT INTO template_stages (template_stage_id, template_id, name, stage_order)
VALUES ('stage-plan', 'simple-plan-code-verify', 'Plan', 1);

INSERT INTO template_jobs (
    template_job_id, template_stage_id, agent_type, name,
    prompt_template, max_iterations, timeout_seconds, artifact_strategy, retry_strategy
) VALUES (
    'job-planner',
    'stage-plan',
    'planner',
    'Planner',
    'You are a planning agent. Break down this task into simple, independent coding tasks.

Original request: {{original_prompt}}

Output a JSON array of task strings in the finish tool.

Example:
{"actions": [{"tool": "finish", "args": {"tasks": ["task1", "task2"]}}]}

Output ONLY that JSON, nothing else.',
    200,
    600,
    '{"type": "stdout_final"}',
    '{"include_context": true, "context_instruction": "RETRY: You hit max iterations. Below is your previous attempt output. Continue from where you left off.\n\n"}'
);

-- Stage 2: Coding (MULTIPLIER - spawns multiple jobs from planner output)
INSERT INTO template_stages (template_stage_id, template_id, name, stage_order)
VALUES ('stage-code', 'simple-plan-code-verify', 'Code', 2);

INSERT INTO template_jobs (
    template_job_id, template_stage_id, agent_type, name,
    prompt_template, max_iterations, timeout_seconds,
    artifact_strategy, job_multiplier, retry_strategy
) VALUES (
    'job-script-kiddie',
    'stage-code',
    'dev',
    'Script-Kiddie',
    'You are a simple code generator. Output working Python code.

Task: {{item}}

Output format:
{"actions": [{"tool": "finish", "args": {"reason": "completed"}}]}

Include the code in your response before the JSON.',
    200,
    600,
    '{"type": "stdout_final"}',
    '{"source_template_job_id": "job-planner", "source_type": "action", "parse_strategy": "json_array", "prompt_template": "You are a simple code generator. Output working Python code.\\n\\nTask: {{item}}\\n\\nOutput format:\\n{\"actions\": [{\"tool\": \"finish\", \"args\": {\"reason\": \"completed\"}}]}\\n\\nInclude the code in your response before the JSON."}',
    '{"include_context": true, "context_instruction": "RETRY: You hit max iterations. Below is your previous attempt output. Continue from where you left off.\n\n"}'
);

-- Stage 3: Verification
INSERT INTO template_stages (template_stage_id, template_id, name, stage_order)
VALUES ('stage-verify', 'simple-plan-code-verify', 'Verify', 3);

INSERT INTO template_jobs (
    template_job_id, template_stage_id, agent_type, name,
    prompt_template, max_iterations, timeout_seconds, artifact_strategy, retry_strategy
) VALUES (
    'job-verifier',
    'stage-verify',
    'verifier',
    'Verifier',
    'You are a code verifier. Check if the code meets requirements.

Original request: {{original_prompt}}

Review the code from previous jobs and output:

PASS - if all code is correct
FAIL - <specific issues> - if there are problems

Output format:
{"actions": [{"tool": "finish", "args": {"reason": "verification_complete"}}]}',
    200,
    600,
    '{"type": "stdout_final"}',
    '{"include_context": true, "context_instruction": "RETRY: You hit max iterations. Below is your previous attempt output. Continue from where you left off.\n\n"}'
);

-- Dependencies
INSERT INTO template_job_dependencies (template_job_id, depends_on_template_job_id, dependency_type)
VALUES ('job-verifier', 'job-script-kiddie', 'success');
