# Orchestrator Implementation Plan

## Goal
Integrate pipeline orchestration into the existing FastAPI server (`server/main.py`). The server will:
- Use SQLite database instead of in-memory state
- Support pipeline templates from database
- Execute jobs via subprocess (existing logic)
- Manage artifacts and dependencies
- Support regression loops

**Note:** Orchestration happens in `server/main.py`, NOT a standalone orchestrator.

---

## Phase 1: Core Orchestrator Structure ⏳ IN PROGRESS

### 1.1 Basic Orchestrator Loop ✅ COMPLETE
**File:** `orchestrator.py`

**What:**
- Main loop that polls database for work
- Finds pending pipelines
- Finds ready-to-run jobs (dependencies satisfied)
- Basic state transitions (pending → running → completed)

**Tasks:**
- [x] Create `Orchestrator` class with main loop
- [x] Implement `find_pending_pipelines()`
- [x] Implement `find_ready_jobs(pipeline_id)` - check dependencies
- [x] Add pipeline state machine (pending → running → completed/failed)
- [x] Add basic logging

**Test:**
```bash
cd agents && python test_orchestrator.py

# Output:
[0s] Pipeline status: running
  [dev] dev: completed
  [test] tester: pending

[2s] Pipeline status: running
  [dev] dev: completed
  [test] tester: completed

[4s] Pipeline status: completed
✅ PASS: Jobs run in order, dependencies respected
```

---

### 1.2 Job Spawning ⬜ TODO
**File:** `orchestrator.py`

**What:**
- Spawn harness process for each job
- Track running jobs (PIDs/processes)
- Handle job completion
- Update job status in database

**Tasks:**
- [ ] Implement `spawn_job(job_id)` - launches harness subprocess
- [ ] Track running jobs in memory (dict: job_id → process)
- [ ] Poll for job completion
- [ ] Update job status when complete
- [ ] Handle job failures (update status='failed')

**Test:**
```python
# Create pipeline with 1 dev job
# Orchestrator spawns harness
# Verify harness runs and completes
# Verify job status updated in DB
```

---

### 1.3 Dependency Resolution ⬜ TODO
**File:** `orchestrator.py`

**What:**
- Check job dependencies before spawning
- Wait for dependency jobs to complete
- Support dependency types (success, failure, always)
- Handle stage ordering

**Tasks:**
- [ ] Implement `get_job_dependencies(job_id)` query
- [ ] Implement `check_dependencies_satisfied(job_id)` logic
- [ ] Only spawn jobs when all dependencies complete successfully
- [ ] Handle failed dependencies (skip dependent jobs)

**Test:**
```python
# Create pipeline: job1 → job2 (dependency)
# Start orchestrator
# Verify job1 runs first
# Verify job2 waits for job1 completion
# Verify job2 runs after job1 completes
```

---

## Phase 2: Artifact Management ⬜ TODO

### 2.1 Artifact Tracking ⬜ TODO
**File:** `artifacts.py`

**What:**
- Track files created by jobs
- Store artifact metadata in database
- Register artifacts when jobs complete

**Tasks:**
- [ ] Create `ArtifactManager` class
- [ ] Implement `track_file(job_id, file_path)` - monitor workspace for new files
- [ ] Implement `register_artifact(job_id, file_path, type)` - save to DB
- [ ] Calculate file hashes for change detection
- [ ] Handle artifact types (file, model_output, error_context)

**Test:**
```python
# Job writes file to workspace
# Artifact manager detects new file
# Artifact registered in DB with hash
```

---

### 2.2 Artifact Consumption ⬜ TODO
**File:** `artifacts.py`

**What:**
- Make artifacts available to dependent jobs
- Track which jobs consume which artifacts
- Copy/link artifacts to job workspace

**Tasks:**
- [ ] Implement `get_artifacts_for_job(job_id)` - query upstream artifacts
- [ ] Implement `provide_artifacts(job_id, artifacts)` - make available to job
- [ ] Record artifact consumption in `artifact_consumption` table
- [ ] Handle artifact passing between stages

**Test:**
```python
# Job1 produces artifact
# Job2 depends on Job1
# Verify Job2 can access Job1's artifact
# Verify consumption recorded in DB
```

---

### 2.3 Model Output Artifacts ⬜ TODO
**File:** `artifacts.py`

**What:**
- Store model outputs (JSON, text) in database
- Extract structured data from job results
- Make model outputs available to next jobs

**Tasks:**
- [ ] Implement `extract_model_output(job_id)` - parse actions.jsonl
- [ ] Store JSON outputs in artifacts table (content field)
- [ ] Provide model outputs to dependent jobs via context

**Test:**
```python
# Planner job produces task_plan.json
# Dev job consumes plan as context
# Verify dev prompt includes plan details
```

---

## Phase 3: Regression Support ⬜ TODO

### 3.1 Job Spawning from Jobs ⬜ TODO
**File:** `regression.py`

**What:**
- Allow jobs to spawn new jobs
- Track parent-child relationships
- Update dependencies dynamically

**Tasks:**
- [ ] Create special "spawn_job" tool for agents
- [ ] Implement `handle_job_spawn(parent_job_id, new_job_spec)`
- [ ] Set parent_job_id and regression_context
- [ ] Add new job to pipeline
- [ ] Update dependencies (parent waits for spawned job)

**Test:**
```python
# Test job spawns dev job
# Verify dev job created with parent_job_id
# Verify test job waits for new dev job
# Verify dev job artifacts flow back to test
```

---

### 3.2 Regression Context Passing ⬜ TODO
**File:** `regression.py`

**What:**
- Pass context from spawning job to spawned job
- Include reason for spawning
- Maintain original_prompt immutability

**Tasks:**
- [ ] Capture spawning job's output/reasoning
- [ ] Store in spawned job's regression_context JSON
- [ ] Include regression_context in spawned job's prompt
- [ ] Verify original_prompt remains unchanged

**Test:**
```python
# Test discovers missing coverage
# Test spawns dev with context: "Add tests for login"
# Verify dev job prompt includes context
# Verify pipeline.original_prompt unchanged
```

---

## Phase 4: Advanced Features ⬜ TODO

### 4.1 Parallel Job Execution ⬜ TODO
**What:**
- Run independent jobs in same stage concurrently
- Limit concurrent jobs (configurable)
- Handle resource constraints

**Tasks:**
- [ ] Implement max concurrent jobs limit
- [ ] Spawn multiple jobs in parallel
- [ ] Track available slots
- [ ] Queue jobs when limit reached

---

### 4.2 Job Retry Logic ⬜ TODO
**What:**
- Retry failed jobs (configurable)
- Exponential backoff
- Max retry limit

**Tasks:**
- [ ] Add retry_count to jobs table
- [ ] Implement retry logic on failure
- [ ] Add max_retries to job config
- [ ] Update status after max retries exhausted

---

### 4.3 Pipeline Cancellation ⬜ TODO
**What:**
- User can cancel running pipeline
- Kill running jobs
- Clean up state

**Tasks:**
- [ ] Add pipeline.status='cancelled' support
- [ ] Implement `cancel_pipeline(pipeline_id)`
- [ ] Kill running job processes
- [ ] Update job statuses

---

### 4.4 Pipeline Monitoring ⬜ TODO
**What:**
- Web UI or CLI to view pipeline status
- Real-time updates
- Job logs viewing

**Tasks:**
- [ ] Add `/api/pipelines` endpoint
- [ ] Add `/api/pipelines/{id}/status` endpoint
- [ ] Stream job logs via websocket
- [ ] Update client UI to display pipeline progress

---

## Phase 5: Integration & Polish ⬜ TODO

### 5.1 Database Migration ⬜ TODO
**What:**
- Migrate from old schema to pipeline schema
- Update harness to work with jobs table
- Deprecate old tasks/agent_state tables

**Tasks:**
- [ ] Create migration script
- [ ] Update harness.py to use jobs table
- [ ] Update db.py with pipeline methods
- [ ] Test backward compatibility

---

### 5.2 Error Handling ⬜ TODO
**What:**
- Graceful error handling throughout
- Proper cleanup on crashes
- Dead job detection

**Tasks:**
- [ ] Add try/except around all operations
- [ ] Implement heartbeat checking for dead jobs
- [ ] Clean up zombie processes
- [ ] Log all errors with context

---

### 5.3 Testing & Documentation ⬜ TODO
**What:**
- Comprehensive test suite
- Integration tests for full pipelines
- Updated documentation

**Tasks:**
- [ ] Write unit tests for orchestrator
- [ ] Write integration tests (end-to-end pipelines)
- [ ] Update README with orchestrator usage
- [ ] Add example pipelines

---

## Success Criteria

**Minimum Viable Orchestrator (Phase 1-2):**
- ✅ Can execute simple linear pipeline (plan → dev → test → verify)
- ✅ Jobs run in order respecting dependencies
- ✅ Artifacts tracked and passed between jobs
- ✅ Pipeline completes with correct status

**Full Orchestrator (Phase 1-3):**
- ✅ Supports regression (test → dev feedback loops)
- ✅ Handles parallel jobs in same stage
- ✅ Proper error handling and retries
- ✅ Can be monitored via UI/API

---

## Development Notes

**Current State:**
- ✅ Database schema complete (pipelines, stages, jobs, artifacts)
- ✅ Templates implemented and tested
- ✅ Harness works with old schema (needs migration to use job_id)
- ✅ **Server Integration Complete:**
  - Replaced in-memory state with SQLite database
  - Integrated TemplateManager for template-based pipelines
  - Added orchestration loop as background task
  - Updated all API endpoints to use database
  - Jobs spawn via harness subprocess (placeholder)
  - Tested with existing client (needs update)

**REVISED APPROACH - Server Integration:**
1. ~~Create standalone orchestrator.py~~ ❌ (Not needed)
2. Integrate database into `server/main.py` ✅
3. Replace in-memory state with database queries
4. Use TemplateManager for templates (not YAML)
5. Keep existing subprocess job execution
6. Add background task for orchestration cycle

**Next Immediate Steps:**
1. Update server/main.py to use ClowderDB
2. Replace running_pipelines dict with database
3. Wire up template endpoints to TemplateManager
4. Add background orchestration cycle
5. Test with existing client

---

## Timeline Estimate

- **Phase 1 (Core):** ~4-6 hours of development
- **Phase 2 (Artifacts):** ~2-3 hours
- **Phase 3 (Regression):** ~2-3 hours
- **Phase 4 (Advanced):** ~3-4 hours
- **Phase 5 (Polish):** ~2-3 hours

**Total:** ~13-19 hours for complete orchestrator

---

**Last Updated:** 2026-02-11
**Status:** Planning complete, ready to start Phase 1.1
