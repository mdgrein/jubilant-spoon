"""
Shared output utilities for Clowder.

Provides clean_job_output() — the single source of truth for stripping
ANSI/VT100 terminal escape sequences from job output text.
"""

import re

_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def clean_job_output(text: str) -> str:
    """Strip ANSI/VT100 terminal escape sequences from job output text."""
    return _ANSI_ESCAPE.sub("", text)
