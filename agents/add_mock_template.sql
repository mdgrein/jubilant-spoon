-- Add Mock Agent template to existing database
-- Run this if you don't want to delete your database

INSERT OR IGNORE INTO pipeline_templates VALUES (
    'template-mock',
    'Mock Agent',
    'Test pipeline structure with fast mock agents (no LLM calls)',
    datetime('now'),
    datetime('now')
);

INSERT OR IGNORE INTO template_stages VALUES ('ts-mock-1', 'template-mock', 'plan', 1);
INSERT OR IGNORE INTO template_stages VALUES ('ts-mock-2', 'template-mock', 'dev', 2);
INSERT OR IGNORE INTO template_stages VALUES ('ts-mock-3', 'template-mock', 'test', 3);

INSERT OR IGNORE INTO template_jobs VALUES (
    'tj-mock-plan',
    'ts-mock-1',
    'mock',
    'Mock Planner',
    '{{original_prompt}}',
    'python agents/mock_agent.py --agent-type planner --failure-rate 0.05 --duration 1.5 --prompt "{{prompt}}" --python "tasks = [f''Task {i+1}: Step {i+1}'' for i in range(random.randint(3, 7))]; print(''\\n''.join(tasks))"',
    50, 300
);

INSERT OR IGNORE INTO template_jobs VALUES (
    'tj-mock-dev',
    'ts-mock-2',
    'mock',
    'Mock Developer',
    '{{original_prompt}}',
    'python agents/mock_agent.py --agent-type dev --failure-rate 0.15 --duration 3.0 --prompt "{{prompt}}" --python "files = [''app.py'', ''utils.py'', ''test_app.py'']; changes = {f: random.randint(10, 100) for f in files}; print(''\\n''.join([f''{f}: +{c} lines'' for f, c in changes.items()]))"',
    50, 300
);

INSERT OR IGNORE INTO template_jobs VALUES (
    'tj-mock-test',
    'ts-mock-3',
    'mock',
    'Mock Tester',
    '{{original_prompt}}',
    'python agents/mock_agent.py --agent-type tester --failure-rate 0.20 --duration 2.0 --prompt "{{prompt}}" --python "total = random.randint(50, 200); passed = int(total * random.uniform(0.8, 1.0)); coverage = random.randint(75, 95); print(f''Tests: {passed}/{total} passed''); print(f''Coverage: {coverage}%'')"',
    50, 300
);

INSERT OR IGNORE INTO template_job_dependencies VALUES ('tj-mock-dev', 'tj-mock-plan', 'success');
INSERT OR IGNORE INTO template_job_dependencies VALUES ('tj-mock-test', 'tj-mock-dev', 'success');
