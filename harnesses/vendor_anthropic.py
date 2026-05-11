"""
Anthropic vendor backend for harness_common.call_model.

Runs `claude -p --output-format stream-json` as a subprocess, streams thinking
and text deltas to stdout, and handles quota exhaustion with automatic waits
and session resumption.

The wait deadline and session_id are persisted to quota_wait.json so a server
crash during the wait doesn't lose them — the next invocation resumes from the
same point automatically.

Requires `claude` to be on PATH and authenticated.
"""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

VENDOR = "anthropic"
CALL_TIMEOUT = 300

# Persisted across crashes: stores {"wait_until": <unix timestamp>}
_QUOTA_WAIT_FILE = Path(__file__).parent / "quota_wait.json"


# ---------------------------------------------------------------------------
# Quota persistence helpers
# ---------------------------------------------------------------------------


def _quota_save(wait_until: float, session_id: str | None = None) -> None:
    data: dict = {"wait_until": wait_until}
    if session_id:
        data["session_id"] = session_id
    _QUOTA_WAIT_FILE.write_text(json.dumps(data))


def _quota_clear() -> None:
    _QUOTA_WAIT_FILE.unlink(missing_ok=True)


def _quota_load() -> tuple[float, str | None] | None:
    """Return (wait_until, session_id) if the wait deadline is still in the future."""
    try:
        data = json.loads(_QUOTA_WAIT_FILE.read_text())
        wait_until = float(data["wait_until"])
        if wait_until > time.time():
            return wait_until, data.get("session_id")
        _quota_clear()
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        pass
    return None


def _quota_wait(wait_until: float) -> None:
    """Block until wait_until (Unix timestamp), logging progress every minute."""
    reset_str = datetime.fromtimestamp(wait_until, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    remaining = wait_until - time.time()
    print(
        f"\n[claude] Quota exhausted. Waiting until {reset_str} "
        f"({remaining / 60:.1f} min). Will retry automatically.\n",
        flush=True,
    )
    while True:
        remaining = wait_until - time.time()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 60))
        remaining = wait_until - time.time()
        if remaining > 0:
            print(
                f"[claude] Still waiting for quota reset… {remaining / 60:.1f} min left",
                flush=True,
            )
    print("[claude] Quota should be reset. Resuming.\n", flush=True)


class _QuotaExhausted(Exception):
    def __init__(self, reset_ts: float | None):
        self.reset_ts = reset_ts


# ---------------------------------------------------------------------------
# Core Claude CLI invocation
# ---------------------------------------------------------------------------


def _call_claude_once(
    prompt: str,
    model: str,
    session_id: str | None = None,
) -> tuple[str, "_QuotaExhausted | None", str | None, int, int]:
    """Run the Claude CLI once; return (text, quota_error_or_None, session_id, tokens_in, tokens_out)."""
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model",
        model,
    ]
    if session_id:
        cmd.extend(["--resume", session_id])

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    proc.stdin.write(prompt)
    proc.stdin.close()

    text_chunks: list[str] = []
    quota_hit: list[_QuotaExhausted] = []  # list used as mutable cell for thread
    seen_session_ids: list[str] = []  # capture first session_id seen
    tokens_in_tally: list[int] = [0]
    tokens_out_tally: list[int] = [0]

    def _read_stdout():
        for raw_line in iter(proc.stdout.readline, ""):
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"[claude] {raw_line}", end="", flush=True)
                continue

            sid = event.get("session_id")
            if sid and not seen_session_ids:
                seen_session_ids.append(sid)

            etype = event.get("type")

            if etype == "rate_limit_event":
                info = event.get("rate_limit_info", {})
                status = info.get("status")
                print(f"\n[claude] rate_limit_event status={status}", flush=True)
                if status == "rejected":
                    raw_ts = info.get("resetsAt")
                    quota_hit.append(
                        _QuotaExhausted(float(raw_ts) if raw_ts is not None else None)
                    )

            elif etype == "stream_event":
                inner = event.get("event", {})
                inner_type = inner.get("type")

                # Capture token usage from Claude API events.
                if inner_type == "message_start":
                    usage = inner.get("message", {}).get("usage", {})
                    tokens_in_tally[0] = usage.get("input_tokens", 0)
                elif inner_type == "message_delta":
                    usage = inner.get("usage", {})
                    tokens_out_tally[0] = usage.get("output_tokens", 0)

                delta = inner.get("delta", {})
                dtype = delta.get("type")

                if dtype == "thinking_delta":
                    chunk = delta.get("thinking", "")
                    if chunk:
                        logger.model("[thinking] %s", chunk)  # type: ignore[attr-defined]
                        print(chunk, end="", flush=True)

                elif dtype == "text_delta":
                    chunk = delta.get("text", "")
                    if chunk:
                        logger.model(chunk)  # type: ignore[attr-defined]
                        print(chunk, end="", flush=True)
                        text_chunks.append(chunk)

            elif etype == "result":
                if event.get("is_error") and not quota_hit:
                    # Detect quota/billing errors via the result errors list
                    errors = event.get("errors") or []
                    combined = " ".join(errors) + " " + (event.get("result") or "")
                    if any(
                        kw in combined.lower()
                        for kw in ("limit", "billing", "quota", "exceeded")
                    ):
                        quota_hit.append(_QuotaExhausted(None))
                if not text_chunks and not quota_hit:
                    fallback = event.get("result") or ""
                    if fallback:
                        text_chunks.append(fallback)

    def _read_stderr():
        for line in iter(proc.stderr.readline, ""):
            logger.model("[claude-err] %s", line.rstrip())  # type: ignore[attr-defined]
            print(f"[claude] {line}", end="", flush=True)

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(
            f"\n[claude] *** TIMEOUT after {CALL_TIMEOUT}s — killing ***\n", flush=True
        )
        proc.kill()
        proc.wait()
        raise

    t_out.join(timeout=2)
    t_err.join(timeout=2)

    return (
        "".join(text_chunks).strip(),
        quota_hit[0] if quota_hit else None,
        seen_session_ids[0] if seen_session_ids else None,
        tokens_in_tally[0],
        tokens_out_tally[0],
    )


def _retry_after_quota(
    prompt: str, model: str | None, session_id: str | None
) -> tuple[str, int, int]:
    """Retry a call after waiting for quota reset.

    Attempts ``--resume <session_id>`` first (gives the model its full prior
    context).  Falls back to the original "figure out where you were" prompt
    if session_id is unavailable or the resume returns an empty result.
    """
    if session_id:
        resume_prompt = (
            "The session was interrupted because the Claude quota was exhausted "
            "and has now reset. Please continue from where you left off."
        )
        text, quota_err, _, ti, to = _call_claude_once(
            resume_prompt, model, session_id=session_id
        )
        if quota_err:
            raise RuntimeError("Claude quota exhausted twice in a row; giving up.")
        if text:
            return text, ti, to
        print(
            "[claude] --resume returned empty result; falling back to original prompt.",
            flush=True,
        )

    # Fallback: no session_id or empty resume result.
    fallback_prompt = (
        "The Claude quota ran out while you were working on this task. "
        "Figure out where you were and continue. The task was:\n\n" + prompt
    )
    text, quota_err, _, ti, to = _call_claude_once(fallback_prompt, model)
    if quota_err:
        raise RuntimeError("Claude quota exhausted twice in a row; giving up.")
    return text, ti, to


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def call_claude(prompt: str, model: str) -> tuple[str, int, int]:
    """Call the Claude CLI in headless mode with streaming JSON output.

    Streams thinking and text deltas to stdout as they arrive.
    Returns (text, tokens_in, tokens_out).

    Quota handling: if the quota is exhausted, waits until the reset time
    (+ 5 min buffer) then retries.  If a ``session_id`` was captured before
    the quota hit, the retry uses ``--resume`` so the model receives its full
    prior context.  Otherwise falls back to a plain continuation prompt.

    The wait deadline and session_id are persisted to disk so a server crash
    during the wait doesn't lose them — the next invocation resumes from the
    same point automatically.
    """
    # Resume a persisted quota wait from a previous (crashed) run.
    persisted = _quota_load()
    if persisted:
        wait_until, saved_session_id = persisted
        print("[claude] Resuming persisted quota wait from previous run.", flush=True)
        _quota_wait(wait_until)
        _quota_clear()
        return _retry_after_quota(prompt, model, saved_session_id)

    # Normal path: first attempt.
    text, quota_err, session_id, tokens_in, tokens_out = _call_claude_once(
        prompt, model
    )
    if quota_err is None:
        return text, tokens_in, tokens_out

    # Quota hit — save deadline + session_id, wait, then retry.
    reset_ts = quota_err.reset_ts
    if reset_ts is None:
        reset_ts = time.time() + 5 * 3600
        print(
            "[claude] No resetsAt in quota event; defaulting to 5 h wait.", flush=True
        )

    wait_until = reset_ts + 300  # +5 min buffer
    _quota_save(wait_until, session_id)
    _quota_wait(wait_until)
    _quota_clear()

    return _retry_after_quota(prompt, model, session_id)
