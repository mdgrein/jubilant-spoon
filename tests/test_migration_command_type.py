"""Tests for the add_command_agent_type migration."""

import sqlite3
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))


_PRE_MIGRATION_DDL = """
CREATE TABLE pipeline_templates (
    template_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    description TEXT, category TEXT,
    default_vendor TEXT NOT NULL DEFAULT 'local-ollama', default_model TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE template_stages (
    template_stage_id TEXT PRIMARY KEY, template_id TEXT NOT NULL,
    name TEXT NOT NULL, stage_order INTEGER NOT NULL,
    FOREIGN KEY(template_id) REFERENCES pipeline_templates(template_id) ON DELETE CASCADE,
    UNIQUE(template_id, name), UNIQUE(template_id, stage_order)
);
CREATE TABLE template_jobs (
    template_job_id TEXT PRIMARY KEY, template_stage_id TEXT NOT NULL,
    agent_type TEXT NOT NULL CHECK(agent_type IN ('planner', 'dev', 'tester', 'verifier', 'mock')),
    name TEXT NOT NULL, prompt_template TEXT, command_template TEXT,
    max_iterations INTEGER DEFAULT 50, timeout_seconds INTEGER DEFAULT 300,
    artifact_strategy JSON, job_multiplier JSON, retry_strategy JSON,
    backend TEXT NOT NULL DEFAULT 'ollama', model TEXT,
    vendor TEXT DEFAULT 'local-ollama', chain_id TEXT,
    FOREIGN KEY(template_stage_id) REFERENCES template_stages(template_stage_id) ON DELETE CASCADE
);
CREATE TABLE template_job_dependencies (
    template_job_id TEXT NOT NULL, depends_on_template_job_id TEXT NOT NULL,
    dependency_type TEXT DEFAULT 'success' CHECK(dependency_type IN ('success', 'failure', 'always', 'completed')),
    PRIMARY KEY(template_job_id, depends_on_template_job_id),
    FOREIGN KEY(template_job_id) REFERENCES template_jobs(template_job_id) ON DELETE CASCADE,
    FOREIGN KEY(depends_on_template_job_id) REFERENCES template_jobs(template_job_id) ON DELETE CASCADE
);
CREATE TABLE pipeline_schedules (
    schedule_id TEXT PRIMARY KEY, template_id TEXT NOT NULL,
    name TEXT NOT NULL, cron_expr TEXT NOT NULL, prompt TEXT NOT NULL,
    workspace_path TEXT NOT NULL DEFAULT '/workspace',
    enabled INTEGER NOT NULL DEFAULT 1, last_fired_at TEXT, next_fire_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE pipelines (
    pipeline_id TEXT PRIMARY KEY, template_id TEXT, original_prompt TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
    metadata JSON, schedule_id TEXT
);
CREATE TABLE stages (
    stage_id TEXT PRIMARY KEY, pipeline_id TEXT NOT NULL, name TEXT NOT NULL,
    stage_order INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(pipeline_id) REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    UNIQUE(pipeline_id, name), UNIQUE(pipeline_id, stage_order)
);
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY, pipeline_id TEXT NOT NULL, stage_id TEXT NOT NULL,
    agent_type TEXT NOT NULL CHECK(agent_type IN ('planner', 'dev', 'tester', 'verifier', 'mock')),
    prompt TEXT NOT NULL, command TEXT,
    max_iterations INTEGER NOT NULL, timeout_seconds INTEGER NOT NULL,
    allowed_paths TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'skipped', 'waiting')),
    iteration INTEGER DEFAULT 0, started_at TEXT, completed_at TEXT, termination_reason TEXT,
    parent_job_id TEXT, regression_context JSON,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 100,
    job_output TEXT, artifact_strategy JSON, template_job_id TEXT, retry_strategy JSON,
    original_prompt TEXT, backend TEXT NOT NULL DEFAULT 'ollama', model TEXT,
    vendor TEXT NOT NULL DEFAULT 'local-ollama',
    FOREIGN KEY(pipeline_id) REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
    FOREIGN KEY(stage_id) REFERENCES stages(stage_id) ON DELETE CASCADE,
    FOREIGN KEY(parent_job_id) REFERENCES jobs(job_id) ON DELETE SET NULL
);
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('file', 'model_output', 'error_context', 'test_results', 'verification_report')),
    name TEXT NOT NULL, description TEXT, file_path TEXT, content TEXT,
    content_hash TEXT, size_bytes INTEGER, metadata JSON, created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE TABLE artifact_consumption (
    job_id TEXT NOT NULL, artifact_id TEXT NOT NULL, consumed_at TEXT NOT NULL,
    PRIMARY KEY(job_id, artifact_id),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id) ON DELETE CASCADE
);
"""


def _fresh_db() -> sqlite3.Connection:
    """In-memory DB pre-loaded with the pre-migration schema (no 'command' agent_type)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_PRE_MIGRATION_DDL)
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    """Insert one template + stage + job so we can verify data is preserved."""
    conn.execute(
        "INSERT INTO pipeline_templates (template_id, name, created_at, updated_at) "
        "VALUES ('t1', 'Test', datetime('now'), datetime('now'))"
    )
    conn.execute(
        "INSERT INTO template_stages (template_stage_id, template_id, name, stage_order) "
        "VALUES ('s1', 't1', 'dev', 1)"
    )
    conn.execute(
        "INSERT INTO template_jobs "
        "(template_job_id, template_stage_id, agent_type, name) "
        "VALUES ('j1', 's1', 'dev', 'Dev job')"
    )
    conn.commit()


_VIEWS_SQL = [
    # active_pipelines
    """CREATE VIEW active_pipelines AS
    SELECT p.pipeline_id, p.original_prompt, p.status, s.name as current_stage,
        s.stage_order, COUNT(DISTINCT j.job_id) as total_jobs,
        SUM(CASE WHEN j.status = 'completed' THEN 1 ELSE 0 END) as completed_jobs,
        p.created_at
    FROM pipelines p
    LEFT JOIN stages s ON p.pipeline_id = s.pipeline_id AND s.status = 'running'
    LEFT JOIN jobs j ON p.pipeline_id = j.pipeline_id
    WHERE p.status IN ('pending', 'running')
    GROUP BY p.pipeline_id""",
    # job_summary
    """CREATE VIEW job_summary AS
    SELECT j.job_id, j.pipeline_id, s.name as stage_name, j.agent_type, j.prompt,
        j.status, j.iteration, j.max_iterations,
        COUNT(DISTINCT a.artifact_id) as artifacts_produced,
        j.parent_job_id, j.started_at, j.completed_at
    FROM jobs j
    JOIN stages s ON j.stage_id = s.stage_id
    LEFT JOIN artifacts a ON j.job_id = a.job_id
    GROUP BY j.job_id""",
    # artifact_flow
    """CREATE VIEW artifact_flow AS
    SELECT a.artifact_id, a.name as artifact_name, a.type as artifact_type,
        producer.job_id as produced_by_job, producer.agent_type as produced_by_agent,
        consumer.job_id as consumed_by_job, consumer.agent_type as consumed_by_agent,
        ac.consumed_at
    FROM artifacts a
    JOIN jobs producer ON a.job_id = producer.job_id
    LEFT JOIN artifact_consumption ac ON a.artifact_id = ac.artifact_id
    LEFT JOIN jobs consumer ON ac.job_id = consumer.job_id""",
    # regression_chains
    """CREATE VIEW regression_chains AS
    WITH RECURSIVE chain AS (
        SELECT job_id, prompt, parent_job_id, 0 as depth, job_id as root_job_id
        FROM jobs WHERE parent_job_id IS NULL
        UNION ALL
        SELECT j.job_id, j.prompt, j.parent_job_id, c.depth + 1, c.root_job_id
        FROM jobs j JOIN chain c ON j.parent_job_id = c.job_id
    ) SELECT * FROM chain""",
]


def _run_migration(conn: sqlite3.Connection) -> None:
    """Apply the migration logic directly (without the CLI wrapper)."""
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")

    conn.execute("""
        CREATE TABLE template_jobs_new (
            template_job_id TEXT PRIMARY KEY,
            template_stage_id TEXT NOT NULL,
            agent_type TEXT NOT NULL CHECK(agent_type IN
                ('planner', 'dev', 'tester', 'verifier', 'mock', 'command')),
            name TEXT NOT NULL,
            prompt_template TEXT,
            command_template TEXT,
            max_iterations INTEGER DEFAULT 50,
            timeout_seconds INTEGER DEFAULT 300,
            artifact_strategy JSON,
            job_multiplier JSON,
            retry_strategy JSON,
            backend TEXT NOT NULL DEFAULT 'ollama',
            model TEXT,
            vendor TEXT DEFAULT 'local-ollama',
            chain_id TEXT,
            FOREIGN KEY(template_stage_id)
                REFERENCES template_stages(template_stage_id) ON DELETE CASCADE
        )
    """)
    conn.execute("INSERT INTO template_jobs_new SELECT * FROM template_jobs")
    conn.execute("DROP TABLE template_jobs")
    conn.execute("ALTER TABLE template_jobs_new RENAME TO template_jobs")

    # Drop views that reference jobs before we rename the table
    for v in ("active_pipelines", "job_summary", "artifact_flow", "regression_chains"):
        conn.execute(f"DROP VIEW IF EXISTS {v}")

    conn.execute("""
        CREATE TABLE jobs_new (
            job_id TEXT PRIMARY KEY,
            pipeline_id TEXT NOT NULL,
            stage_id TEXT NOT NULL,
            agent_type TEXT NOT NULL CHECK(agent_type IN
                ('planner', 'dev', 'tester', 'verifier', 'mock', 'command')),
            prompt TEXT NOT NULL,
            command TEXT,
            max_iterations INTEGER NOT NULL,
            timeout_seconds INTEGER NOT NULL,
            allowed_paths TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('pending', 'running', 'completed', 'failed', 'cancelled', 'skipped', 'waiting')),
            iteration INTEGER DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            termination_reason TEXT,
            parent_job_id TEXT,
            regression_context JSON,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 100,
            job_output TEXT,
            artifact_strategy JSON,
            template_job_id TEXT,
            retry_strategy JSON,
            original_prompt TEXT,
            backend TEXT NOT NULL DEFAULT 'ollama',
            model TEXT,
            vendor TEXT NOT NULL DEFAULT 'local-ollama',
            FOREIGN KEY(pipeline_id) REFERENCES pipelines(pipeline_id) ON DELETE CASCADE,
            FOREIGN KEY(stage_id) REFERENCES stages(stage_id) ON DELETE CASCADE,
            FOREIGN KEY(parent_job_id) REFERENCES jobs_new(job_id) ON DELETE SET NULL
        )
    """)
    conn.execute("INSERT INTO jobs_new SELECT * FROM jobs")
    conn.execute("DROP TABLE jobs")
    conn.execute("ALTER TABLE jobs_new RENAME TO jobs")

    # Recreate views
    for sql in _VIEWS_SQL:
        conn.execute(sql)

    conn.execute("COMMIT")
    conn.execute("PRAGMA foreign_keys = ON")


# ── tests ─────────────────────────────────────────────────────────────────────


def test_command_type_rejected_before_migration():
    conn = _fresh_db()
    _seed(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO template_jobs "
            "(template_job_id, template_stage_id, agent_type, name) "
            "VALUES ('j2', 's1', 'command', 'Cmd job')"
        )


def test_command_type_accepted_in_template_jobs_after_migration():
    conn = _fresh_db()
    _seed(conn)
    _run_migration(conn)
    conn.execute(
        "INSERT INTO template_jobs "
        "(template_job_id, template_stage_id, agent_type, name) "
        "VALUES ('j2', 's1', 'command', 'Cmd job')"
    )
    row = conn.execute(
        "SELECT agent_type FROM template_jobs WHERE template_job_id = 'j2'"
    ).fetchone()
    assert row["agent_type"] == "command"


def test_existing_data_preserved_after_migration():
    conn = _fresh_db()
    _seed(conn)
    _run_migration(conn)
    row = conn.execute(
        "SELECT agent_type, name FROM template_jobs WHERE template_job_id = 'j1'"
    ).fetchone()
    assert row["agent_type"] == "dev"
    assert row["name"] == "Dev job"


def test_invalid_type_still_rejected_after_migration():
    conn = _fresh_db()
    _seed(conn)
    _run_migration(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO template_jobs "
            "(template_job_id, template_stage_id, agent_type, name) "
            "VALUES ('j3', 's1', 'wizard', 'Bad job')"
        )


def test_command_type_accepted_in_jobs_after_migration():
    conn = _fresh_db()
    _seed(conn)
    _run_migration(conn)
    # Insert the pipeline/stage required by jobs FK
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, original_prompt, status, created_at, updated_at) "
        "VALUES ('p1', 'test', 'running', datetime('now'), datetime('now'))"
    )
    conn.execute(
        "INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at) "
        "VALUES ('st1', 'p1', 'dev', 1, 'running', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO jobs (job_id, pipeline_id, stage_id, agent_type, prompt, "
        "max_iterations, timeout_seconds, allowed_paths, status, created_at, updated_at) "
        "VALUES ('job1', 'p1', 'st1', 'command', 'run it', 10, 60, '[]', 'pending', "
        "datetime('now'), datetime('now'))"
    )
    row = conn.execute("SELECT agent_type FROM jobs WHERE job_id = 'job1'").fetchone()
    assert row["agent_type"] == "command"
