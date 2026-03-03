-- Seed Pipeline Templates
-- Uses INSERT OR IGNORE so this file can be run safely on any database.
-- Startup always runs this file; new templates appear automatically on next restart.
--
-- FK enforcement is disabled for the duration of this script so OR IGNORE
-- rows (PK conflicts on re-runs) don't trigger cascading FK checks.
PRAGMA foreign_keys = OFF;

-- =====================================================================
-- TEMPLATE: Full Workflow (Plan → Dev → Test → Verify)
-- =====================================================================
INSERT OR IGNORE INTO pipeline_templates (template_id, name, description, created_at, updated_at) VALUES (
    'template-full',
    'Full Workflow',
    'Complete AI development workflow with planning, implementation, testing, and verification',
    '2026-02-11T00:00:00Z',
    '2026-02-11T00:00:00Z'
);

INSERT OR IGNORE INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES
    ('ts-full-1', 'template-full', 'plan',   1),
    ('ts-full-2', 'template-full', 'dev',    2),
    ('ts-full-3', 'template-full', 'test',   3),
    ('ts-full-4', 'template-full', 'verify', 4);

INSERT OR IGNORE INTO template_jobs
    (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds)
VALUES
    ('tj-full-plan',   'ts-full-1', 'planner',  'Break down task',       '{{original_prompt}}',                                  50, 300),
    ('tj-full-dev',    'ts-full-2', 'dev',       'Implement changes',     'Implement the following: {{original_prompt}}',          50, 300),
    ('tj-full-test',   'ts-full-3', 'tester',    'Plan and verify tests', 'Design comprehensive tests for: {{original_prompt}}',   50, 300),
    ('tj-full-verify', 'ts-full-4', 'verifier',  'Verify implementation', 'Verify that the implementation satisfies: {{original_prompt}}', 50, 300);

INSERT OR IGNORE INTO template_job_dependencies (template_job_id, depends_on_template_job_id, dependency_type) VALUES
    ('tj-full-dev',    'tj-full-plan',   'success'),
    ('tj-full-test',   'tj-full-dev',    'success'),
    ('tj-full-verify', 'tj-full-test',   'success');

-- =====================================================================
-- TEMPLATE: Simple Plan → Code → Verify
-- =====================================================================
INSERT OR IGNORE INTO pipeline_templates (template_id, name, description, created_at, updated_at) VALUES (
    'simple-plan-code-verify',
    'Plan → Code → Verify',
    'Lightweight three-stage workflow: plan the task, implement it, then verify the result',
    '2026-02-11T00:00:00Z',
    '2026-02-11T00:00:00Z'
);

INSERT OR IGNORE INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES
    ('ts-pcv-1', 'simple-plan-code-verify', 'plan',   1),
    ('ts-pcv-2', 'simple-plan-code-verify', 'code',   2),
    ('ts-pcv-3', 'simple-plan-code-verify', 'verify', 3);

INSERT OR IGNORE INTO template_jobs
    (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds)
VALUES
    ('tj-pcv-plan',   'ts-pcv-1', 'planner',  'Break down task',       '{{original_prompt}}',                                           15, 300),
    ('tj-pcv-code',   'ts-pcv-2', 'dev',       'Implement solution',    'Implement the following: {{original_prompt}}',                   15, 300),
    ('tj-pcv-verify', 'ts-pcv-3', 'verifier',  'Verify result',         'Verify that the implementation satisfies: {{original_prompt}}',  15, 300);

INSERT OR IGNORE INTO template_job_dependencies (template_job_id, depends_on_template_job_id, dependency_type) VALUES
    ('tj-pcv-code',   'tj-pcv-plan', 'success'),
    ('tj-pcv-verify', 'tj-pcv-code', 'success');

-- =====================================================================
-- TEMPLATE: Dev + Test (No planning or verification)
-- =====================================================================
INSERT OR IGNORE INTO pipeline_templates (template_id, name, description, created_at, updated_at) VALUES (
    'template-dev-test',
    'Dev + Test',
    'Quick workflow for implementation and testing without planning',
    '2026-02-11T00:00:00Z',
    '2026-02-11T00:00:00Z'
);

INSERT OR IGNORE INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES
    ('ts-dt-1', 'template-dev-test', 'dev',  1),
    ('ts-dt-2', 'template-dev-test', 'test', 2);

INSERT OR IGNORE INTO template_jobs
    (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds)
VALUES
    ('tj-dt-dev',  'ts-dt-1', 'dev',     'Implement changes',    '{{original_prompt}}',                              50, 300),
    ('tj-dt-test', 'ts-dt-2', 'tester',  'Test implementation',  'Test the implementation of: {{original_prompt}}',  50, 300);

INSERT OR IGNORE INTO template_job_dependencies (template_job_id, depends_on_template_job_id, dependency_type) VALUES
    ('tj-dt-test', 'tj-dt-dev', 'success');

-- =====================================================================
-- TEMPLATE: Dev Only (Quick implementation, no testing)
-- =====================================================================
INSERT OR IGNORE INTO pipeline_templates (template_id, name, description, created_at, updated_at) VALUES (
    'template-dev-only',
    'Dev Only',
    'Single development job for quick prototyping',
    '2026-02-11T00:00:00Z',
    '2026-02-11T00:00:00Z'
);

INSERT OR IGNORE INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES
    ('ts-do-1', 'template-dev-only', 'dev', 1);

INSERT OR IGNORE INTO template_jobs
    (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds)
VALUES
    ('tj-do-dev', 'ts-do-1', 'dev', 'Implement changes', '{{original_prompt}}', 50, 300);

-- =====================================================================
-- TEMPLATE: Test Existing Code (No development)
-- =====================================================================
INSERT OR IGNORE INTO pipeline_templates (template_id, name, description, created_at, updated_at) VALUES (
    'template-test-only',
    'Test Existing Code',
    'Test and verify existing implementation',
    '2026-02-11T00:00:00Z',
    '2026-02-11T00:00:00Z'
);

INSERT OR IGNORE INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES
    ('ts-to-1', 'template-test-only', 'test',   1),
    ('ts-to-2', 'template-test-only', 'verify', 2);

INSERT OR IGNORE INTO template_jobs
    (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds)
VALUES
    ('tj-to-test',   'ts-to-1', 'tester',   'Test code',   'Test the existing implementation: {{original_prompt}}',   50, 300),
    ('tj-to-verify', 'ts-to-2', 'verifier', 'Verify code', 'Verify the existing implementation: {{original_prompt}}', 50, 300);

INSERT OR IGNORE INTO template_job_dependencies (template_job_id, depends_on_template_job_id, dependency_type) VALUES
    ('tj-to-verify', 'tj-to-test', 'success');

-- =====================================================================
-- TEMPLATE: Plan Only (Just break down the task)
-- =====================================================================
INSERT OR IGNORE INTO pipeline_templates (template_id, name, description, created_at, updated_at) VALUES (
    'template-plan-only',
    'Plan Only',
    'Just plan the work without executing',
    '2026-02-11T00:00:00Z',
    '2026-02-11T00:00:00Z'
);

INSERT OR IGNORE INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES
    ('ts-po-1', 'template-plan-only', 'plan', 1);

INSERT OR IGNORE INTO template_jobs
    (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds)
VALUES
    ('tj-po-plan', 'ts-po-1', 'planner', 'Break down task', '{{original_prompt}}', 50, 300);

-- =====================================================================
-- TEMPLATE: Mock Agent (Fast testing with simulated agents)
-- =====================================================================
INSERT OR IGNORE INTO pipeline_templates (template_id, name, description, created_at, updated_at) VALUES (
    'template-mock',
    'Mock Agent',
    'Test pipeline structure with fast mock agents (no LLM calls)',
    '2026-02-11T00:00:00Z',
    '2026-02-11T00:00:00Z'
);

INSERT OR IGNORE INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES
    ('ts-mock-1', 'template-mock', 'plan', 1),
    ('ts-mock-2', 'template-mock', 'dev',  2),
    ('ts-mock-3', 'template-mock', 'test', 3);

INSERT OR IGNORE INTO template_jobs
    (template_job_id, template_stage_id, agent_type, name, prompt_template, command_template, max_iterations, timeout_seconds)
VALUES
    ('tj-mock-plan', 'ts-mock-1', 'mock', 'Mock Planner',
     '{{original_prompt}}',
     'python agents/mock_agent.py --agent-type planner --failure-rate 0.05 --duration 1.5 --prompt "{{prompt}}" --python "tasks = [f''Task {i+1}: Step {i+1}'' for i in range(random.randint(3, 7))]; print(''\\n''.join(tasks))"',
     50, 300),
    ('tj-mock-dev', 'ts-mock-2', 'mock', 'Mock Developer',
     '{{original_prompt}}',
     'python agents/mock_agent.py --agent-type dev --failure-rate 0.15 --duration 3.0 --prompt "{{prompt}}" --python "files = [''app.py'', ''utils.py'', ''test_app.py'']; changes = {f: random.randint(10, 100) for f in files}; print(''\\n''.join([f''{f}: +{c} lines'' for f, c in changes.items()]))"',
     50, 300),
    ('tj-mock-test', 'ts-mock-3', 'mock', 'Mock Tester',
     '{{original_prompt}}',
     'python agents/mock_agent.py --agent-type tester --failure-rate 0.20 --duration 2.0 --prompt "{{prompt}}" --python "total = random.randint(50, 200); passed = int(total * random.uniform(0.8, 1.0)); coverage = random.randint(75, 95); print(f''Tests: {passed}/{total} passed''); print(f''Coverage: {coverage}%'')"',
     50, 300);

INSERT OR IGNORE INTO template_job_dependencies (template_job_id, depends_on_template_job_id, dependency_type) VALUES
    ('tj-mock-dev',  'tj-mock-plan', 'success'),
    ('tj-mock-test', 'tj-mock-dev',  'success');
  
-- =====================================================================
-- TEMPLATE: Modify Existing Code (Explore → Modify → Verify)
-- =====================================================================
INSERT OR IGNORE INTO pipeline_templates (template_id, name, description, created_at, updated_at) VALUES (
    'template-modify-existing',
    'Modify Existing Code',
    'Read and understand an existing codebase, then make targeted modifications',
    '2026-03-03T00:00:00Z',
    '2026-03-03T00:00:00Z'
);

INSERT OR IGNORE INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES
    ('ts-me-1', 'template-modify-existing', 'modify', 1),
    ('ts-me-2', 'template-modify-existing', 'verify', 2);

INSERT OR IGNORE INTO template_jobs
    (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds)
VALUES
    ('tj-me-modify', 'ts-me-1', 'dev', 'Modify existing code',
     'You are working on an existing codebase located in your workspace.
First, explore the directory structure and read the relevant files.
Understand the existing patterns and conventions before making any changes.
Make targeted modifications rather than rewriting from scratch.
Do not run git commands or commit changes.
Task: {{original_prompt}}',
     50, 600),
    ('tj-me-verify', 'ts-me-2', 'verifier', 'Verify modifications',
     'You are verifying changes made to an existing codebase in your workspace.
Read the relevant files and confirm the following task was completed correctly.
Do not run git commands.
Task: {{original_prompt}}',
     20, 300);

INSERT OR IGNORE INTO template_job_dependencies (template_job_id, depends_on_template_job_id, dependency_type) VALUES
    ('tj-me-verify', 'tj-me-modify', 'success');

PRAGMA foreign_keys = ON;
