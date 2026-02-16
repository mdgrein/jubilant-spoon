#!/usr/bin/env python3
"""
Simple agent for plan-code-verify pipeline.
Calls local Ollama model with minimal prompt.

This is a SIMPLIFIED agent - no full harness, just model call + output.
"""

import argparse
import json
import sys
import sqlite3
from pathlib import Path


def call_ollama(prompt: str, model: str = "llama3.2:latest") -> str:
    """
    Call local Ollama model.

    For now, uses subprocess to call ollama CLI.
    In production, would use requests to call Ollama API.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True,
            text=True,
            timeout=120,
            check=True
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "ERROR: Model timeout"
    except subprocess.CalledProcessError as e:
        return f"ERROR: Model failed: {e.stderr}"
    except FileNotFoundError:
        return "ERROR: Ollama not found. Install from https://ollama.ai"


def get_job_info(job_id: str) -> dict:
    """Get job details from database."""
    db_path = Path(__file__).parent.parent / "clowder.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    job = conn.execute("""
        SELECT j.*, p.original_prompt
        FROM jobs j
        JOIN pipelines p ON j.pipeline_id = p.pipeline_id
        WHERE j.job_id = ?
    """, (job_id,)).fetchone()

    conn.close()

    if not job:
        raise ValueError(f"Job {job_id} not found")

    return dict(job)


def get_dependency_artifacts(job_id: str) -> list[dict]:
    """Get artifacts from jobs this one depends on."""
    db_path = Path(__file__).parent.parent / "clowder.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    artifacts = conn.execute("""
        SELECT a.*
        FROM artifacts a
        JOIN jobs dep_job ON a.job_id = dep_job.job_id
        JOIN job_dependencies jd ON jd.depends_on_job_id = dep_job.job_id
        WHERE jd.job_id = ?
        ORDER BY a.created_at
    """, (job_id,)).fetchall()

    conn.close()

    return [dict(a) for a in artifacts]


def main():
    parser = argparse.ArgumentParser(description="Simple agent for plan-code-verify pipeline")
    parser.add_argument("--agent-type", required=True, help="Agent type (planner, script-kiddie, verifier)")
    parser.add_argument("--job-id", required=True, help="Job ID")
    parser.add_argument("--model", default="qwen2.5-coder:7b", help="Ollama model to use")

    args = parser.parse_args()

    # Get job info
    try:
        job = get_job_info(args.job_id)
    except Exception as e:
        print(f"ERROR: Failed to get job info: {e}", file=sys.stderr)
        sys.exit(1)

    # Get artifacts from dependencies
    artifacts = get_dependency_artifacts(args.job_id)

    # Build context from artifacts
    artifact_context = ""
    if artifacts:
        artifact_context = "\n\n=== Context from previous jobs ===\n"
        for artifact in artifacts:
            artifact_context += f"\n--- {artifact['name']} ---\n"
            if artifact['content']:
                artifact_context += artifact['content']
            elif artifact['file_path']:
                artifact_context += f"File: {artifact['file_path']}"
            artifact_context += "\n"

    # Build final prompt
    prompt = job['prompt']
    if artifact_context:
        prompt += artifact_context

    print(f"[{args.agent_type.upper()}] Starting job {args.job_id[:8]}", file=sys.stderr)
    print(f"[{args.agent_type.upper()}] Calling model: {args.model}", file=sys.stderr)

    # Call model
    response = call_ollama(prompt, model=args.model)

    # Output response to stdout (will be captured as artifact)
    print(response)

    print(f"[{args.agent_type.upper()}] Complete", file=sys.stderr)


if __name__ == "__main__":
    main()
