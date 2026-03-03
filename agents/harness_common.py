"""
Shared utilities for dev_harness.py, test_harness.py, and verify_harness.py.

Provides: model dispatch, thinking/fence stripping, ruff checks, pytest runner,
          DB helpers, job spawning.

Vendor-specific backends live in separate modules:
  vendor_local_ollama.py  — Ollama running locally via WSL
  vendor_alibaba.py       — Alibaba DashScope (Qwen models)
  vendor_anthropic.py     — Anthropic Claude CLI
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Allow importing db.py from agents/
sys.path.insert(0, str(Path(__file__).parent))
from db import ClowderDB
import log_levels  # noqa: F401  registers TRACE and MODEL levels
from vendor_local_ollama import call_ollama
from vendor_local_ollama import DEFAULT_MODEL as OLLAMA_DEFAULT_MODEL
from vendor_local_ollama import VENDOR as OLLAMA_VENDOR
from vendor_alibaba import call_qwen
from vendor_alibaba import DEFAULT_MODEL as QWEN_DEFAULT_MODEL
from vendor_alibaba import VENDOR as QWEN_VENDOR
from vendor_anthropic import call_claude
from vendor_anthropic import DEFAULT_MODEL as CLAUDE_DEFAULT_MODEL
from vendor_anthropic import VENDOR as CLAUDE_VENDOR

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 15     # LLM iterations per dev/test job (ruff + pytest feedback loops)
MAX_DEV_RETRIES = 5     # max dev+verify cycles before giving up


# ---------------------------------------------------------------------------
# Logging / prompt helpers
# ---------------------------------------------------------------------------

def log_prompt(prompt: str, tag: str = "harness") -> None:
    """Print the prompt to stdout so it appears in the job log."""
    print(f"[{tag}] --- PROMPT ({len(prompt)} chars) ---")
    print(prompt)
    print(f"[{tag}] --- END PROMPT ---")


# ---------------------------------------------------------------------------
# Model dispatch
# ---------------------------------------------------------------------------

def call_model(
    prompt: str,
    vendor: str = "local-ollama",
    model: str | None = None,
    db=None,
    job_id: str | None = None,
) -> str:
    """Call the configured vendor and return only the text response.

    The command can be overridden for testing via the CLOWDER_OLLAMA_CMD
    environment variable (space-separated).  For example:
        CLOWDER_OLLAMA_CMD="python agents/mock_model.py"
    The CLOWDER_OLLAMA_CMD override always wins over vendor dispatch.

    If db is provided, token usage is written to the model_usage table.
    Callers that don't have a DB (tests, mock paths) pass no db — zero-change
    behavior, no rows written.
    """
    cmd_override = os.environ.get("CLOWDER_OLLAMA_CMD")
    if not cmd_override:
        if vendor == "alibaba":
            text, tokens_in, tokens_out = call_qwen(prompt, model=model)
            resolved_vendor = QWEN_VENDOR
            resolved_model = model or QWEN_DEFAULT_MODEL
        elif vendor == "anthropic":
            text, tokens_in, tokens_out = call_claude(prompt, model=model)
            resolved_vendor = CLAUDE_VENDOR
            resolved_model = model or CLAUDE_DEFAULT_MODEL
        else:
            text, tokens_in, tokens_out = call_ollama(prompt, model=model)
            resolved_vendor = OLLAMA_VENDOR
            resolved_model = model or OLLAMA_DEFAULT_MODEL
    else:
        # Test/mock override — always routed through call_ollama.
        text, tokens_in, tokens_out = call_ollama(prompt, cmd_override=cmd_override, model=model)
        resolved_vendor = OLLAMA_VENDOR
        resolved_model = model or OLLAMA_DEFAULT_MODEL

    if db and (tokens_in or tokens_out):
        ts = datetime.now(timezone.utc).isoformat()
        db.conn.execute(
            "INSERT INTO model_usage (vendor, model, tokens_in, tokens_out, called_at, job_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (resolved_vendor, resolved_model, tokens_in, tokens_out, ts, job_id),
        )
        db.conn.commit()

    return text


# ---------------------------------------------------------------------------
# Output cleaning
# ---------------------------------------------------------------------------

def strip_thinking(text: str) -> str:
    """Remove model reasoning output before the actual code.

    Handles two formats:
    - ollama plaintext: "Thinking...\\n...\\n...done thinking.\\n"
    - XML tags:         "<think>...</think>"
    """
    text = re.sub(r"Thinking\.\.\..*?\.\.\.done thinking\.", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


# Markdown section markers that are never valid Python.
# A line matching this pattern signals the model has switched to chatbot mode.
# Note: single-# headings are excluded because `# comment` is valid Python;
# markdown headings use 2+ hashes (## Section).
_PROSE_MARKER_RE = re.compile(r"^(-{3,}|={3,}|#{2,6}\s)", re.MULTILINE)


def _truncate_at_prose(text: str) -> str:
    """Cut output at the first unambiguous markdown section marker.

    When the model goes into chatbot mode it typically starts with the correct
    code, then pivots to explanatory prose separated by a horizontal rule
    (---) or a heading (### ...).  Neither is valid Python, so everything from
    the first such marker onwards can be discarded safely.
    """
    m = _PROSE_MARKER_RE.search(text)
    return text[:m.start()].rstrip() if m else text


def extract_code(text: str) -> str:
    """Extract code from model output.

    If the model wrapped its answer in a fenced code block (despite being told
    not to), pull out only the block's content — discarding any surrounding
    prose.  If there are no fences, return the text as-is (assumed raw code).
    """
    match = re.search(r"```(?:[a-zA-Z]*)?\n?(.*?)```", text, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def clean_output(text: str) -> str:
    """Strip thinking blocks, truncate at prose markers, then extract code."""
    text = strip_thinking(text)
    text = _truncate_at_prose(text)
    return extract_code(text)


# ---------------------------------------------------------------------------
# Ruff
# ---------------------------------------------------------------------------

def _find_ruff() -> str | None:
    """Find ruff next to the current Python, in the project venv, or on PATH."""
    project_root = Path(__file__).parent.parent
    for candidate in [
        Path(sys.executable).parent / "ruff",
        Path(sys.executable).parent / "ruff.exe",
        project_root / ".venv" / "Scripts" / "ruff.exe",
        project_root / ".venv" / "bin" / "ruff",
    ]:
        if candidate.exists():
            return str(candidate)
    return shutil.which("ruff")


def run_ruff(file_path: Path, tag: str = "") -> tuple[bool, str]:
    """
    Run ruff format then ruff check on file_path.

    ruff format:      auto-fixes style; fails on syntax errors.
    ruff check --fix: silently patches trivial lint issues.
    ruff check:       remaining issues returned as feedback.

    Returns (passed, feedback). feedback is "SKIPPED" if ruff not found.
    """
    prefix = f"[{tag}] " if tag else ""
    ruff = _find_ruff()
    if not ruff:
        print(f"{prefix}WARNING: ruff not found, skipping code checks")
        return True, "SKIPPED"

    path_str = str(file_path)

    fmt = subprocess.run([ruff, "format", path_str], capture_output=True, text=True)
    if fmt.returncode != 0:
        output = (fmt.stdout + fmt.stderr).strip()
        return False, f"ruff format failed (likely a syntax error):\n\n{output}"

    subprocess.run(
        [ruff, "check", "--fix", "--silent", path_str],
        capture_output=True, text=True,
    )

    check = subprocess.run([ruff, "check", path_str], capture_output=True, text=True)
    if check.returncode != 0:
        output = (check.stdout + check.stderr).strip()
        return False, f"ruff check reported issues:\n\n{output}"

    return True, ""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def load_job(job_id: str) -> tuple[ClowderDB, dict]:
    """Open DB and return (db, job_row). Exits if job not found."""
    db = ClowderDB(os.environ.get("CLOWDER_DB_PATH", "clowder.db"))
    # Migration: ensure model_usage table exists for DBs created before this feature.
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS model_usage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor      TEXT    NOT NULL,
            model       TEXT    NOT NULL,
            tokens_in   INTEGER NOT NULL DEFAULT 0,
            tokens_out  INTEGER NOT NULL DEFAULT 0,
            called_at   TEXT    NOT NULL,
            job_id      TEXT
        )
    """)
    # Migration: rename jobs.backend → jobs.vendor and translate old values.
    jobs_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "backend" in jobs_cols and "vendor" not in jobs_cols:
        db.conn.execute(
            "ALTER TABLE jobs ADD COLUMN vendor TEXT NOT NULL DEFAULT 'local-ollama'"
        )
        db.conn.execute("""
            UPDATE jobs SET vendor = CASE backend
                WHEN 'qwen'   THEN 'alibaba'
                WHEN 'claude' THEN 'anthropic'
                ELSE 'local-ollama' END
        """)
    db.conn.commit()
    job = db.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not job:
        print(f"ERROR: Job {job_id} not found")
        sys.exit(1)
    return db, job


def fail_job(db: ClowderDB, job_id: str, reason: str, tag: str = "harness"):
    ts = datetime.now(timezone.utc).isoformat()
    db.conn.execute("""
        UPDATE jobs SET status = 'failed', termination_reason = ?,
            completed_at = ?, updated_at = ?
        WHERE job_id = ?
    """, (reason, ts, ts, job_id))
    db.conn.commit()
    db.close()
    print(f"[{tag}] FAILED: {reason}")
    sys.exit(1)


def complete_job(db: ClowderDB, job_id: str, output: str):
    ts = datetime.now(timezone.utc).isoformat()
    db.conn.execute("""
        UPDATE jobs
        SET status = 'completed', termination_reason = 'success',
            completed_at = ?, updated_at = ?, job_output = ?
        WHERE job_id = ?
    """, (ts, ts, output, job_id))
    db.conn.commit()
    db.close()


# ---------------------------------------------------------------------------
# Pytest
# ---------------------------------------------------------------------------

def _find_python() -> str:
    """Find a Python that has pytest installed: prefer the project venv."""
    project_root = Path(__file__).parent.parent
    for candidate in [
        project_root / ".venv" / "Scripts" / "python.exe",
        project_root / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def run_pytest(test_path: Path, workspace: Path) -> tuple[int, str]:
    """
    Run pytest on test_path.

    Returns (returncode, output):
        0  = all tests passed
        1  = tests ran but some failed (expected in TDD before impl exists)
        2+ = collection/internal error (bad test file)
    """
    python = _find_python()
    result = subprocess.run(
        [python, "-m", "pytest", str(test_path), "-v", "--tb=short", "--no-header"],
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )
    return result.returncode, (result.stdout + result.stderr).strip()


# ---------------------------------------------------------------------------
# Job spawning (verifier -> new dev + verifier cycle)
# ---------------------------------------------------------------------------

def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def spawn_retry_jobs(
    db: ClowderDB,
    pipeline_id: str,
    stage_id: str,
    original_prompt: str,
    filename: str,
    task: str,
    failure_context: str,
    attempt_num: int,
    workspace_path: str,
    current_job_id: str | None = None,
    model: str | None = None,
    vendor: str = "local-ollama",
) -> tuple[str, str]:
    """
    Spawn a new dev job (with failure context) and a new verifier job
    (which depends on the new dev job). Returns (new_dev_id, new_verify_id).

    If current_job_id is provided, any job that was waiting on the current
    verifier is also made to wait on the new verifier — keeping downstream jobs
    (e.g. all_join_in) blocked until this retry cycle fully completes.
    """
    ts = timestamp()
    dev_id = str(uuid.uuid4())
    verify_id = str(uuid.uuid4())

    dev_prompt = (
        f"FILENAME: {filename}\n\n"
        f"{task}\n\n"
        f"VERIFIER FEEDBACK (attempt {attempt_num}):\n{failure_context[-3000:]}"
    )

    for job_id, agent_type, prompt, command in [
        (dev_id,    "dev",      dev_prompt,      f"python -u agents/dev_harness.py {dev_id}"),
        (verify_id, "verifier", original_prompt, f"python -u agents/verify_harness.py {verify_id}"),
    ]:
        db.conn.execute("""
            INSERT INTO jobs (
                job_id, pipeline_id, stage_id, agent_type,
                prompt, original_prompt, command,
                max_iterations, timeout_seconds, model, vendor, allowed_paths,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 300, ?, ?, ?, 'pending', ?, ?)
        """, (
            job_id, pipeline_id, stage_id, agent_type,
            prompt, original_prompt, command,
            model, vendor,
            json.dumps([workspace_path]),
            ts, ts,
        ))

    # Verifier depends on dev completing (any terminal state), not just succeeding.
    # This allows the verifier to still run and drive the retry cycle even when
    # the dev job hard-fails after exhausting its internal iterations.
    db.conn.execute("""
        INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type)
        VALUES (?, ?, 'completed')
    """, (verify_id, dev_id))

    # Propagate: any job that was waiting on the current verifier must also
    # wait on the new verifier, so it stays blocked until the retry cycle ends.
    # NOTE: the anchor dep (dev_id → current_job_id) is inserted AFTER this
    # block so that dev_id does not appear in the downstream SELECT and get
    # a circular dependency added to it (verifier → dev → verifier).
    if current_job_id:
        downstream = db.conn.execute(
            "SELECT job_id FROM job_dependencies WHERE depends_on_job_id = ?",
            (current_job_id,)
        ).fetchall()
        for row in downstream:
            already = db.conn.execute(
                "SELECT 1 FROM job_dependencies WHERE job_id = ? AND depends_on_job_id = ?",
                (row['job_id'], verify_id)
            ).fetchone()
            if not already:
                db.conn.execute("""
                    INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type)
                    VALUES (?, ?, 'success')
                """, (row['job_id'], verify_id))
        if downstream:
            print(f"[verify] added {verify_id[:8]} as dependency for "
                  f"{len(downstream)} downstream job(s)")

    # Anchor the retry dev job to the verifier that spawned it.
    # Inserted after the propagation block so the propagation SELECT above
    # does not see dev_id as a downstream job and create a circular dep.
    if current_job_id:
        db.conn.execute("""
            INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type)
            VALUES (?, ?, 'success')
        """, (dev_id, current_job_id))

    db.conn.commit()
    print(f"[verify] spawned dev {dev_id[:8]} + verifier {verify_id[:8]}")
    return dev_id, verify_id


def spawn_child_dev(
    db: ClowderDB,
    parent_job_id: str,
    pipeline_id: str,
    stage_id: str,
    original_prompt: str,
    filename: str,
    task: str,
    failure_context: str,
    attempt_num: int,
    workspace_path: str,
    model: str | None = None,
    vendor: str = "local-ollama",
) -> str:
    """
    Spawn a single dev child job (parent_job_id = verifier's job_id).
    No dep propagation — downstream jobs wait on the coordinator,
    which blocks transitively while the verifier is waiting.
    Returns the new dev job_id.
    """
    ts = timestamp()
    dev_id = str(uuid.uuid4())
    dev_prompt = (
        f"FILENAME: {filename}\n\n"
        f"{task}\n\n"
        f"VERIFIER FEEDBACK (attempt {attempt_num}):\n{failure_context[-3000:]}"
    )
    db.conn.execute("""
        INSERT INTO jobs (
            job_id, pipeline_id, stage_id, agent_type,
            prompt, original_prompt, command,
            max_iterations, timeout_seconds, model, vendor, allowed_paths,
            parent_job_id,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 300, ?, ?, ?, ?, 'pending', ?, ?)
    """, (
        dev_id, pipeline_id, stage_id, "dev",
        dev_prompt, original_prompt,
        f"python -u agents/dev_harness.py {dev_id}",
        model, vendor,
        json.dumps([workspace_path]),
        parent_job_id,
        ts, ts,
    ))
    db.conn.commit()
    print(f"[verify] spawned child dev {dev_id[:8]}")
    return dev_id


def count_dev_attempts(conn, pipeline_id: str, original_prompt: str) -> int:
    """Count how many dev jobs exist for this task in this pipeline."""
    row = conn.execute(
        "SELECT COUNT(*) as n FROM jobs WHERE pipeline_id = ? AND agent_type = 'dev' AND original_prompt = ?",
        (pipeline_id, original_prompt),
    ).fetchone()
    return row["n"] if row else 0


# ---------------------------------------------------------------------------
# Stub generation (for tester validation)
# ---------------------------------------------------------------------------

def generate_stub(test_path: Path, module_name: str) -> str:
    """Parse a test file and produce a minimal stub for the module under test.

    Finds every ``from <module_name> import <name>`` statement and generates
    a ``def name(*args, **kwargs): raise NotImplementedError`` for each name.
    Returns an empty string if the file cannot be parsed (syntax error, etc.).
    """
    import ast as _ast

    try:
        tree = _ast.parse(test_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return ""

    names: list[str] = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom) and node.module == module_name:
            for alias in node.names:
                if alias.name != "*":
                    names.append(alias.name)

    if not names:
        return f"# stub for {module_name}\n"

    lines = [f"# Auto-generated stub for {module_name} — overwritten by dev job\n"]
    for name in sorted(set(names)):
        lines.append(f"def {name}(*args, **kwargs):\n    raise NotImplementedError\n\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Prompt parsing
# ---------------------------------------------------------------------------

def parse_input_files(prompt_text: str) -> list[str]:
    """Extract INPUT_FILES: value from prompt, returning a list of filenames."""
    for line in prompt_text.splitlines():
        if line.strip().upper().startswith("INPUT_FILES:"):
            value = line.strip()[len("INPUT_FILES:"):].strip()
            return [f.strip() for f in value.split(",") if f.strip()]
    return []


def parse_prompt(prompt_text: str) -> tuple[str | None, str]:
    """
    Split job prompt into (filename, task).

    Expects the prompt to begin with a "FILENAME: <name>" line.
    Everything else becomes the task description.
    """
    filename = None
    task_lines = []
    for line in prompt_text.splitlines():
        if filename is None and line.strip().startswith("FILENAME:"):
            filename = line.strip()[len("FILENAME:"):].strip()
        else:
            task_lines.append(line)
    return filename, "\n".join(task_lines).strip()
