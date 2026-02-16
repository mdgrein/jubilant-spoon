#!/usr/bin/env python3
"""
Test the retry logic by creating a mock job that will fail.
Verify it gets retried and then eventually fails permanently.
"""
import sqlite3
import uuid
from datetime import datetime, timezone

db_path = "clowder.db"

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

def timestamp():
    return datetime.now(timezone.utc).isoformat()

# Create a test pipeline
pipeline_id = str(uuid.uuid4())
conn.execute("""
    INSERT INTO pipelines (
        pipeline_id, template_id, original_prompt, status, created_at, updated_at
    ) VALUES (?, 'template-test-retry', 'Test retry logic', 'pending', ?, ?)
""", (pipeline_id, timestamp(), timestamp()))

# Create a test stage
stage_id = str(uuid.uuid4())
conn.execute("""
    INSERT INTO stages (
        stage_id, pipeline_id, name, stage_order, status, created_at
    ) VALUES (?, ?, 'test', 1, 'pending', ?)
""", (stage_id, pipeline_id, timestamp()))

# Create a job that will fail (using a command that doesn't exist)
job_id = str(uuid.uuid4())
conn.execute("""
    INSERT INTO jobs (
        job_id, pipeline_id, stage_id, agent_type, prompt,
        command, max_iterations, timeout_seconds, allowed_paths,
        status, retry_count, max_retries, created_at, updated_at
    ) VALUES (?, ?, ?, 'mock', 'Test job that will fail',
              'exit 1', 50, 300, '["./"]',
              'pending', 0, 2, ?, ?)
""", (job_id, pipeline_id, stage_id, timestamp(), timestamp()))

# Create a dependent job that should be skipped when first job fails
dep_job_id = str(uuid.uuid4())
conn.execute("""
    INSERT INTO jobs (
        job_id, pipeline_id, stage_id, agent_type, prompt,
        command, max_iterations, timeout_seconds, allowed_paths,
        status, retry_count, max_retries, created_at, updated_at
    ) VALUES (?, ?, ?, 'mock', 'Dependent job',
              'echo "This should be skipped"', 50, 300, '["./"]',
              'pending', 0, 2, ?, ?)
""", (dep_job_id, pipeline_id, stage_id, timestamp(), timestamp()))

# Create dependency
conn.execute("""
    INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type)
    VALUES (?, ?, 'success')
""", (dep_job_id, job_id))

conn.commit()

print(f"Created test pipeline: {pipeline_id}")
print(f"Created failing job: {job_id}")
print(f"Created dependent job: {dep_job_id}")
print("\nTo test:")
print("1. Start the server (python server/main.py)")
print("2. Watch the logs - you should see:")
print("   - Job fails on first attempt")
print("   - Job retries (attempt 2/3)")
print("   - Job retries again (attempt 3/3)")
print("   - Job fails permanently")
print("   - Dependent job gets skipped")
print("   - Pipeline marked as failed")
print(f"\n3. Check results with:")
print(f"   python -c \"import sqlite3; conn = sqlite3.connect('clowder.db'); conn.row_factory = sqlite3.Row; jobs = conn.execute('SELECT job_id, status, retry_count, termination_reason FROM jobs WHERE pipeline_id = \\'{pipeline_id}\\'').fetchall(); [print(dict(j)) for j in jobs]\"")

conn.close()
