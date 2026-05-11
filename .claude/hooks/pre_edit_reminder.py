"""
PreToolUse hook: TDD reminder before editing Python source files.

Fires only for Edit/Write tools (never Read/Glob/Grep — different tool names).
Skips test files to avoid a circular reminder.

Loop prevention: uses a per-file marker so each source file gets exactly one
reminder per edit attempt. When Claude retries the same file after writing a
test, the marker is found and consumed — the retry goes through. Other files
are unaffected and each get their own reminder.
"""

import hashlib
import json
import sys
from pathlib import Path

MARKER_DIR = Path(__file__).parent / ".markers"
MARKER_DIR.mkdir(exist_ok=True)


def is_source_py(file_path: str) -> bool:
    path = file_path.replace("\\", "/")
    name = path.split("/")[-1]
    return (
        path.endswith(".py") and "/tests/" not in path and not name.startswith("test_")
    )


def marker_for(file_path: str) -> Path:
    key = hashlib.md5(file_path.encode()).hexdigest()[:12]
    return MARKER_DIR / f".reminded_{key}"


data = json.load(sys.stdin)
file_path = data.get("tool_input", {}).get("file_path", "")

if is_source_py(file_path):
    marker = marker_for(file_path)

    if marker.exists():
        # This is the retry after Claude addressed the reminder — let it through.
        marker.unlink()
    else:
        # First attempt — remind and create the marker so the retry goes through.
        marker.write_text(file_path)
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "Did you write a test for that? "
                            "If yes, or no test is needed, proceed with the edit."
                        ),
                    }
                }
            )
        )
