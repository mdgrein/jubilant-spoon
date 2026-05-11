# Pipeline Templates

Pre-defined workflows that users can select and customize before execution.

## Available Templates

### 1. **Full Workflow** (4 stages, 4 jobs)
```
plan → dev → test → verify
```
Complete AI development workflow with planning, implementation, testing, and verification.

**Use when:** You want the complete cycle with quality checks.

---

### 2. **Dev + Test** (2 stages, 2 jobs)
```
dev → test
```
Quick workflow for implementation and testing without planning.

**Use when:** Task is clear and doesn't need planning or formal verification.

---

### 3. **Dev Only** (1 stage, 1 job)
```
dev
```
Single development job for quick prototyping.

**Use when:** You want fast implementation without any checks.

---

### 4. **Test Existing Code** (2 stages, 2 jobs)
```
test → verify
```
Test and verify existing implementation.

**Use when:** Code exists and you want quality checks only.

---

### 5. **Plan Only** (1 stage, 1 job)
```
plan
```
Just break down the task without executing.

**Use when:** You want to understand complexity before committing.

---

## Workflow: Using Templates

### 1. Open Client & Select Template

```python
from agents.templates import TemplateManager
from agents.db import ClowderDB

db = ClowderDB('clowder.db')
tm = TemplateManager(db)

# List available templates
templates = tm.list_templates()
for t in templates:
    print(f"{t['name']}: {t['stage_count']} stages, {t['job_count']} jobs")
```

**Client UI shows:**
```
┌─────────────────────────────────────┐
│ SELECT PIPELINE TEMPLATE            │
├─────────────────────────────────────┤
│ ○ Full Workflow (4 stages)          │
│   plan → dev → test → verify        │
│                                     │
│ ○ Dev + Test (2 stages)             │
│   dev → test                        │
│                                     │
│ ○ Dev Only (1 stage)                │
│   dev                               │
│                                     │
│ ○ Test Existing Code (2 stages)    │
│   test → verify                     │
│                                     │
│ ○ Plan Only (1 stage)               │
│   plan                              │
└─────────────────────────────────────┘
```

---

### 2. Customize Template (Remove Unwanted Stages/Jobs)

```python
# Get template details
template = tm.get_template('template-full')

# User can see and remove stages/jobs
for stage in template['stages']:
    print(f"Stage: {stage['name']}")
    for job in stage['jobs']:
        print(f"  - {job['name']} ({job['agent_type']})")
        print(f"    [Keep] [Remove]")
```

**Client UI shows:**
```
┌─────────────────────────────────────┐
│ CUSTOMIZE PIPELINE                  │
├─────────────────────────────────────┤
│ Stage 1: plan                       │
│   ✓ Break down task (planner)      │
│                                     │
│ Stage 2: dev                        │
│   ✓ Implement changes (dev)        │
│                                     │
│ Stage 3: test                       │
│   ✗ Plan and verify tests (tester) │  ← User unchecked
│                                     │
│ Stage 4: verify                     │
│   ✓ Verify implementation (verifier)│
│                                     │
│ [Create Pipeline]                   │
└─────────────────────────────────────┘
```

---

### 3. Provide Prompt

```python
original_prompt = input("What do you want to build? ")
# User enters: "Add user authentication with JWT tokens"
```

**Client UI shows:**
```
┌─────────────────────────────────────┐
│ PIPELINE PROMPT                     │
├─────────────────────────────────────┤
│ What do you want to build?          │
│                                     │
│ ┌─────────────────────────────────┐ │
│ │ Add user authentication with    │ │
│ │ JWT tokens                      │ │
│ └─────────────────────────────────┘ │
│                                     │
│ [Start Pipeline]                    │
└─────────────────────────────────────┘
```

---

### 4. Create Pipeline

```python
pipeline_id = tm.instantiate_template(
    template_id='template-full',
    original_prompt='Add user authentication with JWT tokens',
    workspace_path='/path/to/workspace',
    excluded_stage_ids=['ts-full-3'],  # Excluded test stage
)

print(f"Pipeline created: {pipeline_id}")
```

**What happens:**
1. Creates pipeline record with `original_prompt`
2. Creates stages (excluding removed ones)
3. Creates jobs with prompts like:
   - `"Add user authentication with JWT tokens"` (planner)
   - `"Implement the following: Add user authentication with JWT tokens"` (dev)
   - `"Verify that the implementation satisfies: Add user authentication with JWT tokens"` (verifier)
4. Creates dependencies between jobs
5. Pipeline status = `pending`

---

### 5. Orchestrator Executes Pipeline

```python
# Orchestrator monitors database for pending pipelines
pipelines = db.conn.execute("""
    SELECT * FROM pipelines WHERE status='pending'
""").fetchall()

for pipeline in pipelines:
    # Find jobs ready to run (no pending dependencies)
    ready_jobs = find_ready_jobs(pipeline['pipeline_id'])

    for job in ready_jobs:
        # Spawn harness for this job
        spawn_harness(job['job_id'])
```

**Pipeline execution flow:**
```
1. Start pipeline
2. Stage 1 (plan): Run planner job
   → Produces: task_breakdown.json
3. Stage 2 (dev): Run dev job (depends on planner)
   → Consumes: task_breakdown.json
   → Produces: auth.py, jwt_handler.py
4. Stage 3 (test): SKIPPED (user removed)
5. Stage 4 (verify): Run verifier job (depends on dev)
   → Consumes: auth.py, jwt_handler.py
   → Produces: verification_report.json
6. Pipeline complete
```

---

### 6. Monitor Progress

```python
# Get pipeline status
summary = db.conn.execute("""
    SELECT * FROM active_pipelines WHERE pipeline_id = ?
""", (pipeline_id,)).fetchone()

print(f"Stage: {summary['current_stage']}")
print(f"Jobs: {summary['completed_jobs']}/{summary['total_jobs']}")
```

**Client UI shows:**
```
┌─────────────────────────────────────┐
│ PIPELINE: Add user authentication   │
├─────────────────────────────────────┤
│ [✓] plan                            │
│ [⋯] dev (iteration 12/50)           │
│ [ ] verify                          │
│                                     │
│ Progress: 1/3 jobs completed        │
└─────────────────────────────────────┘
```

---

## Template Prompt Substitution

Templates use `{{original_prompt}}` as a placeholder:

```sql
-- Template job
prompt_template: "Implement the following: {{original_prompt}}"

-- User provides
original_prompt: "Add user authentication with JWT tokens"

-- Actual job gets
prompt: "Implement the following: Add user authentication with JWT tokens"
```

This allows templates to be reusable while personalizing each job.

---

## Creating Custom Templates

```sql
-- 1. Create template
INSERT INTO pipeline_templates VALUES (
    'template-custom',
    'My Custom Workflow',
    'Description here',
    '2026-02-11T00:00:00Z',
    '2026-02-11T00:00:00Z'
);

-- 2. Add stages
INSERT INTO template_stages VALUES (
    'ts-custom-1', 'template-custom', 'dev', 1
);
INSERT INTO template_stages VALUES (
    'ts-custom-2', 'template-custom', 'review', 2
);

-- 3. Add jobs
INSERT INTO template_jobs VALUES (
    'tj-custom-dev',
    'ts-custom-1',
    'dev',
    'Implement feature',
    '{{original_prompt}}',
    50, 300
);
INSERT INTO template_jobs VALUES (
    'tj-custom-review',
    'ts-custom-2',
    'verifier',
    'Review code',
    'Review: {{original_prompt}}',
    30, 180
);

-- 4. Add dependencies
INSERT INTO template_job_dependencies VALUES (
    'tj-custom-review', 'tj-custom-dev', 'success'
);
```

---

## Benefits

1. **Reusability**: Define workflows once, use many times
2. **Flexibility**: Remove unwanted stages before execution
3. **Consistency**: Same workflow structure across projects
4. **Customization**: Each pipeline gets personalized prompts
5. **Quick start**: Pick template → provide prompt → go

---

## Next Steps

1. Build orchestrator to execute pipelines
2. Implement job spawning and monitoring
3. Add artifact tracking as jobs complete
4. Support regression (test → dev feedback loops)
5. Build UI for template selection and customization
