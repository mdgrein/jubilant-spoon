#!/usr/bin/env python3
"""
Quick test script for simple 3-stage pipeline.
Starts a pipeline and monitors its progress.
"""

import requests
import time
import json

SERVER_URL = "http://localhost:8000"

def start_pipeline(prompt: str):
    """Start the simple pipeline."""
    response = requests.post(
        f"{SERVER_URL}/pipelines/simple-plan-code-verify/start",
        json={"prompt": prompt, "workspace_path": "./workspace"},
        timeout=5
    )
    response.raise_for_status()
    return response.json()

def get_pipeline_status(pipeline_id: str):
    """Get pipeline and job status."""
    response = requests.get(
        f"{SERVER_URL}/pipelines/{pipeline_id}",
        timeout=5
    )
    response.raise_for_status()
    return response.json()

def get_artifacts(pipeline_id: str):
    """Get all artifacts for a pipeline."""
    import sqlite3
    conn = sqlite3.connect("clowder.db")
    conn.row_factory = sqlite3.Row

    artifacts = conn.execute("""
        SELECT a.*, j.agent_type
        FROM artifacts a
        JOIN jobs j ON a.job_id = j.job_id
        WHERE j.pipeline_id = ?
        ORDER BY a.created_at
    """, (pipeline_id,)).fetchall()

    conn.close()
    return [dict(a) for a in artifacts]

def main():
    prompt = input("Enter task prompt (or press Enter for default): ").strip()
    if not prompt:
        prompt = "Write a Python function to calculate fibonacci numbers"

    print(f"\n🚀 Starting pipeline with prompt: '{prompt}'")

    # Start pipeline
    try:
        result = start_pipeline(prompt)
        pipeline_id = result['pipeline_id']
        print(f"✓ Pipeline started: {pipeline_id[:8]}")
    except requests.exceptions.ConnectionError:
        print("❌ Error: Server not running. Start with: python server/main.py")
        return
    except Exception as e:
        print(f"❌ Error starting pipeline: {e}")
        return

    # Monitor progress
    print("\n⏳ Monitoring progress (Ctrl+C to stop)...")
    last_status = {}

    try:
        while True:
            time.sleep(2)

            status = get_pipeline_status(pipeline_id)
            pipeline_status = status['pipeline']['status']

            # Show job updates
            for job in status['jobs']:
                job_key = f"{job['agent_type']}:{job['status']}"
                if job_key != last_status.get(job['job_id']):
                    icon = "⏳" if job['status'] == 'running' else "✓" if job['status'] == 'completed' else "❌" if job['status'] == 'failed' else "⏸"
                    print(f"  {icon} {job['agent_type']}: {job['status']}")
                    last_status[job['job_id']] = job_key

            # Check if done
            if pipeline_status in ['completed', 'failed']:
                print(f"\n{'✓' if pipeline_status == 'completed' else '❌'} Pipeline {pipeline_status}")
                break

    except KeyboardInterrupt:
        print("\n\n⏸ Monitoring stopped")

    # Show artifacts
    print("\n📦 Artifacts:")
    artifacts = get_artifacts(pipeline_id)
    for artifact in artifacts:
        print(f"\n--- {artifact['agent_type']}: {artifact['name']} ---")
        content = artifact['content'] or "(no content)"
        # Show first 500 chars
        if len(content) > 500:
            print(content[:500] + "\n... (truncated)")
        else:
            print(content)

    print(f"\n✓ Done! Pipeline ID: {pipeline_id}")
    print(f"\nView full results:")
    print(f"  python -c \"import sqlite3; conn = sqlite3.connect('clowder.db'); [print(dict(a)) for a in conn.execute('SELECT * FROM artifacts WHERE job_id IN (SELECT job_id FROM jobs WHERE pipeline_id = \\\"{pipeline_id}\\\")')]; conn.close()\"")

if __name__ == "__main__":
    main()
