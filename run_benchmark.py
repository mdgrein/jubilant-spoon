#!/usr/bin/env python3
"""Clowder benchmark runner.

Runs one or more Ollama models against the case_converter and all_join_in
tasks using the tester->dev->verifier chain. Produces structured logs, per-model
summaries, and a cross-model comparison report.

Usage:
    python run_benchmark.py qwen3:14b qwen3:32b
    python run_benchmark.py qwen3:14b --ollama-cmd "ollama run"
    python run_benchmark.py qwen3:14b --ollama-cmd "wsl ollama run"

Testing without Ollama (mock model):
    # Windows:
    set CLOWDER_OLLAMA_CMD=python agents/mock_model.py
    python run_benchmark.py qwen3:14b
    # Linux/Mac:
    CLOWDER_OLLAMA_CMD="python agents/mock_model.py" python run_benchmark.py qwen3:14b
"""

import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

BENCHMARK_DIR = Path(__file__).parent.resolve()
RESULTS_DIR = BENCHMARK_DIR / "benchmark_results"

DEFAULT_OLLAMA_CMD = (
    "wsl ollama run" if platform.system() == "Windows" else "ollama run"
)

# ---------------------------------------------------------------------------
# Gold-standard workspace file contents
# These are embedded so the script is fully self-contained.
# ---------------------------------------------------------------------------

FIBONACCI_PY = """\
def fibonacci(n):
    if n < 0:
        raise ValueError("n must be a non-negative integer")
    elif n == 0:
        return 0
    elif n == 1:
        return 1
    else:
        a, b = 0, 1
        for _ in range(n - 1):
            a, b = b, a + b
        return b
"""

BINARY_SORT_PY = """\
def binary_sort(arr):
    return sorted(arr)
"""

TEST_FIBONACCI_PY = """\
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fibonacci import fibonacci


def test_fibonacci_0():
    assert fibonacci(0) == 0


def test_fibonacci_1():
    assert fibonacci(1) == 1


def test_fibonacci_2():
    assert fibonacci(2) == 1


def test_fibonacci_3():
    assert fibonacci(3) == 2


def test_fibonacci_4():
    assert fibonacci(4) == 3


def test_fibonacci_5():
    assert fibonacci(5) == 5


def test_fibonacci_6():
    assert fibonacci(6) == 8


def test_fibonacci_7():
    assert fibonacci(7) == 13


def test_fibonacci_8():
    assert fibonacci(8) == 21


def test_fibonacci_9():
    assert fibonacci(9) == 34


def test_fibonacci_10():
    assert fibonacci(10) == 55


def test_fibonacci_negative():
    with pytest.raises(ValueError):
        fibonacci(-1)


def test_fibonacci_non_integer():
    with pytest.raises(TypeError):
        fibonacci(2.5)


def test_fibonacci_large_positive():
    # Fibonacci(20) is 6765
    assert fibonacci(20) == 6765


def test_fibonacci_large_negative():
    with pytest.raises(ValueError):
        fibonacci(-20)


def test_fibonacci_invalid_type_string():
    with pytest.raises(TypeError):
        fibonacci("string")


def test_fibonacci_invalid_type_float():
    with pytest.raises(TypeError):
        fibonacci(10.5)


def test_fibonacci_zero_index():
    assert fibonacci(0) == 0


def test_fibonacci_one_index():
    assert fibonacci(1) == 1


def test_fibonacci_two_index():
    assert fibonacci(2) == 1


def test_fibonacci_positive_integer():
    assert fibonacci(10) == 55


def test_fibonacci_negative_integer():
    with pytest.raises(ValueError):
        fibonacci(-5)


def test_fibonacci_large_positive_100():
    # Fibonacci(100) is 354224848179261915075
    assert fibonacci(100) == 354224848179261915075
"""

TEST_BINARY_SORT_PY = """\
import pytest
from binary_sort import binary_sort


def test_empty_list():
    assert binary_sort([]) == []


def test_single_element():
    assert binary_sort([5]) == [5]


def test_positive_integers():
    assert binary_sort([3, 1, 4, 1, 5]) == [1, 1, 3, 4, 5]


def test_negative_integers():
    assert binary_sort([-3, -1, -4, -1, -5]) == [-5, -4, -3, -1, -1]


def test_mixed_integers():
    assert binary_sort([-1, 0, 1]) == [-1, 0, 1]


def test_floats():
    assert binary_sort([2.5, 1.2, 3.8, 0.1]) == [0.1, 1.2, 2.5, 3.8]


def test_strings():
    assert binary_sort(["banana", "apple", "cherry"]) == ["apple", "banana", "cherry"]


def test_unicode_strings():
    assert binary_sort(["café", "naïve", "año"]) == ["año", "café", "naïve"]


def test_mixed_types():
    with pytest.raises(TypeError):
        binary_sort([1, "a", 2])


def test_none_in_list():
    with pytest.raises(TypeError):
        binary_sort([None, 1, 2])


def test_complex_numbers():
    with pytest.raises(TypeError):
        binary_sort([1 + 2j, 2 + 1j])


def test_dicts():
    with pytest.raises(TypeError):
        binary_sort([{1: 2}, {3: 4}])


def test_tuples():
    assert binary_sort([(3, 2), (1, 4), (2, 1)]) == [(1, 4), (2, 1), (3, 2)]


def test_empty_string():
    assert binary_sort([""]) == [""]


def test_sorted_input():
    assert binary_sort([1, 2, 3]) == [1, 2, 3]


def test_reverse_sorted_input():
    assert binary_sort([3, 2, 1]) == [1, 2, 3]


def test_large_numbers():
    large_list = list(range(1000, 0, -1))
    assert binary_sort(large_list) == list(range(1, 1001))


def test_list_with_duplicates():
    assert binary_sort([5, 3, 2, 5, 3, 3]) == [2, 3, 3, 3, 5, 5]


def test_binary_sort_alias():
    assert binary_sort([5, 3, 2]) == sorted([5, 3, 2])
"""

# ---------------------------------------------------------------------------
# Task definitions (verbatim prompts from pipeline 9cb49c24)
# ---------------------------------------------------------------------------

TASKS = [
    {
        "name": "case_converter",
        "prompt": (
            "FILENAME: case_converter.py\n\n"
            "Write a Python function called convert_case(text, target) that converts "
            "text to 'lower', 'upper', 'kebab', or 'camel' case. The method should "
            "only work on text and any numerical inputs should throw an error"
        ),
    },
    {
        "name": "all_join_in",
        "prompt": (
            "FILENAME: all_join_in.py\n"
            "INPUT_FILES: fibonacci.py,binary_sort.py,case_converter.py\n\n"
            "Write a Python module that imports fibonacci from fibonacci, binary_sort "
            "from binary_sort, and convert_case from case_converter. Create a function "
            "called run_all() that demonstrates all three: print the first 10 Fibonacci "
            "numbers, sort a sample list of integers with binary_sort and print the "
            "result, then convert a few strings to all four case formats ('lower', "
            "'upper', 'kebab', 'camel') using convert_case and print each. Include an "
            "if __name__ == '__main__' block that calls run_all()."
        ),
    },
]


# ---------------------------------------------------------------------------
# Workspace setup
# ---------------------------------------------------------------------------

def setup_workspace(path: Path) -> None:
    """Create workspace directory with gold-standard pre-verified files."""
    (path / "tests").mkdir(parents=True, exist_ok=True)
    (path / "fibonacci.py").write_text(FIBONACCI_PY, encoding="utf-8")
    (path / "binary_sort.py").write_text(BINARY_SORT_PY, encoding="utf-8")
    (path / "tests" / "test_fibonacci.py").write_text(TEST_FIBONACCI_PY, encoding="utf-8")
    (path / "tests" / "test_binary_sort.py").write_text(TEST_BINARY_SORT_PY, encoding="utf-8")


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def setup_db(db_path: Path):
    """Create a fresh pipeline DB. Returns (conn, pipeline_id, stage_id)."""
    schema_path = BENCHMARK_DIR / "agents" / "schema_pipelines.sql"
    if not schema_path.exists():
        print(f"ERROR: schema not found at {schema_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    conn.commit()

    ts = datetime.now(timezone.utc).isoformat()
    pipeline_id = str(uuid.uuid4())
    stage_id = str(uuid.uuid4())

    conn.execute(
        "INSERT INTO pipelines "
        "(pipeline_id, template_id, original_prompt, status, created_at, updated_at) "
        "VALUES (?, NULL, 'benchmark', 'running', ?, ?)",
        (pipeline_id, ts, ts),
    )
    conn.execute(
        "INSERT INTO stages "
        "(stage_id, pipeline_id, name, stage_order, status, created_at) "
        "VALUES (?, ?, 'main', 1, 'running', ?)",
        (stage_id, pipeline_id, ts),
    )
    conn.commit()
    return conn, pipeline_id, stage_id


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------

def create_job(
    conn,
    pipeline_id: str,
    stage_id: str,
    agent_type: str,
    prompt: str,
    original_prompt: str,
    workspace_path: Path,
) -> str:
    """Insert a pending job. Returns job_id."""
    ts = datetime.now(timezone.utc).isoformat()
    job_id = str(uuid.uuid4())
    command = f"python -u agents/{agent_type}_harness.py {job_id}"
    conn.execute(
        """
        INSERT INTO jobs (
            job_id, pipeline_id, stage_id, agent_type,
            prompt, original_prompt, command,
            max_iterations, timeout_seconds, allowed_paths,
            status, retry_count, max_retries, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 15, 300, ?, 'pending', 0, 2, ?, ?)
        """,
        (
            job_id, pipeline_id, stage_id, agent_type,
            prompt, original_prompt, command,
            json.dumps([str(workspace_path)]),
            ts, ts,
        ),
    )
    conn.commit()
    return job_id


def create_job_dependency(
    conn, job_id: str, depends_on_job_id: str, dep_type: str = "success"
) -> None:
    conn.execute(
        "INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type) "
        "VALUES (?, ?, ?)",
        (job_id, depends_on_job_id, dep_type),
    )
    conn.commit()


def find_runnable_job(conn, pipeline_id: str):
    """Return the next runnable pending job for this pipeline, or None."""
    return conn.execute(
        """
        SELECT j.job_id, j.agent_type, j.prompt, j.original_prompt,
               j.command, j.retry_count, j.max_retries
        FROM jobs j
        WHERE j.pipeline_id = ?
          AND j.status = 'pending'
          AND NOT EXISTS (
              SELECT 1
              FROM job_dependencies jd
              JOIN jobs dep ON jd.depends_on_job_id = dep.job_id
              WHERE jd.job_id = j.job_id
                AND (
                  (jd.dependency_type = 'success'   AND dep.status != 'completed')
                  OR (jd.dependency_type = 'failure'   AND dep.status != 'failed')
                  OR (jd.dependency_type = 'always'    AND dep.status NOT IN ('completed', 'failed', 'skipped'))
                  OR (jd.dependency_type = 'completed' AND dep.status NOT IN ('completed', 'failed', 'skipped'))
                )
          )
        LIMIT 1
        """,
        (pipeline_id,),
    ).fetchone()


def handle_job_result(conn, job_id: str, returncode: int) -> None:
    """Update job status based on subprocess exit code.

    returncode 0  → completed (harness succeeded)
    returncode 1  → increment retry_count; retry if under limit, else fail
    returncode ≥2 → pending (OS kill — harness did not update DB)
    """
    ts = datetime.now(timezone.utc).isoformat()
    if returncode == 0:
        conn.execute(
            "UPDATE jobs SET status='completed', termination_reason='success', "
            "updated_at=? WHERE job_id=?",
            (ts, job_id),
        )
    elif returncode == 1:
        row = conn.execute(
            "SELECT retry_count, max_retries FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        retry_count = (row["retry_count"] or 0) + 1
        max_retries = row["max_retries"] if row["max_retries"] is not None else 2
        if retry_count > max_retries:
            conn.execute(
                "UPDATE jobs SET status='failed', retry_count=?, updated_at=? "
                "WHERE job_id=?",
                (retry_count, ts, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status='pending', retry_count=?, updated_at=? "
                "WHERE job_id=?",
                (retry_count, ts, job_id),
            )
    else:
        # OS kill or crash — harness did not update DB; reset to pending
        conn.execute(
            "UPDATE jobs SET status='pending', updated_at=? WHERE job_id=?",
            (ts, job_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Subprocess execution
# ---------------------------------------------------------------------------

def _task_name_for_prompt(prompt: str) -> str:
    """Extract task name from a prompt's FILENAME: line."""
    for line in (prompt or "").splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("FILENAME:"):
            filename = stripped[len("FILENAME:"):].strip()
            return filename.replace(".py", "")
    return "unknown"


def run_job_subprocess(
    job,
    db_path: Path,
    model_cmd: str,
    log_path: Path,
    prefix: str,
) -> int:
    """Spawn the harness subprocess, tee output to log and console.

    If CLOWDER_OLLAMA_CMD is already set in the environment (e.g. for mock
    model testing), that value is used as-is. Otherwise, model_cmd is used.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["CLOWDER_DB_PATH"] = str(db_path)
    env["PYTHONIOENCODING"] = "utf-8"
    # Only set CLOWDER_OLLAMA_CMD if not already overridden (e.g. mock model)
    if "CLOWDER_OLLAMA_CMD" not in env:
        env["CLOWDER_OLLAMA_CMD"] = f"{model_cmd} --nowordwrap"

    with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
        proc = subprocess.Popen(
            job["command"],
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(BENCHMARK_DIR),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in iter(proc.stdout.readline, ""):
            log_file.write(line)
            log_file.flush()
            print(f"{prefix} {line}", end="", flush=True)
        proc.wait()

    return proc.returncode


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def orchestrate(
    conn,
    pipeline_id: str,
    db_path: Path,
    model_cmd: str,
    log_dir: Path,
    model_label: str,
) -> None:
    """Run all runnable jobs until no more are available."""
    while True:
        job = find_runnable_job(conn, pipeline_id)
        if job is None:
            break

        job_id = job["job_id"]
        agent_type = job["agent_type"]
        original_prompt = job["original_prompt"] or job["prompt"]
        retry_count = job["retry_count"] or 0

        task_name = _task_name_for_prompt(original_prompt)
        log_path = (
            log_dir / task_name / f"{job_id[:8]}_{agent_type}_attempt{retry_count}.log"
        )
        # Pad agent_type to 8 chars for aligned console output
        prefix = f"[{model_label} / {task_name} / {agent_type:<8}]"

        print(f"\n{prefix} Starting (attempt {retry_count + 1})...", flush=True)

        # Mark running
        ts = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE jobs SET status='running', started_at=?, updated_at=? WHERE job_id=?",
            (ts, ts, job_id),
        )
        conn.commit()

        returncode = run_job_subprocess(job, db_path, model_cmd, log_path, prefix)

        updated = conn.execute(
            "SELECT status, termination_reason FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()

        # If harness updated DB itself (rc=0 or rc=1), trust it; only override for rc>=2
        if returncode >= 2:
            handle_job_result(conn, job_id, returncode)
            updated = conn.execute(
                "SELECT status, termination_reason FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()

        status = updated["status"] if updated else "unknown"
        reason = updated["termination_reason"] if updated else ""
        print(
            f"{prefix} Done (rc={returncode}, db={status}"
            + (f", reason={reason}" if reason else "")
            + ")",
            flush=True,
        )

        # Print task-level summary when a verifier finishes
        if agent_type == "verifier":
            _print_task_banner(conn, pipeline_id, task_name, original_prompt)

    # Check for stuck jobs
    stuck = conn.execute(
        "SELECT COUNT(*) as n FROM jobs WHERE pipeline_id=? AND status='pending'",
        (pipeline_id,),
    ).fetchone()
    if stuck and stuck["n"] > 0:
        print(
            f"\nWARNING: {stuck['n']} job(s) remain pending with unsatisfied "
            f"dependencies. Pipeline halted (likely a dependency chain blocked by "
            f"a failed job).",
            flush=True,
        )


def _print_task_banner(conn, pipeline_id: str, task_name: str, original_prompt: str) -> None:
    """Print a short result line after each verifier completes."""
    verifiers = conn.execute(
        "SELECT status, job_output FROM jobs "
        "WHERE pipeline_id=? AND agent_type='verifier' AND original_prompt=? "
        "ORDER BY created_at",
        (pipeline_id, original_prompt),
    ).fetchall()
    verdict = "?"
    for v in verifiers:
        output = v["job_output"] or ""
        if str(output).startswith("PASS:"):
            verdict = "PASS"
            break
    else:
        if verifiers:
            last = verifiers[-1]
            verdict = "FAIL" if last["status"] == "completed" else "EXHAUSTED" if last["status"] == "failed" else "..."

    dev_count = conn.execute(
        "SELECT COUNT(*) as n FROM jobs "
        "WHERE pipeline_id=? AND agent_type='dev' AND original_prompt=?",
        (pipeline_id, original_prompt),
    ).fetchone()["n"]

    print(f"\n{'─' * 50}", flush=True)
    print(f"  {task_name}: {verdict} ({dev_count} dev attempt(s))", flush=True)
    print(f"{'─' * 50}\n", flush=True)


# ---------------------------------------------------------------------------
# Result collection
# ---------------------------------------------------------------------------

def collect_results(conn, pipeline_id: str, tasks: list) -> dict:
    """Collect per-task results from the DB."""
    results = {}
    for task in tasks:
        task_name = task["name"]
        original_prompt = task["prompt"]

        # Tester outcome
        tester = conn.execute(
            "SELECT status FROM jobs "
            "WHERE pipeline_id=? AND agent_type='tester' AND original_prompt=? "
            "ORDER BY created_at LIMIT 1",
            (pipeline_id, original_prompt),
        ).fetchone()
        tester_result = "pass" if (tester and tester["status"] == "completed") else "fail"

        # Verifier outcome
        verifiers = conn.execute(
            "SELECT job_id, status, termination_reason, job_output FROM jobs "
            "WHERE pipeline_id=? AND agent_type='verifier' AND original_prompt=? "
            "ORDER BY created_at",
            (pipeline_id, original_prompt),
        ).fetchall()

        final_verdict = "unknown"
        for v in verifiers:
            output = v["job_output"] or ""
            if str(output).startswith("PASS:"):
                final_verdict = "pass"
                break
        else:
            if verifiers:
                last = verifiers[-1]
                if last["status"] == "failed":
                    final_verdict = "exhausted"
                elif last["status"] == "completed":
                    final_verdict = "fail"
                elif last["status"] in ("pending", "running"):
                    final_verdict = "incomplete"

        # Dev job stats
        dev_jobs = conn.execute(
            "SELECT job_id, iteration FROM jobs "
            "WHERE pipeline_id=? AND agent_type='dev' AND original_prompt=? "
            "ORDER BY created_at",
            (pipeline_id, original_prompt),
        ).fetchall()
        dev_attempts = len(dev_jobs)
        total_dev_iterations = sum(r["iteration"] or 0 for r in dev_jobs)

        # Timeouts and format errors from actions table
        job_ids = [
            r["job_id"]
            for r in conn.execute(
                "SELECT job_id FROM jobs WHERE pipeline_id=? AND original_prompt=?",
                (pipeline_id, original_prompt),
            ).fetchall()
        ]
        timeouts = 0
        format_errors = 0
        if job_ids:
            placeholders = ",".join("?" * len(job_ids))
            rows = conn.execute(
                f"SELECT results FROM actions WHERE job_id IN ({placeholders})",
                job_ids,
            ).fetchall()
            for row in rows:
                try:
                    r = json.loads(row["results"] or "{}")
                    status = r.get("status", "")
                    if status == "timeout":
                        timeouts += 1
                    elif status == "format_error":
                        format_errors += 1
                except (json.JSONDecodeError, TypeError):
                    pass

        results[task_name] = {
            "tester": tester_result,
            "final_verdict": final_verdict,
            "dev_attempts": dev_attempts,
            "total_dev_iterations": total_dev_iterations,
            "timeouts": timeouts,
            "format_errors": format_errors,
        }
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_summary(model_name: str, results: dict, result_dir: Path) -> None:
    """Write summary.json and summary.txt for one model."""
    summary_data = {"model": model_name, "results": results}
    (result_dir / "summary.json").write_text(
        json.dumps(summary_data, indent=2), encoding="utf-8"
    )

    lines = [f"Model: {model_name}", "─" * 55]
    for task_name, r in results.items():
        verdict = r["final_verdict"].upper()
        dev_word = "attempt" if r["dev_attempts"] == 1 else "attempts"
        detail = f"{r['dev_attempts']} dev {dev_word}, {r['total_dev_iterations']} iterations"
        if r["timeouts"]:
            detail += f", {r['timeouts']} timeout{'s' if r['timeouts'] != 1 else ''}"
        if r["format_errors"]:
            detail += f", {r['format_errors']} format error{'s' if r['format_errors'] != 1 else ''}"
        lines.append(f"  {task_name:<22} {verdict:<12} ({detail})")
    lines.append("─" * 55)
    summary_text = "\n".join(lines) + "\n"

    (result_dir / "summary.txt").write_text(summary_text, encoding="utf-8")
    print("\n" + summary_text)


def write_report(all_model_results: dict, result_dir: Path) -> None:
    """Write cross-model comparison report.txt."""
    task_names = [t["name"] for t in TASKS]
    models = list(all_model_results.keys())

    lines = [
        "Clowder Benchmark Report",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 65,
        "",
    ]

    # Header
    col_w = 18
    header = f"  {'Task':<22}"
    for m in models:
        label = m.replace(":", "-")
        header += f"  {label:<{col_w}}"
    lines.append(header)
    lines.append("  " + "-" * (22 + (col_w + 2) * len(models)))

    # Rows
    for task_name in task_names:
        row = f"  {task_name:<22}"
        for m in models:
            r = all_model_results[m].get("results", {}).get(task_name, {})
            verdict = r.get("final_verdict", "?").upper()
            dev = r.get("dev_attempts", 0)
            cell = f"{verdict} ({dev}dev)"
            row += f"  {cell:<{col_w}}"
        lines.append(row)

    lines.append("")
    lines.append("Verdict: pass=LLM judge accepted | fail=rejected | exhausted=retry limit hit")
    lines.append("(dev) = total dev job invocations across retry cycles")
    lines.append("")

    # Per-model detail
    for m, data in all_model_results.items():
        lines.append(f"── {m} ──")
        for task_name, r in data.get("results", {}).items():
            verdict = r.get("final_verdict", "?").upper()
            dev_info = f"{r.get('dev_attempts', 0)} dev"
            iter_info = f"{r.get('total_dev_iterations', 0)} iter"
            lines.append(f"   {task_name:<22} {verdict:<12} ({dev_info}, {iter_info})")
        lines.append("")

    report_path = result_dir / "report.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport: {report_path}")


# ---------------------------------------------------------------------------
# Per-model runner
# ---------------------------------------------------------------------------

def run_model(model_name: str, ollama_cmd_prefix: str, run_dir: Path) -> dict:
    """Set up workspace, DB, and jobs for one model; run the full pipeline."""
    model_slug = model_name.replace(":", "-").replace("/", "-")
    model_dir = run_dir / model_slug
    workspace_path = model_dir / "workspace"
    log_dir = model_dir / "logs"
    db_path = model_dir / "benchmark.db"

    model_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Workspace
    setup_workspace(workspace_path)

    # DB
    conn, pipeline_id, stage_id = setup_db(db_path)

    # Build model command (model name appended to prefix)
    model_cmd = f"{ollama_cmd_prefix} {model_name}"

    # --- Task 1: case_converter ---
    cc = TASKS[0]
    cc_tester_id = create_job(
        conn, pipeline_id, stage_id, "tester",
        cc["prompt"], cc["prompt"], workspace_path,
    )
    cc_dev_id = create_job(
        conn, pipeline_id, stage_id, "dev",
        cc["prompt"], cc["prompt"], workspace_path,
    )
    cc_verify_id = create_job(
        conn, pipeline_id, stage_id, "verifier",
        cc["prompt"], cc["prompt"], workspace_path,
    )
    create_job_dependency(conn, cc_dev_id, cc_tester_id, "success")
    create_job_dependency(conn, cc_verify_id, cc_dev_id, "completed")

    # --- Task 2: all_join_in ---
    aj = TASKS[1]
    aj_tester_id = create_job(
        conn, pipeline_id, stage_id, "tester",
        aj["prompt"], aj["prompt"], workspace_path,
    )
    aj_dev_id = create_job(
        conn, pipeline_id, stage_id, "dev",
        aj["prompt"], aj["prompt"], workspace_path,
    )
    aj_verify_id = create_job(
        conn, pipeline_id, stage_id, "verifier",
        aj["prompt"], aj["prompt"], workspace_path,
    )
    create_job_dependency(conn, aj_dev_id, aj_tester_id, "success")
    create_job_dependency(conn, aj_verify_id, aj_dev_id, "completed")
    # all_join_in waits for case_converter regardless of outcome
    create_job_dependency(conn, aj_tester_id, cc_verify_id, "completed")

    # Orchestrate
    orchestrate(conn, pipeline_id, db_path, model_cmd, log_dir, model_slug)

    # Copy final workspace state
    workspace_final = model_dir / "workspace_final"
    if workspace_path.exists():
        if workspace_final.exists():
            shutil.rmtree(workspace_final)
        shutil.copytree(workspace_path, workspace_final)

    # Collect and report
    results = collect_results(conn, pipeline_id, TASKS)
    write_summary(model_name, results, model_dir)

    conn.close()
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clowder benchmark: test Ollama models on TDD coding tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "models",
        nargs="+",
        metavar="MODEL",
        help="Ollama model name(s), e.g. qwen3:14b qwen3:32b",
    )
    parser.add_argument(
        "--ollama-cmd",
        default=DEFAULT_OLLAMA_CMD,
        metavar="CMD",
        help=(
            f"Ollama run command prefix (default: {DEFAULT_OLLAMA_CMD!r}). "
            "The model name is appended automatically."
        ),
    )
    args = parser.parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = RESULTS_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    all_model_results: dict[str, dict] = {}

    for i, model_name in enumerate(args.models, 1):
        print(f"\n{'═' * 42}")
        print(f"  MODEL: {model_name} ({i}/{len(args.models)})")
        print(f"{'═' * 42}\n")

        results = run_model(model_name, args.ollama_cmd, run_dir)
        all_model_results[model_name] = {"results": results}

    write_report(all_model_results, run_dir)

    print(f"\n{'═' * 42}")
    print(f"  All done. Results: {run_dir}")
    print(f"{'═' * 42}\n")


if __name__ == "__main__":
    main()
