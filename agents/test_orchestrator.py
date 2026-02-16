"""
Test orchestrator with a simple pipeline.
"""

import time
import threading
from pathlib import Path
from db import ClowderDB
from templates import TemplateManager
from orchestrator import Orchestrator


def main():
    # Initialize database
    db_path = Path("test_orchestrator.db")
    if db_path.exists():
        db_path.unlink()

    print("Initializing database...")
    db = ClowderDB(str(db_path))

    # Load schemas
    schema_pipelines = Path("schema_pipelines.sql").read_text()
    db.conn.executescript(schema_pipelines)

    # Load templates
    seed_templates = Path("seed_templates.sql").read_text()
    db.conn.executescript(seed_templates)
    db.conn.commit()

    print("[OK] Database initialized\n")

    # Create a simple pipeline
    print("Creating test pipeline...")
    tm = TemplateManager(db)

    pipeline_id = tm.instantiate_template(
        template_id='template-dev-test',  # Dev + Test
        original_prompt='Test orchestrator with simple task',
        workspace_path='D:/workspace',
    )

    print(f"[OK] Created pipeline: {pipeline_id[:8]}...\n")

    # Show pipeline structure
    jobs = db.conn.execute("""
        SELECT j.job_id, s.name as stage, j.agent_type, j.status
        FROM jobs j
        JOIN stages s ON j.stage_id = s.stage_id
        WHERE j.pipeline_id = ?
        ORDER BY s.stage_order
    """, (pipeline_id,)).fetchall()

    print("Pipeline structure:")
    for job in jobs:
        print(f"  [{job['stage']}] {job['agent_type']} - {job['status']}")

    # Show dependencies
    deps = db.conn.execute("""
        SELECT
            j1.agent_type as job,
            j2.agent_type as depends_on
        FROM job_dependencies jd
        JOIN jobs j1 ON jd.job_id = j1.job_id
        JOIN jobs j2 ON jd.depends_on_job_id = j2.job_id
        WHERE j1.pipeline_id = ?
    """, (pipeline_id,)).fetchall()

    print("\nDependencies:")
    for dep in deps:
        print(f"  {dep['job']} -> depends on -> {dep['depends_on']}")

    db.close()

    # Start orchestrator in background
    print("\n" + "="*60)
    print("STARTING ORCHESTRATOR")
    print("="*60 + "\n")

    def run_orchestrator():
        orch = Orchestrator(db_path=str(db_path), poll_interval=2)
        orch.start()

    orch_thread = threading.Thread(target=run_orchestrator, daemon=True)
    orch_thread.start()

    # Monitor pipeline for 30 seconds
    db = ClowderDB(str(db_path))

    for i in range(15):  # 30 seconds total
        time.sleep(2)

        # Check pipeline status
        pipeline = db.conn.execute("""
            SELECT status FROM pipelines WHERE pipeline_id = ?
        """, (pipeline_id,)).fetchone()

        # Check job statuses
        jobs = db.conn.execute("""
            SELECT j.agent_type, j.status, s.name as stage
            FROM jobs j
            JOIN stages s ON j.stage_id = s.stage_id
            WHERE j.pipeline_id = ?
            ORDER BY s.stage_order
        """, (pipeline_id,)).fetchall()

        print(f"\n[{i*2}s] Pipeline status: {pipeline['status']}")
        for job in jobs:
            print(f"  [{job['stage']}] {job['agent_type']}: {job['status']}")

        if pipeline['status'] in ('completed', 'failed'):
            print(f"\n[OK] Pipeline {pipeline['status']}!")
            break

    db.close()


if __name__ == "__main__":
    main()
