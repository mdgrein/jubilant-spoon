"""
Unit tests for dev_utils.py — contradiction detection and I/O example extraction.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))

from dev_utils import Contradiction, detect_test_contradictions, extract_all_io_examples


class TestDetectTestContradictions:
    # ------------------------------------------------------------------
    # Basic non-contradiction cases
    # ------------------------------------------------------------------

    def test_returns_empty_for_no_test_functions(self, tmp_path):
        f = tmp_path / "test_empty.py"
        f.write_text("def helper():\n    pass\n", encoding="utf-8")
        assert detect_test_contradictions(f) == []

    def test_returns_empty_when_no_qualifying_assertions(self, tmp_path):
        """assert True / assert x forms are ignored — no call on the left."""
        f = tmp_path / "test_noop.py"
        f.write_text(
            "def test_a():\n"
            "    assert True\n"
            "def test_b():\n"
            "    assert 1 == 1\n",
            encoding="utf-8",
        )
        assert detect_test_contradictions(f) == []

    def test_returns_empty_for_different_calls(self, tmp_path):
        """Different call arguments → not the same call → no contradiction."""
        f = tmp_path / "test_diff_args.py"
        f.write_text(
            "def test_a():\n"
            "    assert fibonacci(0) == 0\n"
            "\n"
            "def test_b():\n"
            "    assert fibonacci(1) == 1\n",
            encoding="utf-8",
        )
        assert detect_test_contradictions(f) == []

    def test_returns_empty_when_same_outcome_in_two_functions(self, tmp_path):
        """Same call, same expected value → agreement, not contradiction."""
        f = tmp_path / "test_agree.py"
        f.write_text(
            "def test_a():\n"
            "    assert fibonacci(0) == 0\n"
            "\n"
            "def test_b():\n"
            "    assert fibonacci(0) == 0\n",
            encoding="utf-8",
        )
        assert detect_test_contradictions(f) == []

    def test_returns_empty_on_syntax_error(self, tmp_path):
        """Unparseable file → graceful empty result, not an exception."""
        f = tmp_path / "test_bad.py"
        f.write_text("def broken(\n", encoding="utf-8")
        assert detect_test_contradictions(f) == []

    def test_non_test_functions_are_ignored(self, tmp_path):
        """Assertions in helper() functions must not contribute to detection."""
        f = tmp_path / "test_helper.py"
        f.write_text(
            "def helper():\n"
            "    assert fibonacci(0) == 0\n"
            "\n"
            "def test_a():\n"
            "    assert fibonacci(0) == 0\n",
            encoding="utf-8",
        )
        assert detect_test_contradictions(f) == []

    # ------------------------------------------------------------------
    # Contradiction detection — assert vs assert
    # ------------------------------------------------------------------

    def test_detects_conflicting_assert_values(self, tmp_path):
        """Two tests asserting the same call == different values → contradiction."""
        f = tmp_path / "test_vals.py"
        f.write_text(
            "def test_a():\n"
            "    assert fibonacci(5) == 5\n"
            "\n"
            "def test_b():\n"
            "    assert fibonacci(5) == 8\n",
            encoding="utf-8",
        )
        result = detect_test_contradictions(f)
        assert len(result) == 1
        c = result[0]
        assert c.call_repr == "fibonacci(5)"
        assert c.outcome_a == "== 5"
        assert c.outcome_b == "== 8"
        assert {c.test_a, c.test_b} == {"test_a", "test_b"}

    # ------------------------------------------------------------------
    # Contradiction detection — assert vs pytest.raises
    # ------------------------------------------------------------------

    def test_detects_assert_vs_raises(self, tmp_path):
        """assert f(x) == v in one test vs pytest.raises in another → contradiction."""
        f = tmp_path / "test_contra.py"
        f.write_text(
            "import pytest\n"
            "def test_a():\n"
            "    assert fibonacci(0) == 0\n"
            "\n"
            "def test_b():\n"
            "    with pytest.raises(ValueError):\n"
            "        fibonacci(0)\n",
            encoding="utf-8",
        )
        result = detect_test_contradictions(f)
        assert len(result) == 1
        c = result[0]
        assert c.call_repr == "fibonacci(0)"
        assert c.outcome_a == "== 0"
        assert c.outcome_b == "raises ValueError"
        assert c.test_a == "test_a"
        assert c.test_b == "test_b"

    def test_detects_raises_vs_assert_order_independent(self, tmp_path):
        """Detection works regardless of which test function appears first in the file."""
        f = tmp_path / "test_order.py"
        f.write_text(
            "import pytest\n"
            "def test_raises_first():\n"
            "    with pytest.raises(TypeError):\n"
            "        convert(None)\n"
            "\n"
            "def test_assert_second():\n"
            "    assert convert(None) == ''\n",
            encoding="utf-8",
        )
        result = detect_test_contradictions(f)
        assert len(result) == 1
        assert result[0].call_repr == "convert(None)"

    # ------------------------------------------------------------------
    # Multiple contradictions
    # ------------------------------------------------------------------

    def test_detects_multiple_independent_contradictions(self, tmp_path):
        """Two separate calls each contradicted → two Contradiction objects."""
        f = tmp_path / "test_multi.py"
        f.write_text(
            "import pytest\n"
            "def test_a():\n"
            "    assert foo(1) == 1\n"
            "    assert bar(2) == 2\n"
            "\n"
            "def test_b():\n"
            "    with pytest.raises(ValueError):\n"
            "        foo(1)\n"
            "    with pytest.raises(TypeError):\n"
            "        bar(2)\n",
            encoding="utf-8",
        )
        result = detect_test_contradictions(f)
        assert len(result) == 2
        calls = {c.call_repr for c in result}
        assert "foo(1)" in calls
        assert "bar(2)" in calls

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def test_deduplicates_repeated_pattern(self, tmp_path):
        """Same assertion repeated multiple times in both functions → one Contradiction."""
        f = tmp_path / "test_dup.py"
        f.write_text(
            "import pytest\n"
            "def test_a():\n"
            "    assert fibonacci(0) == 0\n"
            "    assert fibonacci(0) == 0\n"
            "\n"
            "def test_b():\n"
            "    with pytest.raises(ValueError):\n"
            "        fibonacci(0)\n"
            "    with pytest.raises(ValueError):\n"
            "        fibonacci(0)\n",
            encoding="utf-8",
        )
        result = detect_test_contradictions(f)
        assert len(result) == 1

    # ------------------------------------------------------------------
    # Contradiction dataclass fields
    # ------------------------------------------------------------------

    def test_contradiction_fields_are_populated(self, tmp_path):
        f = tmp_path / "test_fields.py"
        f.write_text(
            "import pytest\n"
            "def test_zero_is_zero():\n"
            "    assert fib(0) == 0\n"
            "\n"
            "def test_zero_raises():\n"
            "    with pytest.raises(ValueError):\n"
            "        fib(0)\n",
            encoding="utf-8",
        )
        result = detect_test_contradictions(f)
        assert len(result) == 1
        c = result[0]
        assert isinstance(c, Contradiction)
        assert c.call_repr == "fib(0)"
        assert c.test_a == "test_zero_is_zero"
        assert c.outcome_a == "== 0"
        assert c.test_b == "test_zero_raises"
        assert c.outcome_b == "raises ValueError"


class TestExtractAllIoExamples:
    def test_returns_empty_for_no_test_functions(self, tmp_path):
        f = tmp_path / "test_empty.py"
        f.write_text("def helper(): pass\n", encoding="utf-8")
        assert extract_all_io_examples(f) == []

    def test_returns_empty_on_syntax_error(self, tmp_path):
        f = tmp_path / "test_bad.py"
        f.write_text("def broken(\n", encoding="utf-8")
        assert extract_all_io_examples(f) == []

    def test_returns_empty_when_no_qualifying_assertions(self, tmp_path):
        f = tmp_path / "test_noop.py"
        f.write_text(
            "def test_a():\n    assert True\ndef test_b():\n    assert 1 == 1\n",
            encoding="utf-8",
        )
        assert extract_all_io_examples(f) == []

    def test_collects_assert_eq_pairs(self, tmp_path):
        f = tmp_path / "test_asserts.py"
        f.write_text(
            "def test_lower():\n"
            "    assert convert('Hello', 'lower') == 'hello'\n"
            "def test_upper():\n"
            "    assert convert('hello', 'upper') == 'HELLO'\n",
            encoding="utf-8",
        )
        result = extract_all_io_examples(f)
        assert len(result) == 2
        calls = {call for call, _ in result}
        assert "convert('Hello', 'lower')" in calls
        assert "convert('hello', 'upper')" in calls
        outcomes = {outcome for _, outcome in result}
        assert "== 'hello'" in outcomes
        assert "== 'HELLO'" in outcomes

    def test_collects_raises_pairs(self, tmp_path):
        f = tmp_path / "test_raises.py"
        f.write_text(
            "import pytest\n"
            "def test_invalid():\n"
            "    with pytest.raises(ValueError):\n"
            "        convert('text', 'snake')\n",
            encoding="utf-8",
        )
        result = extract_all_io_examples(f)
        assert len(result) == 1
        assert result[0] == ("convert('text', 'snake')", "raises ValueError")

    def test_collects_across_multiple_test_functions(self, tmp_path):
        f = tmp_path / "test_multi.py"
        f.write_text(
            "import pytest\n"
            "def test_a():\n"
            "    assert fib(0) == 0\n"
            "def test_b():\n"
            "    assert fib(1) == 1\n"
            "def test_c():\n"
            "    with pytest.raises(ValueError):\n"
            "        fib(-1)\n",
            encoding="utf-8",
        )
        result = extract_all_io_examples(f)
        assert len(result) == 3
        calls = [call for call, _ in result]
        assert "fib(0)" in calls
        assert "fib(1)" in calls
        assert "fib(-1)" in calls

    def test_deduplicates_identical_pairs(self, tmp_path):
        """Same assertion appearing in two test functions counts once."""
        f = tmp_path / "test_dup.py"
        f.write_text(
            "def test_a():\n"
            "    assert fib(0) == 0\n"
            "def test_b():\n"
            "    assert fib(0) == 0\n",
            encoding="utf-8",
        )
        result = extract_all_io_examples(f)
        assert len(result) == 1

    def test_does_not_deduplicate_different_outcomes(self, tmp_path):
        """Same call with different outcomes (a contradiction) keeps both rows."""
        f = tmp_path / "test_contra.py"
        f.write_text(
            "import pytest\n"
            "def test_a():\n"
            "    assert fib(0) == 0\n"
            "def test_b():\n"
            "    with pytest.raises(ValueError):\n"
            "        fib(0)\n",
            encoding="utf-8",
        )
        result = extract_all_io_examples(f)
        assert len(result) == 2

    def test_non_test_functions_ignored(self, tmp_path):
        f = tmp_path / "test_helper.py"
        f.write_text(
            "def helper():\n"
            "    assert fib(0) == 0\n"
            "def test_real():\n"
            "    assert fib(1) == 1\n",
            encoding="utf-8",
        )
        result = extract_all_io_examples(f)
        assert len(result) == 1
        assert result[0][0] == "fib(1)"
