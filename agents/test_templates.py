"""
Test pipeline templates.
Run: python agents/test_templates.py
"""

from pathlib import Path
from db import ClowderDB
from templates import TemplateManager


def main():
    # Initialize database
    db_path = Path("test_pipelines.db")
    if db_path.exists():
        db_path.unlink()

    print("Initializing database...")
    db = ClowderDB(str(db_path))

    # Load schemas
    print("Loading schemas...")
    schema_pipelines = Path("schema_pipelines.sql").read_text()
    db.conn.executescript(schema_pipelines)
    db.conn.commit()

    # Load seed templates
    print("Loading template seeds...")
    seed_templates = Path("seed_templates.sql").read_text()
    db.conn.executescript(seed_templates)
    db.conn.commit()

    print("[OK] Database initialized\n")

    # List templates
    tm = TemplateManager(db)
    templates = tm.list_templates()

    print("=" * 60)
    print("AVAILABLE TEMPLATES")
    print("=" * 60)
    for t in templates:
        print(f"\n{t['name']}")
        print(f"  ID: {t['template_id']}")
        print(f"  Description: {t['description']}")
        print(f"  Stages: {t['stage_count']}, Jobs: {t['job_count']}")

    # Get full template details
    print("\n" + "=" * 60)
    print("TEMPLATE DETAILS: Full Workflow")
    print("=" * 60)
    full_template = tm.get_template('template-full')
    for stage in full_template['stages']:
        print(f"\nStage {stage['stage_order']}: {stage['name']}")
        for job in stage['jobs']:
            print(f"  - {job['name']} ({job['agent_type']})")
            print(f"    Prompt: {job['prompt_template'][:60]}...")

    # Instantiate template
    print("\n" + "=" * 60)
    print("CREATING PIPELINE FROM TEMPLATE")
    print("=" * 60)
    pipeline_id = tm.instantiate_template(
        template_id='template-full',
        original_prompt='Add user authentication with JWT tokens',
        workspace_path='D:/workspace',
    )
    print(f"[OK] Created pipeline: {pipeline_id}")

    # Query pipeline
    pipeline = db.conn.execute("""
        SELECT * FROM pipelines WHERE pipeline_id = ?
    """, (pipeline_id,)).fetchone()
    print(f"  Original prompt: {pipeline['original_prompt']}")
    print(f"  Template: {pipeline['template_id']}")
    print(f"  Status: {pipeline['status']}")

    # Query stages
    stages = db.conn.execute("""
        SELECT * FROM stages WHERE pipeline_id = ? ORDER BY stage_order
    """, (pipeline_id,)).fetchall()
    print(f"\n  Stages created: {len(stages)}")
    for stage in stages:
        print(f"    {stage['stage_order']}. {stage['name']}")

    # Query jobs
    jobs = db.conn.execute("""
        SELECT j.job_id, s.name as stage_name, j.agent_type, j.prompt
        FROM jobs j
        JOIN stages s ON j.stage_id = s.stage_id
        WHERE j.pipeline_id = ?
        ORDER BY s.stage_order
    """, (pipeline_id,)).fetchall()
    print(f"\n  Jobs created: {len(jobs)}")
    for job in jobs:
        print(f"    [{job['stage_name']}] {job['agent_type']}")
        print(f"      Prompt: {job['prompt'][:70]}...")

    # Test customization: Exclude test stage
    print("\n" + "=" * 60)
    print("CREATING CUSTOMIZED PIPELINE (No Testing)")
    print("=" * 60)

    # Get template to find stage IDs
    template = tm.get_template('template-full')
    test_stage = [s for s in template['stages'] if s['name'] == 'test'][0]

    custom_pipeline_id = tm.instantiate_template(
        template_id='template-full',
        original_prompt='Add user authentication',
        workspace_path='D:/workspace',
        excluded_stage_ids=[test_stage['template_stage_id']],
    )
    print(f"[OK] Created customized pipeline: {custom_pipeline_id}")

    # Query customized pipeline stages
    custom_stages = db.conn.execute("""
        SELECT name FROM stages WHERE pipeline_id = ? ORDER BY stage_order
    """, (custom_pipeline_id,)).fetchall()
    print(f"  Stages: {', '.join([s['name'] for s in custom_stages])}")

    db.close()
    print(f"\n[OK] Database closed")
    print(f"\nInspect: python -c \"from agents.db import ClowderDB; ...\"")


if __name__ == "__main__":
    main()
