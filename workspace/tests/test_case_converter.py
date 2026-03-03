import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from case_converter import convert_case


def test_convert_lower_normal():
    assert convert_case("Hello World", "lower") == "hello world"


def test_convert_upper_normal():
    assert convert_case("Hello World", "upper") == "HELLO WORLD"


def test_convert_kebab_normal():
    assert convert_case("Hello World", "kebab") == "hello-world"


def test_convert_camel_normal():
    assert convert_case("Hello World", "camel") == "helloWorld"


def test_convert_kebab_with_camel_input():
    assert convert_case("HelloWorld", "kebab") == "hello-world"


def test_convert_camel_with_kebab_input():
    assert convert_case("hello-world", "camel") == "helloWorld"


def test_convert_camel_multi_word():
    assert convert_case("this is a test", "camel") == "thisIsATest"


def test_convert_kebab_multi_word():
    assert convert_case("this is a test", "kebab") == "this-is-a-test"


def test_leading_trailing_whitespace():
    assert convert_case("  Hello World  ", "kebab") == "hello-world"


def test_multiple_spaces_between_words():
    assert convert_case("hello   world", "camel") == "helloWorld"


@pytest.mark.parametrize("target", ["lower", "upper", "kebab", "camel"])
def test_empty_string_returns_empty(target):
    assert convert_case("", target) == ""


@pytest.mark.parametrize("invalid_text", [123, 123.45, True, False])
def test_non_string_text_raises_type_error(invalid_text):
    with pytest.raises(TypeError):
        convert_case(invalid_text, "lower")


def test_string_with_numbers_does_not_raise():
    assert convert_case("abc123", "lower") == "abc123"


@pytest.mark.parametrize("invalid_target", ["invalid", "lowercase", 123, None])
def test_invalid_target_raises_value_error(invalid_target):
    with pytest.raises(ValueError):
        convert_case("text", invalid_target)


def test_uppercase_target_raises_value_error():
    with pytest.raises(ValueError):
        convert_case("text", "Camel")


def test_camel_case_with_numbers_in_word():
    assert convert_case("hello2 world", "camel") == "hello2World"


def test_kebab_case_with_numbers_in_word():
    assert convert_case("hello2 world", "kebab") == "hello2-world"


def test_camel_case_with_mixed_case_and_numbers():
    assert convert_case("Hello2World", "camel") == "hello2World"


def test_kebab_case_with_existing_hyphens():
    assert convert_case("hello-world-test", "kebab") == "hello-world-test"


def test_camel_case_with_existing_hyphens():
    assert convert_case("hello-world-test", "camel") == "helloWorldTest"


def test_camel_case_with_underscore_input():
    assert convert_case("hello_world_test", "camel") == "helloWorldTest"


def test_kebab_case_with_camel_case_input():
    assert convert_case("HelloWorldTest", "kebab") == "hello-world-test"


def test_camel_case_with_camel_case_input():
    assert convert_case("HelloWorldTest", "camel") == "helloWorldTest"


def test_camel_case_with_single_word():
    assert convert_case("hello", "camel") == "hello"


def test_kebab_case_with_single_word():
    assert convert_case("hello", "kebab") == "hello"


def test_camel_case_with_acronyms():
    assert convert_case("HTTPRequest", "camel") == "httpRequest"


def test_kebab_case_with_acronyms():
    assert convert_case("HTTPRequest", "kebab") == "http-request"
