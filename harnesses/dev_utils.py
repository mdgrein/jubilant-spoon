from enum import Enum
from dataclasses import dataclass
import re
import ast
from collections import defaultdict
from pathlib import Path


class FailureCategory(Enum):
    MISSING_SYMBOL = "missing_symbol"
    IMPORT_ERROR = "import_error"
    SYNTAX_ERROR = "syntax_error"
    TYPE_ERROR = "type_error"
    ASSERTION_MISMATCH = "assertion_mismatch"
    RUNTIME_EXCEPTION = "runtime_exception"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass
class FailureInfo:
    category: FailureCategory
    symbol: str | None = None
    message: str | None = None

    def to_hint(self) -> str:
        """Produce a short hint for the LLM based on the failure category."""
        if self.category == FailureCategory.MISSING_SYMBOL:
            return f"TIP: The symbol '{self.symbol}' is missing. Check your function/class names."
        if self.category == FailureCategory.IMPORT_ERROR:
            return (
                "TIP: There is an import error. Check your imports and file structure."
            )
        if self.category == FailureCategory.SYNTAX_ERROR:
            return "TIP: There is a syntax error. Double-check your code's structure."
        if self.category == FailureCategory.TYPE_ERROR:
            return "TIP: There is a type error. Check your function signatures and call sites."
        if self.category == FailureCategory.ASSERTION_MISMATCH:
            return "TIP: An assertion failed. The logic doesn't match the test expectations."
        if self.category == FailureCategory.RUNTIME_EXCEPTION:
            return "TIP: A runtime exception occurred. Check the traceback for clues."
        if self.category == FailureCategory.TIMEOUT:
            return "TIP: The execution timed out. Check for infinite loops or very slow logic."
        return "TIP: The tests failed. Review the output below and adjust your implementation."


@dataclass
class ParsedFailure:
    test_name: str
    traceback: str
    file_path: str | None = None
    line_number: int | None = None


def classify_failure(test_output: str) -> FailureInfo:
    """
    Categorize pytest failures based on error messages in the output.
    """
    # 1. Missing Symbol (AttributeError)
    # Match: "AttributeError: module 'case_converter' has no attribute 'snake_to_camel'"
    match = re.search(
        r"AttributeError: (?:module|object) '?.+'? has no attribute '(.+)'", test_output
    )
    if match:
        return FailureInfo(
            category=FailureCategory.MISSING_SYMBOL,
            symbol=match.group(1),
            message=match.group(0),
        )

    # 2. Import Error
    if "ImportError" in test_output or "ModuleNotFoundError" in test_output:
        return FailureInfo(category=FailureCategory.IMPORT_ERROR)

    # 3. Syntax Error
    if "SyntaxError" in test_output:
        return FailureInfo(category=FailureCategory.SYNTAX_ERROR)

    # 4. Type Error
    if "TypeError" in test_output:
        return FailureInfo(category=FailureCategory.TYPE_ERROR)

    # 5. Assertion Mismatch
    if "AssertionError" in test_output:
        return FailureInfo(category=FailureCategory.ASSERTION_MISMATCH)

    # 6. Timeout
    if "Timeout" in test_output:
        return FailureInfo(category=FailureCategory.TIMEOUT)

    # 7. Runtime Exception (Generic Traceback)
    if "Traceback" in test_output:
        return FailureInfo(category=FailureCategory.RUNTIME_EXCEPTION)

    return FailureInfo(category=FailureCategory.UNKNOWN)


def parse_pytest_failures(pytest_output: str) -> dict[str, ParsedFailure]:
    """
    Parses pytest output and returns a mapping of test name to its ParsedFailure.
    Specifically looks for the section between 'FAILURES' and 'short test summary info'.
    """
    failures = {}
    lines = pytest_output.splitlines()

    current_test = None
    current_failure_lines = []
    current_file = None
    current_line = None

    in_failures_section = False

    for line in lines:
        if "=== FAILURES ===" in line:
            in_failures_section = True
            continue
        if "=== short test summary info ===" in line:
            if current_test:
                failures[current_test] = ParsedFailure(
                    test_name=current_test,
                    traceback="\n".join(current_failure_lines).strip(),
                    file_path=current_file,
                    line_number=current_line,
                )
            in_failures_section = False
            break

        if not in_failures_section:
            continue

        # Match "________________ test_name ________________"
        match = re.match(r"_{10,}\s+(.+)\s+_{10,}", line)
        if match:
            if current_test:
                failures[current_test] = ParsedFailure(
                    test_name=current_test,
                    traceback="\n".join(current_failure_lines).strip(),
                    file_path=current_file,
                    line_number=current_line,
                )
            current_test = match.group(1)
            current_failure_lines = []
            current_file = None
            current_line = None
        else:
            if current_test:
                current_failure_lines.append(line)
                # Try to extract file and line: "tests\test_file.py:123: in test_func"
                file_match = re.search(r"([^\s:]+\.py):(\d+): in", line)
                if file_match and current_file is None:
                    current_file = file_match.group(1)
                    current_line = int(file_match.group(2))

    return failures


def format_failure_for_llm(
    test_name: str, traceback: str, test_code: str | None = None
) -> str:
    """
    Summarize a pytest failure into a more human-readable/model-readable format.
    """
    lines = traceback.splitlines()

    # Try to find the line that actually failed in the test
    failing_code_line = ""
    error_detail = ""

    for i, line in enumerate(lines):
        if line.startswith("E   "):
            error_detail = line[4:].strip()

            # Find the actual source line. Sometimes the line immediately before 'E'
            # is just a pointer line like '^^^^^^^^^^^^^^^^'
            j = i - 1
            while j >= 0:
                candidate = lines[j].strip()
                # Skip pointer lines and empty lines
                if candidate and not all(c in "^ " for c in candidate):
                    failing_code_line = candidate
                    break
                j -= 1
            break

    if not error_detail:
        return "The code failed with an unknown error."

    # 1. Handle "DID NOT RAISE" errors
    # E   Failed: DID NOT RAISE <class 'ValueError'>
    match_did_not_raise = re.search(
        r"Failed: DID NOT RAISE <class '(.+)'>", error_detail
    )
    if match_did_not_raise:
        exc_type = match_did_not_raise.group(1).split(".")[
            -1
        ]  # Get short name like 'ValueError'

        # HEURISTIC: If the failing line is a 'with pytest.raises' block, try to find the actual call inside it
        if failing_code_line and "pytest.raises" in failing_code_line and test_code:
            test_lines = test_code.splitlines()
            for i, line in enumerate(test_lines):
                if failing_code_line in line:
                    # Check if it's a one-liner: 'with pytest.raises(V): func()'
                    if ":" in line:
                        after_colon = line.split(":", 1)[1].strip()
                        if after_colon:
                            return f"The call `{after_colon}` should have raised a `{exc_type}`, but it did not."

                    # Check next non-empty line(s) for the indented body
                    with_indent = len(line) - len(line.lstrip())
                    for j in range(i + 1, len(test_lines)):
                        next_line = test_lines[j]
                        if not next_line.strip():
                            continue
                        next_indent = len(next_line) - len(next_line.lstrip())
                        if next_indent > with_indent:
                            # This is likely the call that was supposed to raise
                            inside_call = next_line.strip()
                            return f"The call `{inside_call}` should have raised a `{exc_type}`, but it did not."
                        else:
                            break  # End of block

        if failing_code_line:
            return f"The call `{failing_code_line}` should have raised a `{exc_type}`, but it did not."
        else:
            return f"The code should have raised a `{exc_type}`, but it did not."

    # 2. Handle specific assertion failure details (e.g., assert 'A' == 'B')
    match_assert = re.search(r"AssertionError: assert (.*) == (.*)", error_detail)
    if match_assert:
        actual = match_assert.group(1).strip()
        expected = match_assert.group(2).strip()

        if failing_code_line:
            # e.g., failing_code_line = "assert my_func(10) == 20"
            return f"The call `{failing_code_line}` resulted in {actual}, but it was expected to be {expected}."
        else:
            return (
                f"The code produced {actual} but it was expected to produce {expected}."
            )

    # 3. Handle membership assertions (assert needle in haystack)
    # Pytest formats this as: AssertionError: assert 'needle' in 'long output...'
    # The haystack is already visible in the FAILING TESTS OUTPUT section of the
    # prompt, so suppress it here and just name what was missing.
    match_in = re.search(r"AssertionError: assert (.+?) in \S", error_detail)
    if match_in:
        needle = match_in.group(1).strip()
        if failing_code_line:
            return (
                f"`{failing_code_line}` failed: "
                f"expected the output to contain {needle}, but it did not."
            )
        return f"Expected the output to contain {needle}, but it did not."

    # No recognised pattern — return the raw error detail so the model still
    # gets something useful rather than the string "None".
    if failing_code_line:
        return f"`{failing_code_line}` failed: {error_detail}"
    return error_detail


@dataclass
class Contradiction:
    test_a: str  # test function name containing outcome_a
    test_b: str  # test function name containing outcome_b
    call_repr: str  # e.g. "fibonacci(0)"
    outcome_a: str  # e.g. "== 0"
    outcome_b: str  # e.g. "raises ValueError"


def _collect_test_assertions(func_node: ast.FunctionDef) -> list[tuple[str, str]]:
    """
    Walk the AST of a test function and return (call_repr, outcome_repr) pairs for:
      - `assert f(x) == v`  -> ("f(x)", "== v")
      - `with pytest.raises(E): f(x)` -> ("f(x)", "raises E")
    Ignores all other assertion forms.
    """
    results: list[tuple[str, str]] = []

    for node in ast.walk(func_node):
        # Pattern 1: assert f(x) == v
        if isinstance(node, ast.Assert):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and isinstance(test.left, ast.Call)
            ):
                call_repr = ast.unparse(test.left)
                val_repr = f"== {ast.unparse(test.comparators[0])}"
                results.append((call_repr, val_repr))

        # Pattern 2: with pytest.raises(E): f(x)
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if (
                    isinstance(ctx, ast.Call)
                    and isinstance(ctx.func, ast.Attribute)
                    and ctx.func.attr == "raises"
                    and isinstance(ctx.func.value, ast.Name)
                    and ctx.func.value.id == "pytest"
                    and ctx.args
                ):
                    exc_repr = ast.unparse(ctx.args[0])
                    # Collect direct calls in the with body
                    for stmt in node.body:
                        if isinstance(stmt, ast.Expr) and isinstance(
                            stmt.value, ast.Call
                        ):
                            call_repr = ast.unparse(stmt.value)
                            results.append((call_repr, f"raises {exc_repr}"))

    return results


def extract_all_io_examples(test_file_path: Path) -> list[tuple[str, str]]:
    """
    Return all (call_repr, outcome_repr) assertion pairs found across every
    `test_*` function in the file.

    Reuses `_collect_test_assertions` so the same two patterns are recognised:
      - `assert f(x) == v`         → ("f(x)", "== v")
      - `with pytest.raises(E): f(x)` → ("f(x)", "raises E")

    Returns `[]` on SyntaxError (file unreadable or not yet written).
    Duplicates are removed; order is preserved (first occurrence wins).
    """
    try:
        tree = ast.parse(test_file_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    seen: set[tuple[str, str]] = set()
    results: list[tuple[str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            for pair in _collect_test_assertions(node):
                if pair not in seen:
                    seen.add(pair)
                    results.append(pair)

    return results


def detect_test_contradictions(test_file_path: Path) -> list[Contradiction]:
    """
    Parse a test file and return a list of Contradiction instances where two
    different test functions assert conflicting outcomes for the same call.
    """
    try:
        tree = ast.parse(test_file_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    # {call_repr: [(outcome_repr, func_name), ...]}
    by_call: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            for call_repr, outcome_repr in _collect_test_assertions(node):
                by_call[call_repr].append((outcome_repr, node.name))

    contradictions: list[Contradiction] = []
    seen: set[tuple[str, str, str, str]] = set()

    for call_repr, entries in by_call.items():
        # Check all pairs for differing outcomes
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                outcome_a, func_a = entries[i]
                outcome_b, func_b = entries[j]
                if outcome_a == outcome_b:
                    continue
                key = (
                    call_repr,
                    *sorted([f"{func_a}:{outcome_a}", f"{func_b}:{outcome_b}"]),
                )
                if key in seen:
                    continue
                seen.add(key)
                contradictions.append(
                    Contradiction(
                        test_a=func_a,
                        test_b=func_b,
                        call_repr=call_repr,
                        outcome_a=outcome_a,
                        outcome_b=outcome_b,
                    )
                )

    return contradictions


def get_test_code_by_name(test_file: Path, test_name: str) -> str:
    """
    Find a test function by name in the test file using AST and return its source.
    This is the preferred lookup method — it doesn't depend on parsing pytest output.
    """
    if not test_file.exists():
        return f"# [Test file not found: {test_file}]"
    try:
        source = test_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
        lines = source.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == test_name:
                start = node.lineno - 1  # ast lineno is 1-based
                end = node.end_lineno  # end_lineno is inclusive
                return "\n".join(lines[start:end])
        return f"# [Test function '{test_name}' not found in {test_file.name}]"
    except Exception as e:
        return f"# [Error reading test code: {e}]"


def get_test_code(file_path: str, line_number: int, workspace_root: Path) -> str:
    """
    Reads the test file and extracts the function containing the given line number.
    Returns a string of the test function code.
    """
    full_path = workspace_root / file_path
    if not full_path.exists():
        return f"# [Error: Test file not found at {full_path}]"

    try:
        lines = full_path.read_text(encoding="utf-8").splitlines()
        if not (1 <= line_number <= len(lines)):
            return f"# [Error: Line {line_number} out of bounds in {file_path}]"

        # Look backwards for the 'def test_' line
        start_idx = -1
        for i in range(line_number - 1, -1, -1):
            if lines[i].strip().startswith("def test_"):
                start_idx = i
                break

        if start_idx == -1:
            # Fallback: just return 5 lines around the error
            s = max(0, line_number - 5)
            e = min(len(lines), line_number + 5)
            return "\n".join(lines[s:e])

        # Find the end of the function (until next 'def' or end of file)
        # Note: This is a simple approximation.
        end_idx = len(lines)
        for i in range(start_idx + 1, len(lines)):
            if lines[i].strip().startswith("def ") or lines[i].strip().startswith(
                "class "
            ):
                # If the line is at the same or lower indentation than the start, it's a new function
                start_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())
                current_indent = len(lines[i]) - len(lines[i].lstrip())
                if current_indent <= start_indent:
                    end_idx = i
                    break

        return "\n".join(lines[start_idx:end_idx]).strip()
    except Exception as e:
        return f"# [Error reading test code: {e}]"
