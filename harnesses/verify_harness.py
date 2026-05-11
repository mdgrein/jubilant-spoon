#!/usr/bin/env python3
"""
Verify harness (TDD loop).

Runs pytest against the test file for the just-completed implementation,
then asks the LLM to make a qualitative judgment: does this actually solve
the task?

- If LLM says PASS: mark job complete. Done.
- If LLM says FAIL (or pytest collection error): spawn a new dev job (with
  the LLM's critique as context) and a new verifier job, then mark self
  complete. The cycle continues until MAX_DEV_RETRIES is exhausted.
- If max retries reached: mark job failed.

Job prompt format (same FILENAME: convention):
    FILENAME: fibonacci.py

    <original task description>

The harness exits 0 on success or after spawning retry jobs, 1 on hard failure.
"""

import json
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from harness_common import (
    MAX_DEV_RETRIES,
    call_model,
    complete_job,
    fail_job,
    load_job,
    log_prompt,
    parse_prompt,
    run_pytest,
    spawn_child_dev,
    strip_thinking,
)
from dev_utils import classify_failure


# ---------------------------------------------------------------------------
# LLM judge helpers
# ---------------------------------------------------------------------------


def _judge_prompt(task: str, filename: str, code: str, pytest_output: str) -> str:
    return (
        "You are a code reviewer. Answer with exactly one word on the first line:\n"
        "PASS or FAIL\n\n"
        "Then on the next line, write a one-sentence explanation.\n\n"
        f"TASK:\n{task}\n\n"
        f"IMPLEMENTATION ({filename}):\n{code}\n\n"
        f"TEST RESULTS:\n{pytest_output[-3000:]}\n\n"
        "Does the implementation correctly solve the task? Output PASS or FAIL first."
    )


def _parse_verdict(raw: str) -> tuple[str, str]:
    """Return ('PASS'|'FAIL'|'UNKNOWN', explanation). UNKNOWN treated as FAIL."""
    text = strip_thinking(raw).strip()
    head = text[:200].upper()
    if "PASS" in head and "FAIL" not in head:
        verdict = "PASS"
    elif "FAIL" in head:
        verdict = "FAIL"
    else:
        verdict = "UNKNOWN"
    lines = text.splitlines()
    explanation = " ".join(lines[1:]).strip() if len(lines) > 1 else text
    return verdict, explanation


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if len(sys.argv) < 2:
        print("Usage: verify_harness.py <job_id>")
        sys.exit(1)

    job_id = sys.argv[1]
    db, job = load_job(job_id)

    # original_prompt carries the clean task description (no retry context).
    # job["prompt"] may include INPUT_FILES which must be preserved so retry
    # dev jobs can load the test file from disk at execution time.
    original_prompt = job["original_prompt"] or job["prompt"]
    allowed_paths = json.loads(job["allowed_paths"])
    workspace = Path(allowed_paths[0]).resolve()
    pipeline_id = job["pipeline_id"]
    stage_id = job["stage_id"]
    vendor = job["vendor"]
    model = job["model"]

    filename, task = parse_prompt(job["prompt"])
    if not filename:
        fail_job(db, job_id, "prompt missing FILENAME: line", tag="verify")

    test_path = workspace / "tests" / f"test_{filename}"
    if not test_path.exists():
        fail_job(db, job_id, f"test file not found: {test_path}", tag="verify")

    impl_path = workspace / filename

    # Mark iteration 1 in DB for live UI progress
    ts = db._timestamp()
    db.conn.execute(
        "UPDATE jobs SET iteration = 1, updated_at = ? WHERE job_id = ?", (ts, job_id)
    )
    db.conn.commit()

    action_llm_response = {}
    action_results = {}

    print(f"[verify] {job_id[:8]} running pytest for {filename}")
    returncode, pytest_output = run_pytest(test_path, workspace)

    for line in pytest_output.splitlines()[-30:]:
        print(f"[verify]   {line}")

    iteration_log = (
        f"[verify] pytest output (returncode={returncode}):\n{pytest_output}\n"
    )
    action_results["pytest_returncode"] = returncode

    failure_info = classify_failure(pytest_output)
    hint = failure_info.to_hint()

    if returncode >= 2:
        # Collection error — skip LLM judge, treat as retry-able failure.
        # The dev job likely produced the wrong API (wrong function names, etc.).
        print(
            f"[verify] collection error (returncode {returncode}) — retry-able failure"
        )
        pytest_output = f"[COLLECTION ERROR - returncode {returncode}]\n{pytest_output}"
        verdict = "FAIL"
        explanation = f"pytest collection error (returncode {returncode})"
        failure_context = (
            f"VERIFIER CRITIQUE:\n{explanation}\n\n"
            f"{hint}\n\n"
            f"PYTEST OUTPUT:\n{pytest_output[-2000:]}"
        )
        iteration_log += f"[verify] verdict: {verdict} — {explanation}\n"
    else:
        # Tests ran (rc 0 or 1) — ask the LLM judge.
        code = impl_path.read_text(encoding="utf-8") if impl_path.exists() else ""
        judge_prompt_text = _judge_prompt(task, filename, code, pytest_output)
        log_prompt(judge_prompt_text, tag="verify")
        try:
            raw_verdict = call_model(judge_prompt_text, vendor=vendor, model=model)
            verdict, explanation = _parse_verdict(raw_verdict)
            if verdict == "PASS" and returncode != 0:
                print(
                    f"[verify] LLM said PASS but pytest failed (rc={returncode}) — treating as UNKNOWN"
                )
                verdict = "UNKNOWN"
                explanation = f"LLM said PASS but pytest failed (rc={returncode}); treating as FAIL"
            print(f"[verify] LLM verdict: {verdict} — {explanation}")
            iteration_log += (
                f"\n[verify] --- JUDGE PROMPT ({len(judge_prompt_text)} chars) ---\n"
                f"{judge_prompt_text}\n"
                f"[verify] --- END JUDGE PROMPT ---\n\n"
                f"{raw_verdict}\n"
            )
        except subprocess.TimeoutExpired:
            print("[verify] LLM judge timed out — falling back to pytest result")
            verdict = "PASS" if returncode == 0 else "FAIL"
            explanation = "LLM judge unavailable; pytest result used as fallback"
            iteration_log += "[verify] LLM judge timed out\n"
        except Exception as exc:
            print(f"[verify] LLM judge error ({exc!r}) — falling back to pytest result")
            verdict = "PASS" if returncode == 0 else "FAIL"
            explanation = f"LLM judge error: {exc!r}"
            iteration_log += f"[verify] LLM judge error: {exc!r}\n"

        action_llm_response["judge_prompt"] = judge_prompt_text
        action_llm_response["verdict"] = verdict

        if returncode == 0 and verdict != "PASS":
            failure_context = f"VERIFIER CRITIQUE (tests passed but implementation is incorrect):\n{explanation}"
        else:
            failure_context = (
                f"VERIFIER CRITIQUE:\n{explanation}\n\n"
                f"{hint}\n\n"
                f"PYTEST OUTPUT:\n{pytest_output[-2000:]}"
            )

    action_results["verdict"] = verdict
    action_results["explanation"] = explanation

    if verdict == "PASS":
        print(f"[verify] ACCEPTED: {filename}")
        db.log_action(
            job_id, 1, action_llm_response, action_results, raw_stdout=iteration_log
        )
        complete_job(db, job_id, f"PASS: {explanation}\n\n{pytest_output}")
        return

    # FAIL or UNKNOWN — check retry budget and spawn next cycle.
    attempt_num = db.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE parent_job_id = ? AND agent_type = 'dev'",
        (job_id,),
    ).fetchone()[0]
    print(
        f"[verify] REJECTED (dev attempt {attempt_num}/{MAX_DEV_RETRIES}): {explanation}"
    )

    if attempt_num >= MAX_DEV_RETRIES:
        fail_job(
            db,
            job_id,
            f"rejected after {MAX_DEV_RETRIES} dev attempts: {explanation}",
            tag="verify",
        )

    ts = db._timestamp()
    db.conn.execute(
        "UPDATE jobs SET status = 'waiting', updated_at = ? WHERE job_id = ?",
        (ts, job_id),
    )
    db.conn.commit()

    workspace_path = str(allowed_paths[0])
    spawn_child_dev(
        db=db,
        parent_job_id=job_id,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        original_prompt=original_prompt,
        filename=filename,
        task=task,
        failure_context=failure_context,
        attempt_num=attempt_num + 1,
        workspace_path=workspace_path,
        model=model,
        vendor=vendor,
    )

    db.log_action(
        job_id, 1, action_llm_response, action_results, raw_stdout=iteration_log
    )
    db.close()
    print(f"[verify] yielding to child dev (attempt {attempt_num + 1})")


if __name__ == "__main__":
    main()
