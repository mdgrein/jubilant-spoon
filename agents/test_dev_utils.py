import pytest
from pathlib import Path
from dev_utils import (
    classify_failure, 
    parse_pytest_failures, 
    format_failure_for_llm, 
    get_test_code,
    FailureCategory
)

def test_classify_failure_missing_symbol():
    output = "AttributeError: module 'case_converter' has no attribute 'snake_to_camel'"
    info = classify_failure(output)
    assert info.category == FailureCategory.MISSING_SYMBOL
    assert info.symbol == "snake_to_camel"

def test_classify_failure_assertion():
    output = "E   AssertionError: assert 'HELLO' == 'hello'"
    info = classify_failure(output)
    assert info.category == FailureCategory.ASSERTION_MISMATCH

def test_parse_pytest_failures():
    output = """
============================================================================= FAILURES ==============================================================================
____________________________________________________________________________ test_fail1 _____________________________________________________________________________
temp_test.py:5: in test_fail1
    assert False
E   assert False
____________________________________________________________________________ test_fail2 _____________________________________________________________________________ 
temp_test.py:8: in test_fail2
    1 / 0
E   ZeroDivisionError: division by zero
====================================================================== short test summary info ====================================================================== 
"""
    failures = parse_pytest_failures(output)
    assert "test_fail1" in failures
    assert "test_fail2" in failures
    assert failures["test_fail1"].file_path == "temp_test.py"
    assert failures["test_fail1"].line_number == 5
    assert failures["test_fail2"].line_number == 8

def test_format_failure_for_llm_did_not_raise():
    traceback = r"""
______________________ test_convert_case_invalid_target _______________________
tests\test_case_converter.py:67: in test_convert_case_invalid_target
    with pytest.raises(ValueError):
         ^^^^^^^^^^^^^^^^^^^^^^^^^
E   Failed: DID NOT RAISE <class 'ValueError'>
"""
    summary = format_failure_for_llm("test_convert_case_invalid_target", traceback)
    assert "should have raised a `ValueError`" in summary
    assert "with pytest.raises(ValueError):" in summary

def test_format_failure_for_llm_assertion_with_carets():
    traceback = r"""
_________________________ test_convert_case_lowercase _________________________
tests\test_case_converter.py:10: in test_convert_case_lowercase
    assert convert_case("HELLO", "lower") == "hello"
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   AssertionError: assert 'HELLO' == 'hello'
"""
    summary = format_failure_for_llm("test_convert_case_lowercase", traceback)
    assert 'assert convert_case("HELLO", "lower") == "hello"' in summary
    assert "resulted in 'HELLO', but it was expected to be 'hello'" in summary

def test_format_failure_for_llm_basic_assertion():
    traceback = """
_________________________ test_basic _________________________
    assert 1 == 2
E   AssertionError: assert 1 == 2
"""
    summary = format_failure_for_llm("test_basic", traceback)
    assert "resulted in 1, but it was expected to be 2" in summary
    assert "assert 1 == 2" in summary

def test_format_failure_for_llm_did_not_raise_with_code():
    traceback = r"""
______________________ test_convert_case_invalid_target _______________________
tests\test_case_converter.py:67: in test_convert_case_invalid_target
    with pytest.raises(ValueError):
         ^^^^^^^^^^^^^^^^^^^^^^^^^
E   Failed: DID NOT RAISE <class 'ValueError'>
"""
    test_code = """
def test_convert_case_invalid_target():
    with pytest.raises(ValueError):
        case_converter.convert_case("Hello", "invalid")
"""
    summary = format_failure_for_llm("test_convert_case_invalid_target", traceback, test_code)
    print(f"Summary: {summary}")
    assert "case_converter.convert_case(\"Hello\", \"invalid\")" in summary
    assert "should have raised a `ValueError`" in summary

def test_get_test_code(tmp_path):
    test_file = tmp_path / "temp_test.py"
    test_file.write_text("""
def test_pass():
    assert True

def test_fail1():
    # some comment
    assert False

def test_fail2():
    1 / 0

def another_func():
    pass
""", encoding="utf-8")
    
    # Test line 7 (assert False)
    code1 = get_test_code("temp_test.py", 7, tmp_path)
    assert "def test_fail1():" in code1
    assert "assert False" in code1
    assert "def test_fail2():" not in code1

    # Test line 10 (1 / 0)
    code2 = get_test_code("temp_test.py", 10, tmp_path)
    assert "def test_fail2():" in code2
    assert "1 / 0" in code2
    assert "def another_func():" not in code2

def test_get_test_code_no_file(tmp_path):
    code = get_test_code("non_existent.py", 10, tmp_path)
    assert "Error: Test file not found" in code
