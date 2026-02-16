-- Simple 3-stage pipeline: Plan → Code → Verify
-- Demonstrates artifact handoff with minimal complexity

-- Delete existing template if it exists
DELETE FROM pipeline_templates WHERE template_id = 'simple-plan-code-verify';

-- Create template
INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at)
VALUES (
    'simple-plan-code-verify',
    'Simple: Plan → Code → Verify',
    'Three-stage pipeline: Planner breaks down work, Script-Kiddie writes code, Verifier checks output',
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

-- Stage 2: Coding (will run multiple times, one per task from planner)
INSERT INTO template_stages (template_stage_id, template_id, name, stage_order)
VALUES ('stage-code', 'simple-plan-code-verify', 'Code', 2);

INSERT INTO template_jobs (
    template_job_id, template_stage_id, agent_type, name,
    prompt_template, command_template, max_iterations, timeout_seconds, artifact_strategy
) VALUES (
    'job-script-kiddie',
    'stage-code',
    'dev',
    'Script-Kiddie',
    'You are a simple code generator. Write ONLY code to stdout, no explanations.

Task: {{original_prompt}}

Output the complete, working code. Nothing else - just the code.',
    'python agents/simple_agent.py --agent-type script-kiddie --job-id {{job_id}}',
    10,
    300,
    '{"type": "stdout_final"}'
);

-- Dependency: script-kiddie depends on planner completing
INSERT INTO template_job_dependencies (template_job_id, depends_on_template_job_id, dependency_type)
VALUES ('job-script-kiddie', 'job-planner', 'success');

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

-- Dependency: verifier depends on script-kiddie completing
INSERT INTO template_job_dependencies (template_job_id, depends_on_template_job_id, dependency_type)
VALUES ('job-verifier', 'job-script-kiddie', 'success');
