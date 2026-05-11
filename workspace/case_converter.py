import re


def convert_case(text, target):
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    allowed_targets = {"lower", "upper", "kebab", "camel"}
    if target not in allowed_targets:
        raise ValueError(f"Invalid target: {target}")
    if target == "lower":
        return text.lower()
    if target == "upper":
        return text.upper()

    # Normalize text by replacing non-alphanumeric with spaces
    normalized = re.sub(r"[^A-Za-z0-9]", " ", text).strip()
    # Split words by transitions from lowercase/number to uppercase or uppercase to uppercase+lowercase
    words = (
        re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", normalized)
        .strip()
        .split()
    )

    if not words:
        return ""

    if target == "kebab":
        return "-".join(word.lower() for word in words)
    if target == "camel":
        return words[0].lower() + "".join(word.capitalize() for word in words[1:])
