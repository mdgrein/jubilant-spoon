#!/usr/bin/env python3
"""
Parent-child variant of test_simple_pipeline.py.

Same four-task TDD pipeline, using the parent-child job model with
verifier-as-loop-controller (runtime dev spawning).

Structure per task:
    coordinator  (mock, trivial exit 0, parent_job_id = None)
      ├── tester   (tester harness, parent_job_id = coordinator)
      └── verifier (verify harness, parent_job_id = coordinator)
            dep: tester must succeed first
            runtime loop:
              ├── verifier runs → spawns child dev → exits → waiting
              ├── dev child runs → writes impl → completes
              └── verifier wakes → re-runs → passes or loops again

Dev is not pre-created. The verifier spawns it on demand when tests fail.

Cross-task ordering: each task's tester depends on the previous task's
coordinator job (success type). The coordinator only becomes runnable once
all its children (including any runtime-spawned dev grandchildren) reach a
terminal state, so depending on the coordinator is equivalent to "wait for
the entire previous task to finish".

For all_join_in (which needs fibonacci, binary_sort, AND case_converter done)
this means three coordinator deps instead of three verifier deps, but the
intent is clearer: "wait for task X" rather than "wait for a specific job
inside task X".
"""

import json
import requests
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SERVER_URL = "http://localhost:8000"
WORKSPACE_PATH = "./workspace"

DIRECT_TASKS = [
    {
        "outputFilename": "fibonacci.py",
        "task": (
            "Write a Python function called fibonacci(n) that returns the nth "
            "Fibonacci number."
        ),
    },
    {
        "outputFilename": "binary_sort.py",
        "task": (
            "Write a Python function called binary_sort(arr) that returns a sorted "
            "copy of the input list using Python's built-in sort."
        ),
    },
    {
        "outputFilename": "case_converter.py",
        "task": (
            "Write a Python function called convert_case(text, target) that converts "
            "text to 'lower', 'upper', 'kebab', or 'camel' case. "
            "The method should only work on text and any numerical inputs should throw an error"
        ),
    },
    {
        "outputFilename": "all_join_in.py",
        "inputFiles": ["fibonacci.py", "binary_sort.py", "case_converter.py"],
        "task": (
            "Write a Python module that imports fibonacci from fibonacci, "
            "binary_sort from binary_sort, and convert_case from case_converter. "
            "Create a function called run_all() that demonstrates all three: "
            "print the first 10 Fibonacci numbers, sort a sample list of integers "
            "with binary_sort and print the result, then convert a few strings to "
            "all four case formats ('lower', 'upper', 'kebab', 'camel') using "
            "convert_case and print each. "
            "Include an if __name__ == '__main__' block that calls run_all()."
        ),
    },
]


# ---------------------------------------------------------------------------
# Pipeline monitoring and artifact display (same as test_simple_pipeline.py)
# ---------------------------------------------------------------------------

def get_pipeline_status(pipeline_id: str):
    response = requests.get(f"{SERVER_URL}/pipelines/{pipeline_id}", timeout=5)
    response.raise_for_status()
    return response.json()


def monitor(pipeline_id: str):
    print("\nMonitoring progress (Ctrl+C to stop)...")
    last_status = {}

    try:
        while True:
            time.sleep(2)

            status = get_pipeline_status(pipeline_id)
            pipeline_status = status['pipeline']['status']

            for job in status['jobs']:
                job_key = f"{job['agent_type']}:{job['status']}"
                if job_key != last_status.get(job['job_id']):
                    icon = (
                        "..." if job['status'] == 'running'   else
                        "[~]" if job['status'] == 'waiting'   else
                        "OK"  if job['status'] == 'completed' else
                        "FAIL" if job['status'] == 'failed'   else
                        "-"
                    )
                    search_text = job.get('original_prompt') or job.get('prompt') or ''
                    task_hint = ''
                    for line in search_text.splitlines():
                        if line.startswith('Task:') or line.startswith('FILENAME:'):
                            task_hint = line.split(':', 1)[1].strip()[:60]
                            break
                    label = task_hint or job['job_id'][:8]
                    print(f"  [{icon}] {job['agent_type']} [{job['job_id'][:6]}] {job['status']}  {label}")
                    last_status[job['job_id']] = job_key

            if pipeline_status in ['completed', 'failed', 'cancelled']:
                result = "OK" if pipeline_status == 'completed' else "FAIL"
                print(f"\n[{result}] Pipeline {pipeline_status}")
                break

    except KeyboardInterrupt:
        print("\n\nMonitoring stopped")


def show_artifacts(pipeline_id: str):
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

    if not artifacts:
        print("\n(no artifacts recorded)")
        return

    print("\nArtifacts:")
    for artifact in artifacts:
        print(f"\n--- {artifact['agent_type']}: {artifact['name']} ---")
        content = artifact['content']
        if not content and artifact.get('file_path'):
            try:
                with open(artifact['file_path'], 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
            except OSError:
                content = f"(file not found: {artifact['file_path']})"
        if not content:
            content = "(no content)"
        print(content[:500] + ("\n... (truncated)" if len(content) > 500 else ""))


def clear_workspace(workspace_path: str = WORKSPACE_PATH):
    ws = Path(workspace_path)
    if ws.exists():
        for item in ws.iterdir():
            if item.is_file():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
    print(f"   Workspace cleared: {workspace_path}")


# ---------------------------------------------------------------------------
# Pipeline creation using parent-child jobs
# ---------------------------------------------------------------------------

def start_pc_pipeline(tasks=DIRECT_TASKS, workspace_path=WORKSPACE_PATH):
    """
    Create a pipeline using the parent-child job model with runtime dev spawning.

    Per task:
      - coordinator: mock job, trivially exits 0. Becomes runnable only
        after all children (including runtime-spawned dev grandchildren) reach
        a terminal state. Acts as a "task complete" gate for downstream ordering.
      - tester, verifier: children (parent_job_id = coordinator). The verifier
        depends on tester succeeding, then spawns dev child jobs at runtime.

    Cross-task deps point at coordinators, not individual verifiers.
    """
    conn = sqlite3.connect("clowder.db")
    ts = datetime.now(timezone.utc).isoformat()

    pipeline_id = str(uuid.uuid4())
    stage_id = str(uuid.uuid4())

    conn.execute("""
        INSERT INTO pipelines (pipeline_id, template_id, original_prompt, status, created_at, updated_at)
        VALUES (?, NULL, 'parent-child pipeline run', 'pending', ?, ?)
    """, (pipeline_id, ts, ts))

    conn.execute("""
        INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at)
        VALUES (?, ?, 'code', 0, 'pending', ?)
    """, (stage_id, pipeline_id, ts))

    allowed = json.dumps([workspace_path])

    def _job(agent_type, prompt, command_tpl, parent_job_id=None, vendor='local-ollama', model=None):
        job_id = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO jobs (
                job_id, pipeline_id, stage_id, agent_type,
                prompt, original_prompt, command,
                max_iterations, timeout_seconds, model, vendor, allowed_paths,
                parent_job_id,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 300, ?, ?, ?, ?, 'pending', ?, ?)
        """, (
            job_id, pipeline_id, stage_id, agent_type,
            prompt, prompt, command_tpl.format(job_id=job_id),
            model, vendor,
            allowed, parent_job_id,
            ts, ts,
        ))
        return job_id

    def _dep(job_id, depends_on, dep_type='success'):
        conn.execute("""
            INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type)
            VALUES (?, ?, ?)
        """, (job_id, depends_on, dep_type))

    # Maps outputFilename → coordinator_id, for cross-task deps.
    file_coordinator: dict[str, str] = {}
    prev_coordinator_id: str | None = None
    prev_output_file: str | None = None

    for task_def in tasks:
        output_file = task_def['outputFilename']
        input_files = task_def.get('inputFiles', [])

        header = f"FILENAME: {output_file}"
        if input_files:
            header += f"\nINPUT_FILES: {','.join(input_files)}"
        prompt = f"{header}\n\n{task_def['task']}"

        # Coordinator: mock, no harness, just signals "task complete".
        # It cannot run until all its children are terminal (NOT EXISTS check
        # in find_runnable_job).  When it eventually runs and exits 0 it
        # completes normally — downstream tasks depend on this completion.
        coordinator_id = _job(
            'mock',
            f"coordinator: {output_file}",
            'python -c "import sys; sys.exit(0)"',
            parent_job_id=None,
        )

        # Children: tester and verifier. Dev is spawned at runtime by the verifier.
        # vendor='alibaba' propagates to dev children via spawn_child_dev.
        tester_id   = _job('tester',   prompt, 'python -u agents/test_harness.py {job_id}',   coordinator_id, vendor='alibaba')
        verifier_id = _job('verifier', prompt, 'python -u agents/verify_harness.py {job_id}', coordinator_id, vendor='alibaba')

        # Within-task ordering: verifier waits for tester to succeed.
        # Dev is not pre-created; the verifier spawns it on demand.
        _dep(verifier_id, tester_id)   # verifier waits for tester to succeed

        # Cross-task ordering: tester waits for the previous task's coordinator,
        # not for an individual verifier.  Skip if prev task is already an
        # inputFile (the loop below will add that dep explicitly).
        if prev_coordinator_id and prev_output_file not in input_files:
            _dep(tester_id, prev_coordinator_id)

        for f in input_files:
            if f in file_coordinator:
                _dep(tester_id, file_coordinator[f])

        file_coordinator[output_file] = coordinator_id
        prev_coordinator_id = coordinator_id
        prev_output_file = output_file

    conn.commit()
    conn.close()
    return pipeline_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("\nStarting parent-child pipeline (hardcoded tasks, no LLM planner)")
    print(f"   Tasks: {[t['outputFilename'] for t in DIRECT_TASKS]}")
    clear_workspace()

    try:
        pipeline_id = start_pc_pipeline()
        print(f"Pipeline created: {pipeline_id[:8]}")
    except Exception as e:
        print(f"Error creating pipeline: {e}")
        return

    try:
        get_pipeline_status(pipeline_id)
    except requests.exceptions.ConnectionError:
        print("Error: Server not running. Start with: python server/main.py")
        return

    monitor(pipeline_id)
    show_artifacts(pipeline_id)
    print(f"\nDone! Pipeline ID: {pipeline_id}")


if __name__ == "__main__":
    main()
