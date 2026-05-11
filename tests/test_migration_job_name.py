"""Tests for migration 008: add name and chain_id to jobs table."""

import importlib.util as _ilu
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))


# Pre-migration schema: jobs table without name/chain_id (matches post-007 state)
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
    agent_type TEXT NOT NULL CHECK(agent_type IN ('planner', 'dev', 'tester', 'verifier', 'mock', 'command')),
    name TEXT NOT NULL, prompt_template TEXT, command_template TEXT,
    max_iterations INTEGER DEFAULT 50, timeout_seconds INTEGER DEFAULT 300,
    artifact_strategy JSON, job_multiplier JSON, retry_strategy JSON,
    backend TEXT NOT NULL DEFAULT 'ollama', model TEXT,
    vendor TEXT DEFAULT 'local-ollama', chain_id TEXT,
    FOREIGN KEY(template_stage_id) REFERENCES template_stages(template_stage_id) ON DELETE CASCADE
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
    agent_type TEXT NOT NULL CHECK(agent_type IN ('planner', 'dev', 'tester', 'verifier', 'mock', 'command')),
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
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_PRE_MIGRATION_DDL)
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    """Insert pipeline + stage + job so we can verify data is preserved."""
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, original_prompt, status, created_at, updated_at) "
        "VALUES ('p1', 'test prompt', 'pending', datetime('now'), datetime('now'))"
    )
    conn.execute(
        "INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at) "
        "VALUES ('s1', 'p1', 'dev', 1, 'pending', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO jobs (job_id, pipeline_id, stage_id, agent_type, prompt, "
        "max_iterations, timeout_seconds, allowed_paths, status, created_at, updated_at) "
        "VALUES ('j1', 'p1', 's1', 'dev', 'do work', 50, 300, '[]', 'pending', "
        "datetime('now'), datetime('now'))"
    )
    conn.commit()


def _load_migration():
    spec = _ilu.spec_from_file_location(
        "migration_008",
        Path(__file__).parent.parent
        / "pipeline"
        / "migrations"
        / "008_add_job_name.py",
    )
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_name_column_absent_before_migration():
    conn = _fresh_db()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    assert "name" not in cols
    assert "chain_id" not in cols


def test_name_and_chain_id_present_after_migration():
    conn = _fresh_db()
    _seed(conn)
    _load_migration().run(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    assert "name" in cols
    assert "chain_id" in cols


def test_existing_data_preserved_after_migration():
    conn = _fresh_db()
    _seed(conn)
    _load_migration().run(conn)
    row = conn.execute(
        "SELECT agent_type, prompt, name, chain_id FROM jobs WHERE job_id = 'j1'"
    ).fetchone()
    assert row["agent_type"] == "dev"
    assert row["prompt"] == "do work"
    assert row["name"] == ""  # default for migrated rows
    assert row["chain_id"] is None  # default for migrated rows


def test_name_column_accepts_value_after_migration():
    conn = _fresh_db()
    _seed(conn)
    _load_migration().run(conn)
    conn.execute(
        "UPDATE jobs SET name = 'fibonacci tester', chain_id = 'fibonacci' WHERE job_id = 'j1'"
    )
    row = conn.execute("SELECT name, chain_id FROM jobs WHERE job_id = 'j1'").fetchone()
    assert row["name"] == "fibonacci tester"
    assert row["chain_id"] == "fibonacci"


def test_new_job_with_name_and_chain_id_after_migration():
    conn = _fresh_db()
    _seed(conn)
    _load_migration().run(conn)
    conn.execute(
        "INSERT INTO jobs (job_id, pipeline_id, stage_id, agent_type, name, prompt, "
        "max_iterations, timeout_seconds, allowed_paths, status, created_at, updated_at, chain_id) "
        "VALUES ('j2', 'p1', 's1', 'tester', 'fib tester', 'run tests', 50, 300, '[]', 'pending', "
        "datetime('now'), datetime('now'), 'fibonacci')"
    )
    row = conn.execute("SELECT name, chain_id FROM jobs WHERE job_id = 'j2'").fetchone()
    assert row["name"] == "fib tester"
    assert row["chain_id"] == "fibonacci"
