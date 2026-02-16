# Clowder Pipeline System

Inspired by GitLab CI/CD with support for **feedback loops**.

## Hierarchy

```
Pipeline (original_prompt: "Add user authentication")
  ├── Stage 1: plan
  │   └── Job 1: planner agent
  ├── Stage 2: dev
  │   ├── Job 2: dev agent (implements auth)
  │   └── Job 5: dev agent (adds missing tests) ← REGRESSION
  ├── Stage 3: test
  │   └── Job 3: tester agent (spawns Job 5)
  └── Stage 4: verify
      └── Job 4: verifier agent
```

## Key Concepts

### Pipelines
- Created from user request
- `original_prompt` is **immutable** - never changes
- Contains multiple stages in order

### Stages
- Sequential phases: plan → dev → test → verify
- Jobs within a stage can run in parallel
- Each stage has an `order` field

### Jobs
- Individual agent executions (our old "tasks")
- Each job has its own `prompt` (may differ from `original_prompt`)
- Jobs produce **artifacts**
- Jobs can spawn other jobs (**regression**)

### Artifacts
- **Files on disk**: Code, configs, docs
- **Model outputs**: Test plans, verification reports, error contexts
- Tracked in database with `file_path` or `content`
- Consumed by later jobs via `artifact_consumption`

## Regression (Feedback Loops)

**The key feature**: Later jobs can trigger earlier stages with new prompts.

### Example: Test → Dev Feedback Loop

```sql
-- Initial pipeline
Pipeline(original_prompt="Add user authentication")

-- Stage 2: Dev job completes
Job 2: agent=dev, prompt="Add user authentication", stage=dev
  → Produces: auth.py

-- Stage 3: Test job analyzes and finds gaps
Job 3: agent=tester, prompt="Test authentication", stage=test
  → Discovers: Missing test for password reset
  → Spawns Job 5

-- REGRESSION: New dev job triggered by test job
Job 5: agent=dev, prompt="Add test for password reset", stage=dev
       parent_job_id=3
       regression_context={"reason": "Missing test coverage", "requested_by": "tester"}
  → Produces: test_password_reset.py

-- Test job continues with new artifact
Job 3: Consumes artifact from Job 5
  → Verifies test coverage is now adequate
```

### Database Records

```sql
INSERT INTO jobs VALUES (
    'job-5',
    'pipeline-1',
    'stage-dev',
    'dev',
    'Add test for password reset',  -- Different from original_prompt!
    50, 300, '["/workspace"]',
    'pending', 0, NULL, NULL, NULL,
    'job-3',  -- parent_job_id: Spawned by test job
    '{"reason": "Missing test coverage for password reset"}',
    '2026-02-11T00:00:00Z', '2026-02-11T00:00:00Z'
);
```

## Artifact Flow

```
Job 2 (dev) → produces → auth.py
                ↓
Job 3 (test) → consumes → auth.py
             → produces → test_plan.json
                ↓
Job 5 (dev) → consumes → test_plan.json
            → produces → test_password_reset.py
                ↓
Job 3 (test) → consumes → test_password_reset.py
             → produces → test_results.json
                ↓
Job 4 (verify) → consumes → all artifacts
               → produces → verification_report.json
```

### Artifact Types

1. **file**: Code files on disk (`file_path` set)
2. **model_output**: JSON/text in database (`content` set)
3. **error_context**: Full context from failed jobs
4. **test_results**: Test execution outputs
5. **verification_report**: Verifier findings

## Job Dependencies

Jobs can wait for specific other jobs (like GitLab `needs`):

```sql
-- Job 3 (test) depends on Job 2 (dev)
INSERT INTO job_dependencies VALUES ('job-3', 'job-2', 'success');

-- Job 4 (verify) depends on Job 3 (test)
INSERT INTO job_dependencies VALUES ('job-4', 'job-3', 'success');

-- Job 3 (test) ALSO depends on Job 5 (regressed dev)
INSERT INTO job_dependencies VALUES ('job-3', 'job-5', 'success');
```

This creates a DAG with cycles via regression.

## Views

### active_pipelines
```sql
SELECT * FROM active_pipelines;
-- Shows running pipelines with current stage and job counts
```

### job_summary
```sql
SELECT * FROM job_summary WHERE pipeline_id='pipeline-1';
-- Shows all jobs in a pipeline with artifact counts
```

### artifact_flow
```sql
SELECT * FROM artifact_flow WHERE pipeline_id='pipeline-1';
-- Shows producer → consumer relationships for all artifacts
```

### regression_chains
```sql
SELECT * FROM regression_chains WHERE root_job_id='job-2';
-- Shows all jobs spawned from job-2 (depth, parent relationships)
```

## Comparison to GitLab

### Similar
- ✅ Pipelines contain stages contain jobs
- ✅ Stages run sequentially, jobs in parallel
- ✅ Job dependencies (`needs`)
- ✅ Artifact passing between jobs

### Different
- ✨ **Regression**: Jobs can spawn earlier-stage jobs with new prompts
- ✨ **Immutable original_prompt**: Pipeline's initial request never changes
- ✨ **Parent tracking**: `parent_job_id` tracks feedback loops
- ✨ **Artifact consumption tracking**: Explicit record of artifact usage

## Example Pipeline Execution

```python
# User creates pipeline
pipeline = Pipeline(original_prompt="Add user authentication")

# Stage 1: Planner breaks down task
planner_job = Job(prompt="Add user authentication", agent="planner")
# Produces: task_plan.json

# Stage 2: Dev implements
dev_job = Job(prompt="Implement auth module", agent="dev")
# Consumes: task_plan.json
# Produces: auth.py, db_schema.sql

# Stage 3: Tester analyzes
test_job = Job(prompt="Test auth module", agent="tester")
# Consumes: auth.py, db_schema.sql
# Produces: test_plan.json
# Discovers: Missing password reset tests
# SPAWNS: new_dev_job

# Stage 2 (again): Dev adds missing tests
new_dev_job = Job(
    prompt="Add password reset tests",
    agent="dev",
    parent_job_id=test_job.id
)
# Produces: test_password_reset.py

# Stage 3 (continues): Test verifies
test_job.consume(test_password_reset.py)
# Produces: test_results.json (pass)

# Stage 4: Verifier checks
verify_job = Job(prompt="Verify auth implementation", agent="verifier")
# Consumes: ALL artifacts
# Produces: verification_report.json
# Decision: PASS → pipeline completes
```

## Migration from Old Schema

The new `jobs` table replaces the old `tasks` table:

```sql
-- Old: tasks
-- New: jobs (with pipeline_id, stage_id, parent_job_id)

-- Old: agent_state (separate table)
-- New: status fields directly in jobs

-- Old: actions(task_id)
-- New: actions(job_id)
```
