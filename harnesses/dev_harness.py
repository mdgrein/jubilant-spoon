#!/usr/bin/env python3
"""
Direct code harness.

Sends the task to the LLM and captures raw code output -- no tool calls.
The model is asked to output only raw Python code; the harness saves it,
runs ruff (format + lint), then runs the test suite (if one exists).

On ruff failure the harness retries with the lint output as context.
On pytest failure the harness retries with the test output as context.
The model gets MAX_ITERATIONS shots total across both types of failure.

If tests are still failing after MAX_ITERATIONS, the job hard-fails so
the verifier can still run (via 'completed' dependency) and drive the
outer retry cycle.

Job prompt format (set at job creation time):
    FILENAME: fibonacci.py

    Write a Python function that calculates fibonacci numbers...

The harness exits 0 on success, 1 on failure.
"""

import difflib
import json
import subprocess
import sys
from pathlib import Path
import ast  # Added for AST parsing

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from harness_common import (
    MAX_ITERATIONS,
    call_model,
    clean_output,
    complete_job,
    fail_job,
    load_job,
    log_prompt,
    parse_input_files,
    parse_prompt,
    run_pytest,
    run_ruff,
)
from dev_utils import (
    classify_failure,
    parse_pytest_failures,
    format_failure_for_llm,
    get_test_code,
    get_test_code_by_name,
    extract_all_io_examples,
)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _file_context(input_file_contents: dict[str, str]) -> str:
    """Format input file contents as a context block to append to prompts."""
    if not input_file_contents:
        return ""
    sections = "\n\n".join(
        f"# === {name} ===\n{content.rstrip()}"
        for name, content in input_file_contents.items()
    )
    return f"\n\nExisting source files to import from:\n\n{sections}"


def _initial_prompt(
    task: str, input_file_contents: dict[str, str], prior_code: str | None = None
) -> str:
    prompt = (
        "You are a Python programmer. "
        "Output ONLY raw Python code. No explanations, no markdown, no code fences.\n\n"
        + task
    )
    if prior_code:
        prompt += f"\n\nCODE TO EDIT:\n{prior_code}"
    return prompt + _file_context(input_file_contents)


def _retry_prompt(
    task: str,
    current_code: str,
    llm_feedback: str,
    input_file_contents: dict[str, str],
    is_format_failure: bool = False,
) -> str:
    base_prompt = (
        "You are a Python programmer. "
        "Output ONLY raw Python code. No explanations, no markdown, no code fences.\n\n"
    )

    if is_format_failure:
        return (
            base_prompt + f"URGENT: Your previous output was NOT valid Python code. "
            f"You MUST output ONLY raw Python code on STDOUT. "
            f"Do NOT include explanations, prose, or markdown formatting. "
            f"ORIGINAL TASK:\n{task}\n\n"
            f"ISSUES WITH PREVIOUS OUTPUT:\n{llm_feedback}\n\n"
            f"Please provide the complete, corrected Python file for the task, adhering strictly to the format."
            + _file_context(input_file_contents)
        )
    else:
        # Existing logic for code failures (ruff, pytest)
        return (
            base_prompt
            + "The code below failed automated checks. Fix ALL reported issues and output "
            "the complete corrected file -- not just the changed lines.\n\n"
            f"ORIGINAL TASK:\n{task}\n\n"
            f"CURRENT CODE:\n{current_code}\n\n"
            f"ISSUES TO FIX:\n{llm_feedback}" + _file_context(input_file_contents)
        )


def _test_failure_prompt(
    task: str,
    current_code: str,
    pytest_output: str,
    input_file_contents: dict[str, str],
) -> str:
    failure_info = classify_failure(pytest_output)
    hint = failure_info.to_hint()
    return (
        "You are a Python programmer. "
        "Output ONLY raw Python code. No explanations, no markdown, no code fences.\n\n"
        "The code below passed syntax checks but FAILED the test suite. "
        "Fix ALL failing tests and output the complete corrected file.\n\n"
        f"{hint}\n\n"
        f"ORIGINAL TASK:\n{task}\n\n"
        f"CURRENT CODE:\n{current_code}\n\n"
        f"FAILING TESTS OUTPUT:\n{pytest_output[-3000:]}"
        + _file_context(input_file_contents)
    )


def _single_test_failure_prompt(
    task: str,
    current_code: str,
    test_name: str,
    failure_traceback: str,
    test_code: str,
    input_file_contents: dict[str, str],
) -> str:
    summary = format_failure_for_llm(test_name, failure_traceback, test_code)
    return (
        "You are a Python programmer. "
        "Output ONLY raw Python code. No explanations, no markdown, no code fences.\n\n"
        f"ORIGINAL TASK:\n{task}\n\n"
        f"CURRENT CODE:\n{current_code}\n\n"
        + _file_context(input_file_contents)
        + f"\n\nFAILING TEST CODE ({test_name}):\n{test_code}\n\n"
        f"CRITICAL INSTRUCTION: {summary} "
        "Fix this specific error and output the complete corrected file."
    )


# ---------------------------------------------------------------------------
# Steering helpers
# ---------------------------------------------------------------------------

_STUCK_THRESHOLD = 3


def _extract_failure_signature(traceback: str) -> str:
    """Extract a compact identity string from a test failure traceback.

    Used to detect when the model is stuck producing the same wrong output
    for the same test across multiple iterations.
    """
    for line in reversed(traceback.splitlines()):
        line = line.strip()
        if "AssertionError" in line or line.startswith("E   "):
            return line[:150]
    return (traceback or "")[-100:]


def _with_warnings(prompt: str, warnings: list[str]) -> str:
    """Prepend any accumulated steering warnings to a prompt."""
    if not warnings:
        return prompt
    return "\n".join(warnings) + "\n\n" + prompt


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if len(sys.argv) < 2:
        print("Usage: dev_harness.py <job_id>")
        sys.exit(1)

    job_id = sys.argv[1]
    db, job = load_job(job_id)

    prompt_text = job["prompt"]
    allowed_paths = json.loads(job["allowed_paths"])
    workspace = Path(allowed_paths[0]).resolve()
    vendor = job["vendor"]
    model = job["model"]

    filename, task = parse_prompt(prompt_text)
    if not filename:
        fail_job(db, job_id, "prompt missing FILENAME: line", tag="direct")

    # Read any input files declared in the prompt header.
    input_filenames = parse_input_files(prompt_text)
    input_file_contents: dict[str, str] = {}
    for f in input_filenames:
        file_path = workspace / f
        if file_path.exists():
            input_file_contents[f] = file_path.read_text(encoding="utf-8")
        else:
            print(f"[direct] WARNING: input file not found: {file_path}")

    output_path = workspace / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    test_path = workspace / "tests" / f"test_{filename}"

    llm_feedback = ""
    pytest_output = ""
    last_failure = None  # None | 'ruff' | 'pytest' | 'format_error' | 'single_test'
    job_succeeded_in_loop = (
        False  # NEW: Track if the entire job succeeded and broke out of the loop
    )

    # State for "one test at a time" mode
    test_todo_list = []
    current_todo_test = None
    all_tests_failure_count = (
        0  # How many times have we failed in the "all at once" mode?
    )

    # Steering state
    best_code: str | None = None  # code with the fewest test failures seen so far
    best_failure_count: int = 10_000  # failure count for best_code
    stuck_tracker: dict[str, list[str]] = {}  # test_name -> last N failure signatures
    next_warnings: list[str] = []  # warnings to inject into the NEXT iteration's prompt

    start_attempt = (job["iteration"] or 0) + 1
    for attempt in range(start_attempt, MAX_ITERATIONS + 1):
        # Header for console logs
        print(f"\n\n{'=' * 80}")
        print(
            f"  ITERATION {attempt}/{MAX_ITERATIONS}  |  Job: {job_id[:8]}  |  File: {filename}"
        )
        print(f"{'=' * 80}\n")

        # Update job iteration in DB for live UI progress
        ts = db._timestamp()
        db.conn.execute(
            "UPDATE jobs SET iteration = ?, updated_at = ? WHERE job_id = ?",
            (attempt, ts, job_id),
        )
        db.conn.commit()

        existing_code = (
            output_path.read_text(encoding="utf-8") if output_path.exists() else None
        )

        # Collect steering warnings accumulated by the previous iteration, then reset
        current_warnings: list[str] = list(next_warnings)
        next_warnings = []

        # Iteration-level data for the actions table
        action_results = {}
        action_llm_response = {}
        raw_output = ""

        try:
            if last_failure is None:
                model_prompt = _with_warnings(
                    _initial_prompt(task, input_file_contents, existing_code),
                    current_warnings,
                )
            elif last_failure == "ruff":
                model_prompt = _with_warnings(
                    _retry_prompt(
                        task, existing_code, llm_feedback, input_file_contents
                    ),
                    current_warnings,
                )
            elif last_failure == "format_error":  # Special prompt for format failures
                model_prompt = _with_warnings(
                    _retry_prompt(
                        task,
                        existing_code,
                        llm_feedback,
                        input_file_contents,
                        is_format_failure=True,
                    ),
                    current_warnings,
                )
            elif last_failure == "single_test":
                # We are in the "to-do list" mode
                test_name = current_todo_test
                # Find the failure for this specific test
                failures = parse_pytest_failures(pytest_output)
                parsed_failure = failures.get(test_name)
                traceback = (
                    parsed_failure.traceback
                    if parsed_failure
                    else "No traceback found."
                )

                # Stuck detection: same failure signature N times in a row
                sig = _extract_failure_signature(traceback)
                history = stuck_tracker.get(test_name, [])
                history = (history + [sig])[-_STUCK_THRESHOLD:]
                stuck_tracker[test_name] = history
                if len(history) >= _STUCK_THRESHOLD and len(set(history)) == 1:
                    stuck_msg = (
                        f"WARNING: '{test_name}' has produced the same error {_STUCK_THRESHOLD} times "
                        f"in a row. Your current approach is not working. "
                        f"Rewrite the relevant section from scratch using a completely different approach."
                    )
                    # Append the full I/O contract so the model sees ALL constraints
                    # it must satisfy simultaneously — not just the one it's stuck on.
                    examples = extract_all_io_examples(test_path)
                    if examples:
                        table = "\n".join(
                            f"  {call} {outcome}" for call, outcome in examples
                        )
                        stuck_msg += (
                            f"\n\nFULL CONTRACT — every assertion your implementation must satisfy:\n"
                            f"{table}\n"
                            f"Design your solution to pass ALL of the above at once."
                        )
                    current_warnings.append(stuck_msg)

                # Primary: look up by name — reliable regardless of pytest path format.
                test_code = get_test_code_by_name(test_path, test_name)
                if (
                    test_code.startswith("# [")
                    and parsed_failure
                    and parsed_failure.file_path
                    and parsed_failure.line_number
                ):
                    # Fallback: path+line extracted from pytest output (unreliable on Windows).
                    test_code = get_test_code(
                        parsed_failure.file_path, parsed_failure.line_number, workspace
                    )

                model_prompt = _with_warnings(
                    _single_test_failure_prompt(
                        task,
                        existing_code,
                        test_name,
                        traceback,
                        test_code,
                        input_file_contents,
                    ),
                    current_warnings,
                )
            else:  # 'pytest' (all at once)
                model_prompt = _with_warnings(
                    _test_failure_prompt(
                        task, existing_code, pytest_output, input_file_contents
                    ),
                    current_warnings,
                )

            log_prompt(model_prompt, tag="direct")
            try:
                raw_output = call_model(model_prompt, vendor=vendor, model=model)
                # Format the iteration-specific log to include the prompt
                iteration_log = (
                    f"[direct] --- PROMPT ({len(model_prompt)} chars) ---\n"
                    f"{model_prompt}\n"
                    f"[direct] --- END PROMPT ---\n\n"
                    f"{raw_output}"
                )
            except subprocess.TimeoutExpired:
                print(f"[direct] attempt {attempt} timed out, retrying")
                last_failure = last_failure or "ruff"
                db.log_action(
                    job_id, attempt, {}, {"status": "timeout"}, raw_stdout="TIMEOUT"
                )
                continue

            code = clean_output(raw_output)
            action_llm_response["code"] = code
            print(f"\n[direct] --- EXTRACTED CODE ({len(code)} chars) ---")
            if len(code) > 1000:
                print(code[:500] + "\n... [TRUNCATED] ...\n" + code[-500:])
            else:
                print(code)
            print("[direct] --- END EXTRACTED CODE ---\n")

            # --- NEW: Strict validation that the output is pure Python code ---
            format_check_failed = False
            if not code.strip():
                print("[direct] LLM produced empty output after cleaning.")
                llm_feedback = "Your output is empty after cleaning. You must provide raw Python code."
                format_check_failed = True
            else:
                try:
                    ast.parse(code)
                except SyntaxError as e:
                    print(f"[direct] LLM output is not valid Python code: {e}")
                    is_prose = "def " not in code and "class " not in code
                    if is_prose:
                        llm_feedback = (
                            "You output a description or explanation, not Python code. "
                            "Do not explain. Do not use markdown. "
                            "Start your response directly with `import` or `def` and output ONLY executable Python."
                        )
                    else:
                        llm_feedback = (
                            f"Your output must consist ONLY of valid raw Python code. "
                            f"Do NOT include any explanations, prose, markdown formatting, or non-code text in your STDOUT. "
                            f"Specifically, a SyntaxError was found: {e}"
                        )
                    format_check_failed = True
                except Exception as e:
                    print(
                        f"[direct] LLM output is not valid Python code (parsing error): {e}"
                    )
                    llm_feedback = (
                        f"Your output must consist ONLY of valid raw Python code. "
                        f"An unexpected parsing error occurred: {type(e).__name__}: {e}."
                    )
                    format_check_failed = True

            if format_check_failed:
                last_failure = "format_error"
                action_results["status"] = "format_error"
                action_results["feedback"] = llm_feedback
                db.log_action(
                    job_id,
                    attempt,
                    action_llm_response,
                    action_results,
                    raw_stdout=iteration_log,
                )
                continue

            output_path.write_text(code, encoding="utf-8")
            print(f"[direct] wrote {len(code)} chars")

            current_iteration_passed, llm_feedback = run_ruff(output_path, tag="direct")
            if llm_feedback and llm_feedback != "SKIPPED":
                iteration_log += f"\n\n[direct] ruff output:\n{llm_feedback}"

            if not current_iteration_passed:
                print(f"[direct] attempt {attempt} did not pass ruff:\n{llm_feedback}")
                last_failure = "ruff"
                action_results["status"] = "ruff_fail"
                action_results["feedback"] = llm_feedback
                db.log_action(
                    job_id,
                    attempt,
                    action_llm_response,
                    action_results,
                    raw_stdout=iteration_log,
                )
                continue

            if llm_feedback != "SKIPPED":
                print(f"[direct] OK ruff passed on attempt {attempt}")
                action_results["ruff"] = "passed"

            # Read the ruff-formatted version (ruff may have modified it in-place)
            ruff_clean_code = output_path.read_text(encoding="utf-8")

            if test_path.exists():
                rc, pytest_output = run_pytest(test_path, workspace)
                iteration_log += (
                    f"\n\n[direct] internal pytest output:\n{pytest_output}"
                )

                if rc == 0:
                    print("[direct] internal pytest: all tests passed")
                    last_failure = None
                    job_succeeded_in_loop = True
                    action_results["status"] = "success"
                    db.log_action(
                        job_id,
                        attempt,
                        action_llm_response,
                        action_results,
                        raw_stdout=iteration_log,
                    )
                    break
                else:
                    print(f"[direct] internal pytest output:\n{pytest_output}")
                    summary = next(
                        (
                            ln
                            for ln in reversed(pytest_output.splitlines())
                            if "failed" in ln.lower()
                        ),
                        "tests failed",
                    )
                    print(f"[direct] internal pytest FAILED: {summary}")

                    failures = parse_pytest_failures(pytest_output)
                    current_failure_count = len(failures)
                    action_results["pytest_summary"] = summary
                    action_results["failed_tests_count"] = current_failure_count

                    # Best-so-far / regression prevention
                    if current_failure_count < best_failure_count:
                        best_failure_count = current_failure_count
                        best_code = ruff_clean_code
                    elif (
                        current_failure_count > best_failure_count
                        and best_code is not None
                    ):
                        print(
                            f"[direct] REGRESSION: {current_failure_count} failures > best "
                            f"{best_failure_count}. Restoring best code."
                        )
                        output_path.write_text(best_code, encoding="utf-8")
                        next_warnings.append(
                            f"WARNING: Your last attempt increased failures from "
                            f"{best_failure_count} to {current_failure_count}. "
                            f"The better version ({best_failure_count} failures) has been "
                            f"restored. Fix the remaining failures without breaking what was working."
                        )

                    # Diff check: flag minimal changes that didn't help
                    if existing_code and last_failure in ("single_test", "pytest"):
                        old_lines = existing_code.splitlines()
                        new_lines = ruff_clean_code.splitlines()
                        changed = [
                            ln
                            for ln in difflib.unified_diff(old_lines, new_lines)
                            if ln.startswith(("+", "-"))
                            and not ln.startswith(("+++", "---"))
                        ]
                        total_lines = max(len(old_lines), 1)
                        if len(changed) / total_lines < 0.05 and len(changed) < 5:
                            next_warnings.append(
                                f"WARNING: Your code changed by only {len(changed)} line(s) "
                                f"out of {total_lines}. The same tests are still failing. "
                                f"You need a fundamentally different approach — rewrite the "
                                f"relevant section from scratch."
                            )

                    if last_failure == "single_test":
                        if current_todo_test not in failures:
                            print(f"[direct] Fixed test: {current_todo_test}")
                            stuck_tracker.pop(
                                current_todo_test, None
                            )  # reset stuck history for this test
                            action_results["test_fixed"] = current_todo_test
                            if test_todo_list:
                                current_todo_test = test_todo_list.pop(0)
                                print(
                                    f"[direct] Moving to next test in todo list: {current_todo_test}"
                                )
                                last_failure = "single_test"
                            else:
                                print(
                                    "[direct] Todo list empty, but some tests still fail. Refreshing todo list."
                                )
                                test_todo_list = list(failures.keys())
                                current_todo_test = test_todo_list.pop(0)
                                last_failure = "single_test"
                        else:
                            print(
                                f"[direct] Test {current_todo_test} STILL failing. Retrying it."
                            )
                            last_failure = "single_test"
                    else:
                        all_tests_failure_count += 1
                        if all_tests_failure_count >= 1:
                            print("[direct] Switching to 'one test at a time' mode.")
                            test_todo_list = list(failures.keys())
                            if test_todo_list:
                                current_todo_test = test_todo_list.pop(0)
                                last_failure = "single_test"
                            else:
                                last_failure = "pytest"
                        else:
                            last_failure = "pytest"

                    action_results["status"] = last_failure
                    db.log_action(
                        job_id,
                        attempt,
                        action_llm_response,
                        action_results,
                        raw_stdout=iteration_log,
                    )

            else:
                print("[direct] no test file found, skipping internal pytest")
                last_failure = None
                job_succeeded_in_loop = True
                action_results["status"] = "success"
                action_results["note"] = "no test file - ruff only"
                db.log_action(
                    job_id,
                    attempt,
                    action_llm_response,
                    action_results,
                    raw_stdout=iteration_log,
                )
                break

        except Exception as e:
            print(f"[direct] EXCEPTION in iteration loop: {e}")
            action_results["status"] = "exception"
            action_results["error"] = str(e)
            db.log_action(
                job_id,
                attempt,
                action_llm_response,
                action_results,
                raw_stdout=iteration_log,
            )
            raise e

    # Hard failures (after loop finishes)
    if not job_succeeded_in_loop:
        if last_failure == "format_error":
            fail_job(
                db,
                job_id,
                f"LLM output format consistently invalid after {MAX_ITERATIONS} attempts",
                tag="direct",
            )
        elif last_failure == "ruff":
            fail_job(
                db,
                job_id,
                f"did not pass ruff checks after {MAX_ITERATIONS} iterations",
                tag="direct",
            )
        elif last_failure in ("pytest", "single_test"):
            fail_job(
                db,
                job_id,
                f"tests still failing after {MAX_ITERATIONS} iterations",
                tag="direct",
            )
        else:
            fail_job(
                db,
                job_id,
                f"job failed after {MAX_ITERATIONS} iterations for unknown reason",
                tag="direct",
            )

    # Success path
    output_label = "tests passed" if test_path.exists() else "no test file — ruff only"
    final_code = output_path.read_text(encoding="utf-8")
    print(f"[direct] final file: {output_path}  ({len(final_code)} chars)")
    complete_job(db, job_id, f"{output_label}\n{final_code}")


if __name__ == "__main__":
    main()
