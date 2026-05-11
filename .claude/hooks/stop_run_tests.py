"""
Stop hook: auto-fixes Ruff issues, then runs pytest before Claude finishes —
but only if Python files were modified during the session (detected via git status).

Order: ruff check --fix → ruff format → ruff check (verify) → pytest.
Auto-fixes what it can; blocks if unfixable lint/format errors remain or tests fail.
Respects stop_hook_active to avoid an infinite block loop.

Change detection uses content hashes (MD5) rather than file mtimes, so ruff
auto-fixing files during the hook run cannot cause false positives next turn.
State is saved after ruff runs, so next turn's baseline reflects the post-ruff content.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

data = json.load(sys.stdin)

# Prevent infinite loop: if we already blocked once this turn, stand down.
if data.get("stop_hook_active"):
    sys.exit(0)

proj_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
state_file = proj_dir / ".claude" / "hooks" / ".last_stop_state.json"


def file_hash(path):
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except Exception:
        return None


# Get dirty Python files from git status (captured once; ruff doesn't change tracking).
git_result = subprocess.run(
    ["git", "status", "--porcelain"],
    capture_output=True,
    text=True,
    cwd=proj_dir,
)


def get_dirty_py_hashes():
    """Return {rel_path: md5} for all dirty Python files that exist on disk."""
    hashes = {}
    for line in git_result.stdout.splitlines():
        if len(line) <= 3 or not line[3:].strip().endswith(".py"):
            continue
        rel = line[3:].strip()
        abs_path = proj_dir / rel
        if abs_path.exists():
            h = file_hash(abs_path)
            if h:
                hashes[rel] = h
    return hashes


# Load previous state (hashes saved at end of last hook run).
try:
    prev_state = json.loads(state_file.read_text()) if state_file.exists() else {}
except Exception:
    prev_state = {}

current_state = get_dirty_py_hashes()

if not current_state:
    # No dirty Python files at all.
    sys.exit(0)

if current_state == prev_state:
    # Hashes identical to last run — no changes were made this turn.
    sys.exit(0)


# Resolve venv binaries (Windows first, Unix fallback).
def venv_bin(name):
    for candidate in [
        proj_dir / ".venv" / "Scripts" / f"{name}.exe",
        proj_dir / ".venv" / "Scripts" / name,
        proj_dir / ".venv" / "bin" / name,
    ]:
        if candidate.exists():
            return str(candidate)
    return name  # fall back to PATH


def block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


ruff = venv_bin("ruff")
venv_python = venv_bin("python")

# --- Ruff auto-fix ---
subprocess.run(
    [ruff, "check", ".", "--fix", "--color=never"],
    capture_output=True,
    cwd=proj_dir,
)
subprocess.run(
    [ruff, "format", ".", "--color=never"],
    capture_output=True,
    cwd=proj_dir,
)

# --- Save post-ruff state NOW so next turn compares against post-ruff hashes.
# This prevents ruff's own content changes from triggering the hook again next turn.
post_ruff_state = get_dirty_py_hashes()
try:
    state_file.write_text(json.dumps(post_ruff_state))
except Exception:
    pass

# --- Ruff lint (verify no unfixable errors remain) ---
lint_result = subprocess.run(
    [ruff, "check", ".", "--output-format=concise", "--color=never"],
    capture_output=True,
    text=True,
    cwd=proj_dir,
)
if lint_result.returncode != 0:
    block(
        "Ruff lint errors (could not auto-fix). Fix before finishing.\n\n"
        + lint_result.stdout.strip()
    )

# --- Pytest ---
pytest_result = subprocess.run(
    [venv_python, "-m", "pytest", "tests/", "-q", "--tb=no", "--color=no"],
    capture_output=True,
    text=True,
    cwd=proj_dir,
)
if pytest_result.returncode != 0:
    output = (pytest_result.stdout + pytest_result.stderr).strip()

    # Extract the "short test summary info" block.
    summary_lines = []
    in_summary = False
    for line in output.splitlines():
        if "short test summary info" in line:
            in_summary = True
            continue
        if in_summary:
            summary_lines.append(line)

    summary = "\n".join(summary_lines).strip() or output  # raw fallback
    block(
        "Tests are failing. Fix them before finishing.\n\nShort test summary info:\n"
        + summary
    )
