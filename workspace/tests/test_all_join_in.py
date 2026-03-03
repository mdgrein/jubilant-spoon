import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import all_join_in



def test_run_all_fibonacci_sequence(capsys):
    all_join_in.run_all()
    captured = capsys.readouterr().out
    expected = [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]
    for num in expected:
        assert str(num) in captured


def test_run_all_binary_sort_output(capsys):
    all_join_in.run_all()
    captured = capsys.readouterr().out
    sample_list = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5]
    expected_sorted = sorted(sample_list)
    assert str(expected_sorted) in captured


def test_run_all_case_converter_lowercase(capsys):
    all_join_in.run_all()
    captured = capsys.readouterr().out
    assert "helloworld" in captured
    assert "teststring" in captured
    assert "anotherexample" in captured


def test_run_all_case_converter_uppercase(capsys):
    all_join_in.run_all()
    captured = capsys.readouterr().out
    assert "HELLOWORLD" in captured
    assert "TESTSTRING" in captured
    assert "ANOTHEREXAMPLE" in captured


def test_run_all_case_converter_kebab_case(capsys):
    all_join_in.run_all()
    captured = capsys.readouterr().out
    assert "hello-world" in captured
    assert "test-string" in captured
    assert "another-example" in captured


def test_run_all_case_converter_camel_case(capsys):
    all_join_in.run_all()
    captured = capsys.readouterr().out
    assert "helloWorld" in captured
    assert "testString" in captured
    assert "anotherExample" in captured


def test_run_all_edge_case_empty_list(capsys):
    all_join_in.run_all()
    captured = capsys.readouterr().out
    assert "[]\n" not in captured
