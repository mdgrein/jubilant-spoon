-- Clowder Pipeline Schema
-- Inspired by GitLab CI/CD but with feedback loop support

-- =====================================================================
-- PIPELINE TEMPLATES: Reusable workflow definitions
-- =====================================================================
CREATE TABLE pipeline_templates (
    template_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Template stages (blueprint for stages)
CREATE TABLE template_stages (
    template_stage_id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    name TEXT NOT NULL,
    stage_order INTEGER NOT NULL,
    FOREIGN KEY(template_id) REFERENCES pipeline_templates(template_id) ON DELETE CASCADE,
    UNIQUE(template_id, name),
    UNIQUE(template_id, stage_order)
);

-- Template jobs (blueprint for jobs)
CREATE TABLE template_jobs (
    template_job_id TEXT PRIMARY KEY,
    template_stage_id TEXT NOT NULL,
    agent_type TEXT NOT NULL CHECK(agent_type IN ('planner', 'dev', 'tester', 'verifier', 'mock')),
    name TEXT NOT NULL,
    prompt_template TEXT,  -- Optional prompt template with placeholders
    command_template TEXT,  -- Optional: custom command instead of harness (supports {{job_id}}, {{prompt}}, etc.)
    max_iterations INTEGER DEFAULT 50,
    timeout_seconds INTEGER DEFAULT 300,
    artifact_strategy JSON,  -- Defines how artifacts are collected (e.g., {"type": "stdout_final"} or {"type": "git_diff"})
    job_multiplier JSON,  -- Defines dynamic job spawning from another job's output (e.g., {"source_job_id": "job-planner", "parse_strategy": "json_array"})
    retry_strategy JSON,  -- How to handle retries (e.g., {"include_context": true, "context_instruction": "Continue from where you left off..."})
    FOREIGN KEY(template_stage_id) REFERENCES template_stages(template_stage_id) ON DELETE CASCADE
);

-- Template job dependencies (blueprint for dependencies)
CREATE TABLE template_job_dependencies (
    template_job_id TEXT NOT NULL,
    depends_on_template_job_id TEXT NOT NULL,
    dependency_type TEXT DEFAULT 'success' CHECK(dependency_type IN ('success', 'failure', 'always')),
    PRIMARY KEY(template_job_id, depends_on_template_job_id),
    FOREIGN KEY(template_job_id) REFERENCES template_jobs(template_job_id) ON DELETE CASCADE,
    FOREIGN KEY(depends_on_template_job_id) REFERENCES template_jobs(template_job_id) ON DELETE CASCADE
);

CREATE INDEX idx_template_stages ON template_stages(template_id, stage_order);
CREATE INDEX idx_template_jobs ON template_jobs(template_stage_id);

-- =====================================================================
-- PIPELINES: Top-level workflow containers
-- =====================================================================
CREATE TABLE pipelines (
    pipeline_id TEXT PRIMARY KEY,
    template_id TEXT,  -- Which template was used (NULL if custom)
    original_prompt TEXT NOT NULL,  -- IMMUTABLE: The user's initial request
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    metadata JSON,  -- User-defined data
    FOREIGN KEY(template_id) REFERENCES pipeline_templates(template_id) ON DELETE SET NULL
);

CREATE INDEX idx_pipelines_status ON pipelines(status);
CREATE INDEX idx_pipelines_created ON pipelines(created_at);

-- =====================================================================
-- STAGES: Ordered phases within a pipeline (plan → dev → test → verify)
-- =====================================================================
CREATE TABLE stages (
    stage_id TEXT PRIMARY KEY,
    pipeline_id TEXT NOT NULL,
    name TEXT NOT NULL,  -- e.g., "plan", "dev", "test", "verify"
    stage_order INTEGER NOT NULL,  -- Execution order (1, 2, 3, ...)
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(pipeline_id) REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    UNIQUE(pipeline_id, name),
    UNIQUE(pipeline_id, stage_order)
);

CREATE INDEX idx_stages_pipeline ON stages(pipeline_id, stage_order);

-- =====================================================================
-- JOBS: Individual agent executions (replaces old "tasks" table)
-- =====================================================================
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    pipeline_id TEXT NOT NULL,
    stage_id TEXT NOT NULL,
    agent_type TEXT NOT NULL CHECK(agent_type IN ('planner', 'dev', 'tester', 'verifier', 'mock')),

    -- The prompt for THIS specific job (may differ from pipeline.original_prompt)
    prompt TEXT NOT NULL,
    original_prompt TEXT,  -- Stores the original prompt before retry context augmentation

    -- Execution: Either runs harness (default) or custom command
    command TEXT,  -- Optional: custom command to run instead of harness

    -- Execution constraints
    max_iterations INTEGER NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    allowed_paths TEXT NOT NULL,  -- JSON array

    -- Status tracking
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'skipped')),
    iteration INTEGER DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    termination_reason TEXT,

    -- Retry support
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 100,
    retry_strategy JSON,  -- How to handle retries: include_context, context_instruction, etc.

    -- Job output (stdout/stderr combined)
    job_output TEXT,

    -- Artifact collection strategy
    artifact_strategy JSON,  -- Copied from template, defines how to collect artifacts

    -- Template tracking (for job multiplier matching)
    template_job_id TEXT,  -- Which template job this was instantiated from

    -- REGRESSION SUPPORT: Track which job triggered this one
    parent_job_id TEXT,  -- If this job was spawned by another job (for feedback loops)
    regression_context JSON,  -- Why this job was spawned (e.g., "Missing test coverage for login")

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY(pipeline_id) REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    FOREIGN KEY(stage_id) REFERENCES stages(stage_id) ON DELETE CASCADE,
    FOREIGN KEY(parent_job_id) REFERENCES jobs(job_id) ON DELETE SET NULL
);

CREATE INDEX idx_jobs_pipeline ON jobs(pipeline_id);
CREATE INDEX idx_jobs_stage ON jobs(stage_id);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_parent ON jobs(parent_job_id);

-- =====================================================================
-- JOB DEPENDENCIES: Jobs can wait for other jobs (like GitLab "needs")
-- =====================================================================
CREATE TABLE job_dependencies (
    job_id TEXT NOT NULL,
    depends_on_job_id TEXT NOT NULL,
    dependency_type TEXT DEFAULT 'success' CHECK(dependency_type IN ('success', 'failure', 'always')),
    PRIMARY KEY(job_id, depends_on_job_id),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE,
    FOREIGN KEY(depends_on_job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX idx_job_deps_depends ON job_dependencies(depends_on_job_id);

-- =====================================================================
-- ACTIONS: Line-by-line iteration history (same as before)
-- =====================================================================
CREATE TABLE actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,  -- Changed from task_id to job_id
    iteration INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    llm_response JSON,
    results JSON,
    raw_stdout TEXT,
    raw_stderr TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX idx_actions_job ON actions(job_id, iteration);
CREATE INDEX idx_actions_timestamp ON actions(timestamp);

-- =====================================================================
-- ARTIFACTS: Files and model outputs produced by jobs
-- =====================================================================
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,

    -- Type of artifact
    type TEXT NOT NULL CHECK(type IN ('file', 'model_output', 'error_context', 'test_results', 'verification_report')),

    -- Identification
    name TEXT NOT NULL,  -- e.g., "implementation.py", "test_plan.json"
    description TEXT,

    -- Storage
    file_path TEXT,  -- For files on disk (code, outputs)
    content TEXT,  -- For model outputs stored in DB (JSON, text)
    content_hash TEXT,  -- SHA256 for change tracking
    size_bytes INTEGER,

    -- Metadata
    metadata JSON,  -- Flexible field (mime_type, language, etc.)

    created_at TEXT NOT NULL,

    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX idx_artifacts_job ON artifacts(job_id);
CREATE INDEX idx_artifacts_type ON artifacts(type);
CREATE INDEX idx_artifacts_name ON artifacts(name);

-- =====================================================================
-- ARTIFACT CONSUMPTION: Track which jobs use which artifacts
-- =====================================================================
CREATE TABLE artifact_consumption (
    job_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    PRIMARY KEY(job_id, artifact_id),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id) ON DELETE CASCADE
);

CREATE INDEX idx_artifact_consumption_artifact ON artifact_consumption(artifact_id);

-- =====================================================================
-- VIEWS: Useful queries
-- =====================================================================

-- Active pipelines with current stage
CREATE VIEW active_pipelines AS
SELECT
    p.pipeline_id,
    p.original_prompt,
    p.status,
    s.name as current_stage,
    s.stage_order,
    COUNT(DISTINCT j.job_id) as total_jobs,
    SUM(CASE WHEN j.status = 'completed' THEN 1 ELSE 0 END) as completed_jobs,
    p.created_at
FROM pipelines p
LEFT JOIN stages s ON p.pipeline_id = s.pipeline_id AND s.status = 'running'
LEFT JOIN jobs j ON p.pipeline_id = j.pipeline_id
WHERE p.status IN ('pending', 'running')
GROUP BY p.pipeline_id;

-- Job summary with artifact counts
CREATE VIEW job_summary AS
SELECT
    j.job_id,
    j.pipeline_id,
    s.name as stage_name,
    j.agent_type,
    j.prompt,
    j.status,
    j.iteration,
    j.max_iterations,
    COUNT(DISTINCT a.artifact_id) as artifacts_produced,
    j.parent_job_id,
    j.started_at,
    j.completed_at
FROM jobs j
JOIN stages s ON j.stage_id = s.stage_id
LEFT JOIN artifacts a ON j.job_id = a.job_id
GROUP BY j.job_id;

-- Artifact flow: Which jobs produced/consumed each artifact
CREATE VIEW artifact_flow AS
SELECT
    a.artifact_id,
    a.name as artifact_name,
    a.type as artifact_type,
    producer.job_id as produced_by_job,
    producer.agent_type as produced_by_agent,
    consumer.job_id as consumed_by_job,
    consumer.agent_type as consumed_by_agent,
    ac.consumed_at
FROM artifacts a
JOIN jobs producer ON a.job_id = producer.job_id
LEFT JOIN artifact_consumption ac ON a.artifact_id = ac.artifact_id
LEFT JOIN jobs consumer ON ac.job_id = consumer.job_id;

-- Regression chains: Track feedback loops
CREATE VIEW regression_chains AS
WITH RECURSIVE chain AS (
    -- Base case: Jobs with no parent (original jobs)
    SELECT
        job_id,
        prompt,
        parent_job_id,
        0 as depth,
        job_id as root_job_id
    FROM jobs
    WHERE parent_job_id IS NULL

    UNION ALL

    -- Recursive case: Jobs spawned by other jobs
    SELECT
        j.job_id,
        j.prompt,
        j.parent_job_id,
        c.depth + 1,
        c.root_job_id
    FROM jobs j
    JOIN chain c ON j.parent_job_id = c.job_id
)
SELECT * FROM chain;
