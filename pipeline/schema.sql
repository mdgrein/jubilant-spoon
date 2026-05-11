-- Clowder SQLite Schema
-- State management for agent orchestration

-- Tasks table: Immutable task definitions
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    agent_type TEXT DEFAULT 'dev',  -- dev, planner, tester, verifier
    max_iterations INTEGER NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    allowed_paths TEXT NOT NULL,  -- JSON array
    created_at TEXT NOT NULL,
    parent_task_id TEXT,  -- For task hierarchies
    metadata JSON,  -- Flexible field for custom data
    FOREIGN KEY(parent_task_id) REFERENCES tasks(task_id)
);

CREATE INDEX idx_tasks_created ON tasks(created_at);
CREATE INDEX idx_tasks_agent_type ON tasks(agent_type);
CREATE INDEX idx_tasks_parent ON tasks(parent_task_id);

-- Agent state table: Mutable runtime state
CREATE TABLE agent_state (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed', 'stopped')),
    iteration INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    termination_reason TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE INDEX idx_state_status ON agent_state(status);
CREATE INDEX idx_state_updated ON agent_state(updated_at);

-- Actions table: One row per iteration
CREATE TABLE actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    llm_response JSON,  -- Parsed JSON from LLM
    results JSON,  -- Tool execution results
    raw_stdout TEXT,  -- Raw LLM output
    raw_stderr TEXT,  -- Error output from LLM runtime
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE INDEX idx_actions_task ON actions(task_id, iteration);
CREATE INDEX idx_actions_timestamp ON actions(timestamp);

-- Task dependencies: For planner-created workflows
CREATE TABLE task_dependencies (
    task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    PRIMARY KEY(task_id, depends_on_task_id),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
    FOREIGN KEY(depends_on_task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

-- Artifacts table: Track files created/modified by agents
CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('create', 'write', 'delete')),
    timestamp TEXT NOT NULL,
    content_hash TEXT,  -- For change tracking
    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE INDEX idx_artifacts_task ON artifacts(task_id);
CREATE INDEX idx_artifacts_path ON artifacts(file_path);

-- Views for common queries

-- Active tasks with current state
CREATE VIEW active_tasks AS
SELECT
    t.task_id,
    t.prompt,
    t.agent_type,
    s.status,
    s.iteration,
    t.max_iterations,
    s.started_at,
    s.updated_at
FROM tasks t
JOIN agent_state s ON t.task_id = s.task_id
WHERE s.status IN ('running', 'pending');

-- Task summary with stats
CREATE VIEW task_summary AS
SELECT
    t.task_id,
    t.prompt,
    t.agent_type,
    s.status,
    s.iteration,
    t.max_iterations,
    COUNT(a.id) as total_actions,
    s.termination_reason,
    s.started_at,
    s.updated_at
FROM tasks t
JOIN agent_state s ON t.task_id = s.task_id
LEFT JOIN actions a ON t.task_id = a.task_id
GROUP BY t.task_id;

-- Failed actions for debugging
CREATE VIEW failed_actions AS
SELECT
    a.task_id,
    a.iteration,
    a.timestamp,
    json_extract(a.results, '$') as results,
    a.raw_stdout
FROM actions a
WHERE json_extract(a.results, '$[0].status') LIKE '%error%'
   OR json_extract(a.results, '$[0].status') = 'validation_error';
