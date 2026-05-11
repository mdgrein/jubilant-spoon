"""
Migration 007: Add 'command' as a valid agent_type in template_jobs and jobs.

SQLite does not support ALTER COLUMN, so we recreate both tables with the
updated CHECK constraint.  Views that reference 'jobs' must be dropped before
the table rename and recreated afterwards.
"""

import sqlite3
from pathlib import Path


_VIEWS = [
    (
        "active_pipelines",
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
    ),
    (
        "job_summary",
        """CREATE VIEW job_summary AS
    SELECT j.job_id, j.pipeline_id, s.name as stage_name, j.agent_type, j.prompt,
        j.status, j.iteration, j.max_iterations,
        COUNT(DISTINCT a.artifact_id) as artifacts_produced,
        j.parent_job_id, j.started_at, j.completed_at
    FROM jobs j
    JOIN stages s ON j.stage_id = s.stage_id
    LEFT JOIN artifacts a ON j.job_id = a.job_id
    GROUP BY j.job_id""",
    ),
    (
        "artifact_flow",
        """CREATE VIEW artifact_flow AS
    SELECT a.artifact_id, a.name as artifact_name, a.type as artifact_type,
        producer.job_id as produced_by_job, producer.agent_type as produced_by_agent,
        consumer.job_id as consumed_by_job, consumer.agent_type as consumed_by_agent,
        ac.consumed_at
    FROM artifacts a
    JOIN jobs producer ON a.job_id = producer.job_id
    LEFT JOIN artifact_consumption ac ON a.artifact_id = ac.artifact_id
    LEFT JOIN jobs consumer ON ac.job_id = consumer.job_id""",
    ),
    (
        "regression_chains",
        """CREATE VIEW regression_chains AS
    WITH RECURSIVE chain AS (
        SELECT job_id, prompt, parent_job_id, 0 as depth, job_id as root_job_id
        FROM jobs WHERE parent_job_id IS NULL
        UNION ALL
        SELECT j.job_id, j.prompt, j.parent_job_id, c.depth + 1, c.root_job_id
        FROM jobs j JOIN chain c ON j.parent_job_id = c.job_id
    ) SELECT * FROM chain""",
    ),
]


def run(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")

    # --- template_jobs ---
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

    # Drop views that reference jobs before we rename that table
    for view_name, _ in _VIEWS:
        conn.execute(f"DROP VIEW IF EXISTS {view_name}")

    # --- jobs ---
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
    for _, view_sql in _VIEWS:
        conn.execute(view_sql)

    conn.execute("COMMIT")
    conn.execute("PRAGMA foreign_keys = ON")
    print("Migration 007 applied: 'command' added to agent_type CHECK constraint.")


if __name__ == "__main__":
    db_path = Path(__file__).parent.parent / "clowder.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    run(conn)
    conn.close()
