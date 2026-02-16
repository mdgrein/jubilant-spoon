#!/usr/bin/env python3
"""
Test that job output is now being captured and stored.
This will create a job with a simple mock command that produces output.
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
    ) VALUES (?, 'template-test-output', 'Test job output capture', 'pending', ?, ?)
""", (pipeline_id, timestamp(), timestamp()))

# Create a test stage
stage_id = str(uuid.uuid4())
conn.execute("""
    INSERT INTO stages (
        stage_id, pipeline_id, name, stage_order, status, created_at
    ) VALUES (?, ?, 'test', 1, 'pending', ?)
""", (stage_id, pipeline_id, timestamp()))

# Create a job using mock_agent.py that will output several lines
job_id = str(uuid.uuid4())
conn.execute("""
    INSERT INTO jobs (
        job_id, pipeline_id, stage_id, agent_type, prompt,
        command, max_iterations, timeout_seconds, allowed_paths,
        status, retry_count, max_retries, created_at, updated_at
    ) VALUES (?, ?, ?, 'mock', 'Test job with output',
              'python agents/mock_agent.py --agent-type tester --duration 0.5 --failure-rate 0.0 --prompt "Testing output capture"',
              50, 300, '["./"]',
              'pending', 0, 0, ?, ?)
""", (job_id, pipeline_id, stage_id, timestamp(), timestamp()))

conn.commit()

print(f"Created test pipeline: {pipeline_id}")
print(f"Created test job: {job_id}")
print("\nTo test:")
print("1. Start the server (if not running): python server/main.py")
print("2. Wait ~10 seconds for the job to run")
print("3. Check the job output:")
print(f"\n   python -c \"import sqlite3; conn = sqlite3.connect('clowder.db'); conn.row_factory = sqlite3.Row; job = conn.execute('SELECT agent_type, status, job_output FROM jobs WHERE job_id = \\'{job_id}\\'').fetchone(); print('Status:', job['status']); print('\\nOutput:'); print(job['job_output'])\"")
print("\n4. Or view in the client UI by selecting the job in the tree")

conn.close()
