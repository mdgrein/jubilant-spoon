#!/usr/bin/env python3
"""
Test generation harness.

Sends the task description to the LLM and asks it to write pytest tests.
The implementation does not exist yet -- tests are written first (TDD).

After the tests pass ruff, pytest is run against them. They are expected
to FAIL (the implementation doesn't exist yet). The harness exits 0 so
the dev job dependency is unblocked; the pytest failure is recorded in
job_output for reference.

Job prompt format (same as dev_harness.py):
    FILENAME: fibonacci.py

    Write a Python function called fibonacci(n)...

Output is written to workspace/tests/test_<filename>.
The harness exits 0 on success, 1 on failure.
"""

import json
import subprocess
import sys
from pathlib import Path
import ast
import inspect
from collections import deque
from typing import Dict, Any, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from dev_utils import detect_test_contradictions, Contradiction
from harness_common import (
    MAX_ITERATIONS,
    call_model,
    clean_output,
    complete_job,
    fail_job,
    generate_stub,
    load_job,
    log_prompt,
    parse_prompt,
    run_pytest,
    run_ruff,
)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _initial_prompt(module_name: str, task: str) -> str:
    guidelines = """
GUIDELINES:
- Focus on FEATURE coverage, not line coverage
- Test happy paths and error cases
- Consider edge cases and boundary conditions
- Test integration points between components
- Verify behavior matches original prompt
- Include both positive and negative test cases
- Think about user-facing scenarios
- Test the Module's Interface: Focus tests on functions *defined or explicitly re-exported* within the module under test. Avoid creating direct tests for functions imported from other modules if they are not part of this module's public API. Instead, test how this module *uses* such imported functions (e.g., through integration tests for wrapper functions like `run_all()`).
- Return Type Consistency: When testing functions, ensure test assertions align with the function's expected return type. If a function's description implies a single value, assert against single values. If a sequence (e.g., a list of numbers), assert against sequences. For Nth-item functions, expect a single item.
- String Manipulation Clarity: For string case conversion, understand how 'words' are defined for splitting (whitespace, case change, delimiters). Design test inputs and expected outputs to match this, e.g., 'HelloWorld' to 'hello-world' for kebab-case.
- Specific Exception Types: When testing invalid inputs, use `pytest.raises` for the most specific `Exception` type (e.g., `TypeError` for incorrect types, `ValueError` for invalid values).
"""
    return (
        "You are a Python test writer using pytest.\n"
        "Output ONLY raw Python code. No explanations, no markdown, no code fences.\n\n"
        "Write pytest tests for the following task. "
        "The implementation does NOT exist yet -- write the tests first.\n\n"
        f"The module being tested is '{module_name}'. "
        "Add this import block at the top of your test file so it can find the source:\n\n"
        "    import sys\n"
        "    from pathlib import Path\n"
        "    sys.path.insert(0, str(Path(__file__).parent.parent))\n\n"
        f"TASK:\n{task}\n\n"
        "Write thorough tests covering normal cases, edge cases, and invalid inputs. "
        "Use descriptive test function names (test_<what>_<condition>).\n"
        f"{guidelines}"
    )


def _retry_prompt(module_name: str, task: str, current_code: str, llm_feedback: str, is_format_failure: bool = False) -> str:
    guidelines = """
GUIDELINES:
- Focus on FEATURE coverage, not line coverage
- Test happy paths and error cases
- Consider edge cases and boundary conditions
- Test integration points between components
- Verify behavior matches original prompt
- Include both positive and negative test cases
- Think about user-facing scenarios
- Test the Module's Interface: Focus tests on functions *defined or explicitly re-exported* within the module under test. Avoid creating direct tests for functions imported from other modules if they are not part of this module's public API. Instead, test how this module *uses* such imported functions (e.g., through integration tests for wrapper functions like `run_all()`).
- Return Type Consistency: When testing functions, ensure test assertions align with the function's expected return type. If a function's description implies a single value, assert against single values. If a sequence (e.g., a list of numbers), assert against sequences. For Nth-item functions, expect a single item.
- String Manipulation Clarity: For string case conversion, understand how 'words' are defined for splitting (whitespace, case change, delimiters). Design test inputs and expected outputs to match this, e.g., 'HelloWorld' to 'hello-world' for kebab-case.
- Specific Exception Types: When testing invalid inputs, use `pytest.raises` for the most specific `Exception` type (e.g., `TypeError` for incorrect types, `ValueError` for invalid values).
"""
    base_prompt = (
        "You are a Python test writer using pytest.\n"
        "Output ONLY raw Python code. No explanations, no markdown, no code fences.\n\n"
    )

    if is_format_failure:
        return (
            base_prompt +
            f"URGENT: Your previous output was NOT valid Python code. "
            f"You MUST output ONLY raw Python code on STDOUT. "
            f"Do NOT include explanations, prose, or markdown formatting. "
            f"MODULE UNDER TEST: {module_name}\n"
            f"ORIGINAL TASK:\n{task}\n\n"
            f"ISSUES WITH PREVIOUS OUTPUT:\n{llm_feedback}\n\n"
            f"Please provide the complete, corrected Python file for the task, adhering strictly to the format."
            f"{guidelines}"
        )
    else:
        return (
            base_prompt +
            "CRITICAL: You are a TEST WRITER. Output ONLY pytest test code.\n"
            "DO NOT implement the module under test. DO NOT write the actual functions being tested.\n"
            "The test file below failed automated checks. Fix ALL reported issues and output "
            "the complete corrected test file -- not just the changed lines.\n\n"
            f"MODULE UNDER TEST: {module_name}\n"
            f"ORIGINAL TASK:\n{task}\n\n"
            f"CURRENT TEST CODE:\n{current_code}\n\n"
            f"ISSUES TO FIX:\n{llm_feedback}\n\n"
            f"{guidelines}"
        )


# ---------------------------------------------------------------------------
# AST-based test validation
# ---------------------------------------------------------------------------

def _get_function_required_args(module_path: Path, func_name: str) -> Optional[int]:
    """
    Parses a Python module to find a specific function's required positional arguments.
    Returns None if the function is not found or cannot be parsed.
    Returns the count of required positional arguments.
    Functions with *args are treated as having 0 required positional arguments for simplicity,
    as they can absorb any number of extra args.
    """
    if not module_path.exists():
        return None

    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                # Count positional arguments that do not have default values
                num_args = len(node.args.args) + len(node.args.posonlyargs)
                num_defaults = len(node.args.defaults)
                required_positional_args = num_args - num_defaults

                # If the function accepts *args, it can effectively take any number of positional args
                if node.args.vararg:
                    return 0 # Treat as 0 required for this simple validation purpose

                return required_positional_args
        
    except (SyntaxError, Exception):
        # Ignore modules with syntax errors or other parsing issues; return None
        pass
    return None

def _extract_imported_function_signatures(test_file_path: Path, workspace: Path) -> Dict[str, int]:
    """
    Extracts function imports from a test file and determines their required argument counts.
    Returns a dict of {function_name: required_arg_count}.
    """
    signatures: Dict[str, int] = {}
    
    try:
        test_tree = ast.parse(test_file_path.read_text(encoding="utf-8"))
    except (SyntaxError, FileNotFoundError):
        return signatures # Cannot parse test file or it doesn't exist

    for node in test_tree.body:
        if isinstance(node, ast.ImportFrom):
            # from <module> import <name> [as alias]
            module_name = node.module
            if module_name:
                module_path = workspace / f"{module_name}.py"
                if not module_path.exists():
                    continue # Cannot find the source module

                for alias in node.names:
                    func_name = alias.name
                    required_args = _get_function_required_args(module_path, func_name)
                    if required_args is not None:
                        signatures[alias.asname or func_name] = required_args
        elif isinstance(node, ast.Import):
            # import <module> [as alias]
            for alias in node.names:
                module_name = alias.name
                module_path = workspace / f"{module_name}.py"
                if not module_path.exists():
                    continue

                # If imported as 'import module', functions are called as 'module.func'
                # We don't extract individual func signatures here, will rely on ast.Attribute later
                pass # This is complex to resolve statically, might need dynamic inspection or a simpler heuristic

    return signatures

class FunctionCallValidator(ast.NodeVisitor):
    """AST visitor to find function calls and validate against known signatures."""
    def __init__(self, function_signatures: Dict[str, int]):
        self.function_signatures = function_signatures
        self.errors: List[str] = []

    def visit_Call(self, node: ast.Call):
        func_name = None
        if isinstance(node.func, ast.Name):
            # Direct call to an imported function, e.g., fibonacci()
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            # Call to a function from an imported module, e.g., all_join_in.fibonacci()
            if isinstance(node.func.value, ast.Name):
                # Assuming 'all_join_in' is directly available in the test's scope
                # and its functions are in self.function_signatures.
                # For now, let's just consider the func.attr name.
                func_name = node.func.attr
        
        if func_name and func_name in self.function_signatures:
            expected_args = self.function_signatures[func_name]
            actual_args = len(node.args) # Only positional args for now

            if actual_args < expected_args:
                # Get line number for better feedback
                self.errors.append(
                    f"Line {node.lineno}: Function '{func_name}' called with {actual_args} "
                    f"positional arguments, but requires at least {expected_args}. "
                    "This likely causes a TypeError."
                )
            elif expected_args == 0 and actual_args > 0 and func_name in ["fibonacci", "binary_sort", "convert_case"]: # Simple heuristic for functions that should take args
                 # This is a very rough heuristic, for specific functions that we know require args,
                 # but _get_function_required_args returned 0 (e.g., due to *args handling).
                 # This is tricky without knowing the exact module.
                 # For now, let's focus on the 'missing' argument case.
                 pass

        self.generic_visit(node) # Continue visiting child nodes


def _validate_test_function_calls(test_file_path: Path, workspace: Path) -> List[str]:
    """
    Validates function calls in a test file against the signatures of imported functions.
    Returns a list of error strings.
    """
    errors: List[str] = []

    # First, build a map of available functions and their required arg counts
    # This requires parsing the test file to find imports, then parsing those modules.
    
    # This is simplified: assume test imports from top-level files in workspace
    # (e.g., `from fibonacci import fibonacci` implies `workspace/fibonacci.py`)
    
    # We need to extract all unique modules imported by the test file
    imported_modules: Dict[str, Path] = {} # {module_name: module_path}
    try:
        test_tree_imports = ast.parse(test_file_path.read_text(encoding="utf-8"))
        for node in test_tree_imports.body:
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    module_name = node.module
                    # Check if it's a relative import like '.module'
                    if module_name.startswith('.'):
                        # This would mean the module is in the same directory as the test file
                        # This needs to be handled carefully or assume absolute imports for simplicity for now
                        pass
                    else:
                        module_path = workspace / f"{module_name}.py"
                        if module_path.exists():
                            imported_modules[module_name] = module_path
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module_name = alias.name
                    module_path = workspace / f"{module_name}.py"
                    if module_path.exists():
                        imported_modules[module_name] = module_path
    except (SyntaxError, FileNotFoundError, Exception):
        errors.append(f"Could not parse imports from test file '{test_file_path.name}'.")
        return errors

    # Now, for each imported function, get its signature
    function_signatures: Dict[str, int] = {} # {func_name: required_args}
    for module_name, module_path in imported_modules.items():
        try:
            module_tree = ast.parse(module_path.read_text(encoding="utf-8"))
            for node in module_tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Get required args for each function in the imported module
                    required_args = _get_function_required_args(module_path, node.name)
                    if required_args is not None:
                        # Store both direct imports (func_name) and potentially module.func_name
                        # For simplicity, store func_name directly for now,
                        # assuming direct imports like 'from module import func'
                        function_signatures[node.name] = required_args
        except (SyntaxError, Exception):
            pass # Ignore modules with parsing errors

    # Now, validate calls within the test file
    try:
        test_tree_calls = ast.parse(test_file_path.read_text(encoding="utf-8"))
        validator = FunctionCallValidator(function_signatures)
        validator.visit(test_tree_calls)
        errors.extend(validator.errors)
    except (SyntaxError, FileNotFoundError, Exception) as e:
        errors.append(f"Error validating function calls in '{test_file_path.name}': {e}")
    
    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: test_harness.py <job_id>")
        sys.exit(1)

    job_id = sys.argv[1]
    db, job = load_job(job_id)

    prompt_text = job["prompt"]
    allowed_paths = json.loads(job["allowed_paths"])
    workspace = Path(allowed_paths[0]).resolve()
    vendor = job["vendor"] or "local-ollama"
    model = job["model"]

    filename, task = parse_prompt(prompt_text)
    if not filename:
        fail_job(db, job_id, "prompt missing FILENAME: line", tag="test")

    module_name = Path(filename).stem          # "fibonacci"
    test_filename = f"test_{filename}"          # "test_fibonacci.py"
    output_path = workspace / "tests" / test_filename
    output_path.parent.mkdir(parents=True, exist_ok=True)

    stub_path = workspace / filename
    # If a real implementation already exists, don't overwrite it (manual retry).
    real_impl_exists = stub_path.exists()
    stub_written = False

    llm_feedback = ""
    pytest_output = ""
    last_failure = None   # None | 'ruff' | 'pytest' | 'format_error'
    job_succeeded_in_loop = False

    for attempt in range(1, MAX_ITERATIONS + 1):
        print(f"\n\n{'='*80}")
        print(f"  ITERATION {attempt}/{MAX_ITERATIONS}  |  Job: {job_id[:8]}  |  File: tests/{test_filename}")
        print(f"{'='*80}\n")

        # Update job iteration in DB for live UI progress
        ts = db._timestamp()
        db.conn.execute("UPDATE jobs SET iteration = ?, updated_at = ? WHERE job_id = ?", (attempt, ts, job_id))
        db.conn.commit()

        existing_code = output_path.read_text(encoding="utf-8") if output_path.exists() else None

        # Per-iteration tracking
        action_results = {}
        action_llm_response = {}
        raw = ""

        if last_failure is None:
            model_prompt = _initial_prompt(module_name, task)
        elif last_failure == "ruff":
            model_prompt = _retry_prompt(module_name, task, existing_code, llm_feedback)
        elif last_failure == "format_error":
            # Use existing_code (last successfully-written version); don't re-read a bad file
            model_prompt = _retry_prompt(module_name, task, existing_code or "", llm_feedback, is_format_failure=True)
        else:  # 'pytest'
            model_prompt = _retry_prompt(module_name, task, existing_code, pytest_output)

        log_prompt(model_prompt, tag="test")
        try:
            raw = call_model(model_prompt, vendor=vendor, model=model)
            iteration_log = (
                f"[test] --- PROMPT ({len(model_prompt)} chars) ---\n"
                f"{model_prompt}\n"
                f"[test] --- END PROMPT ---\n\n"
                f"{raw}"
            )
        except subprocess.TimeoutExpired:
            fail_job(db, job_id, "timeout calling ollama", tag="test")

        code = clean_output(raw)
        action_llm_response["code"] = code

        # Format check BEFORE writing to disk — don't corrupt the test file with non-Python output
        format_check_failed = False
        if not code.strip():
            print("[test] LLM produced empty output after cleaning.")
            llm_feedback = "Your output is empty after cleaning. You must provide raw Python code."
            format_check_failed = True
        else:
            try:
                ast.parse(code)
            except SyntaxError as e:
                print(f"[test] LLM output is not valid Python code: {e}")
                llm_feedback = (
                    f"Your output must consist ONLY of valid raw Python code. "
                    f"Do NOT include any explanations, prose, markdown formatting, or non-code text in your STDOUT. "
                    f"Specifically, a SyntaxError was found: {e}"
                )
                format_check_failed = True
            except Exception as e:
                print(f"[test] LLM output is not valid Python code (parsing error): {e}")
                llm_feedback = (
                    f"Your output must consist ONLY of valid raw Python code. "
                    f"An unexpected parsing error occurred: {type(e).__name__}: {e}. "
                    f"Ensure your output is pure Python."
                )
                format_check_failed = True

        if format_check_failed:
            last_failure = "format_error"
            action_results["status"] = "format_error"
            action_results["feedback"] = llm_feedback
            db.log_action(job_id, attempt, action_llm_response, action_results, raw_stdout=iteration_log)
            continue

        # Valid Python — write to disk
        output_path.write_text(code, encoding="utf-8")
        print(f"[test] wrote {len(code)} chars")
        last_failure = None

        current_iteration_passed, llm_feedback = run_ruff(output_path, tag="test")
        if llm_feedback and llm_feedback != "SKIPPED":
            iteration_log += f"\n\n[test] ruff output:\n{llm_feedback}"

        if not current_iteration_passed:
            print(f"[test] attempt {attempt} did not pass ruff:\n{llm_feedback}")
            last_failure = "ruff"
            action_results["status"] = "ruff_fail"
            action_results["feedback"] = llm_feedback
            db.log_action(job_id, attempt, action_llm_response, action_results, raw_stdout=iteration_log)
            continue

        if llm_feedback != "SKIPPED":
            print(f"[test] OK ruff passed on attempt {attempt}")

        # AST-based test validation
        ast_errors = _validate_test_function_calls(output_path, workspace)
        if ast_errors:
            print(f"[test] AST validation found issues:\n" + "\n".join(ast_errors))
            llm_feedback = (
                f"AST validation of generated test code found issues:\n"
                f"{'; '.join(ast_errors)}\n\n"
                f"Please fix these errors in the test code."
            )
            last_failure = "ruff"
            action_results["status"] = "ast_error"
            action_results["feedback"] = llm_feedback
            db.log_action(job_id, attempt, action_llm_response, action_results, raw_stdout=iteration_log)
            continue

        # Contradiction check: same call, conflicting expected outcomes
        contradictions = detect_test_contradictions(output_path)
        if contradictions:
            print(f"[test] {len(contradictions)} contradiction(s) detected")
            lines = [
                f"  {c.call_repr}: '{c.test_a}' expects {c.outcome_a}"
                f"  vs  '{c.test_b}' expects {c.outcome_b}"
                for c in contradictions
            ]
            llm_feedback = (
                "CONTRADICTION(S) DETECTED: The following tests assert conflicting outcomes "
                "for the same call. A single implementation cannot satisfy both. "
                "Choose one consistent behaviour and remove or rewrite the conflicting test:\n\n"
                + "\n".join(lines)
            )
            last_failure = "ruff"   # re-use the existing retry flow
            action_results["status"] = "contradiction"
            action_results["feedback"] = llm_feedback
            db.log_action(job_id, attempt, action_llm_response, action_results, raw_stdout=iteration_log)
            continue

        # Write a minimal stub so pytest can actually import the module and
        # collect the tests. Catches wrong function names / missing exports
        # that ruff cannot detect. Regenerated each attempt since the test
        # file may have changed imports.
        if not real_impl_exists:
            stub_code = generate_stub(output_path, module_name)
            if stub_code:
                stub_path.write_text(stub_code, encoding="utf-8")
                stub_written = True
                print(f"[test] wrote stub: {stub_path}  ({len(stub_code)} chars)")
            else:
                print("[test] WARNING: generate_stub returned empty (syntax error?); skipping stub")
        else:
            print(f"[test] stub skipped — {stub_path.name} already exists (manual retry?)")

        # Run pytest with the stub in place.
        # returncode 0 = all passed (unlikely — stub raises NotImplementedError)
        # returncode 1 = tests collected, some/all failed — expected in TDD
        # returncode 2+ = collection error even with stub → bad test file → retry
        print("[test] running pytest with stub to validate test collection...")
        returncode, pytest_output = run_pytest(output_path, workspace)
        iteration_log += f"\n\n[test] pytest output:\n{pytest_output}"

        for line in pytest_output.splitlines()[-20:]:
            print(f"[test]   {line}")

        if returncode >= 2:
            # Collection failed even with the stub — bad import name, wrong
            # function name, or other structural problem.
            print(f"[test] collection error (returncode {returncode}) — test file needs regeneration")
            llm_feedback = (
                f"pytest collection failed even with a minimal stub (returncode {returncode}).\n"
                f"This means the test file imports a name that does not exist in {module_name}, "
                f"or has another structural problem.\n\n"
                f"COLLECTION OUTPUT:\n{pytest_output[-3000:]}"
            )
            last_failure = "ruff"
            action_results["status"] = "collection_error"
            action_results["feedback"] = llm_feedback
            db.log_action(job_id, attempt, action_llm_response, action_results, raw_stdout=iteration_log)
            continue

        if returncode == 0:
            label = "all passed (stub may be incomplete)"
        else:
            label = "tests collected, failures expected in TDD"
        print(f"[test] pytest {label}")
        action_results["status"] = "success"
        db.log_action(job_id, attempt, action_llm_response, action_results, raw_stdout=iteration_log)
        job_succeeded_in_loop = True
        break

    # Hard failures (after loop finishes)
    if not job_succeeded_in_loop:
        if last_failure == "format_error":
            fail_job(db, job_id, f"LLM output format consistently invalid after {MAX_ITERATIONS} attempts", tag="test")
        elif last_failure == "ruff":
            fail_job(db, job_id, f"test validation failed (ruff or AST) after {MAX_ITERATIONS} attempts", tag="test")
        elif last_failure == "pytest":
            fail_job(db, job_id, f"tests still failing after {MAX_ITERATIONS} attempts", tag="test")
        else:
            fail_job(db, job_id, f"job failed after {MAX_ITERATIONS} attempts for unknown reason", tag="test")

    # Success path
    final_code = output_path.read_text(encoding="utf-8")
    print(f"[test] final file: {output_path}  ({len(final_code)} chars)")
    if stub_written:
        print(f"[test] stub remains at {stub_path} for dev job to overwrite")
    complete_job(db, job_id, final_code)


if __name__ == "__main__":
    main()
