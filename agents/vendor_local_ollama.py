"""
Local Ollama vendor backend for harness_common.call_model.

Handles subprocess invocation of `wsl ollama run …`, streaming stdout/stderr,
hallucination detection, and output-size kill switches.

Token counts are not available from the Ollama CLI; always returns (text, 0, 0).
"""

import logging
import subprocess
import threading
from collections import deque

logger = logging.getLogger(__name__)

VENDOR        = "local-ollama"
DEFAULT_MODEL = "deepseek-r1:8b"
# DEFAULT_MODEL = "qwen3:8b"

CALL_TIMEOUT = 300

HALLUCINATION_WINDOW    = 100       # sliding window size (lines)
HALLUCINATION_THRESHOLD = 10        # times a line must repeat in the window to trigger kill
MAX_STDOUT_CHARS        = 100_000   # kill if model stdout exceeds this (chatbot-mode runaway)


def call_ollama(prompt: str, cmd_override: str | None = None, model: str | None = None) -> tuple[str, int, int]:
    """Run a prompt through Ollama (via WSL) and return (stdout, 0, 0).

    cmd_override (space-separated string) replaces the default `wsl ollama run …`
    command — used in tests via CLOWDER_OLLAMA_CMD.

    Token counts are unavailable from the Ollama CLI; always returns 0/0.
    """
    cmd = (
        cmd_override.split()
        if cmd_override
        else ["wsl", "ollama", "run", model or DEFAULT_MODEL, "--nowordwrap"]
    )

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

    stdout_lines: list[str] = []
    _hallucination_killed = threading.Event()

    def _read_stdout():
        recent: deque[str] = deque(maxlen=HALLUCINATION_WINDOW)
        total_chars = 0
        last_logged_at = 0
        logger.trace("Starting _read_stdout thread, total_chars reset to 0")  # type: ignore[attr-defined]
        for line in iter(proc.stdout.readline, ""):
            line_len = len(line)
            stdout_lines.append(line)
            total_chars += line_len

            if total_chars - last_logged_at > 50_000:
                print(f"\n[ollama] ... received {total_chars:,} chars ...\n", flush=True)
                last_logged_at = total_chars

            logger.model("[model-out] %s", line.rstrip())  # type: ignore[attr-defined]
            print(line, end="", flush=True)

            stripped = line.strip()
            if stripped:
                recent.append(stripped)
                if recent.count(stripped) >= HALLUCINATION_THRESHOLD:
                    preview = stripped[:120]
                    print(
                        f"\n[ollama] *** HALLUCINATION KILL SWITCH TRIGGERED ***\n"
                        f"[ollama] Line repeated {HALLUCINATION_THRESHOLD}x "
                        f"in last {HALLUCINATION_WINDOW} lines: {preview!r}\n"
                        f"[ollama] Process killed. Returning partial output.\n",
                        flush=True,
                    )
                    logger.warning(
                        "Hallucination kill switch fired — line repeated %dx: %r",
                        HALLUCINATION_THRESHOLD, preview,
                    )
                    _hallucination_killed.set()
                    proc.kill()
                    break

            if total_chars > MAX_STDOUT_CHARS:
                print(
                    f"\n[ollama] *** OUTPUT SIZE KILL SWITCH TRIGGERED ***\n"
                    f"[ollama] stdout exceeded {MAX_STDOUT_CHARS:,} chars "
                    f"({total_chars:,} received). Process killed. Returning partial output.\n",
                    flush=True,
                )
                logger.warning(
                    "Output size kill switch fired — %d chars exceeded limit of %d",
                    total_chars, MAX_STDOUT_CHARS,
                )
                _hallucination_killed.set()
                proc.kill()
                break

    def _read_stderr():
        for line in iter(proc.stderr.readline, ""):
            logger.model("[model-err] %s", line.rstrip())  # type: ignore[attr-defined]
            print(line, end="", flush=True)

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(
            f"\n[ollama] *** TIMEOUT KILL SWITCH TRIGGERED ***\n"
            f"[ollama] Ollama did not finish within {CALL_TIMEOUT}s. Process killed.\n",
            flush=True,
        )
        logger.warning("call_ollama timed out after %ds, killing process", CALL_TIMEOUT)
        proc.kill()
        proc.wait()
        raise

    t_out.join(timeout=1)
    t_err.join(timeout=1)

    return "".join(stdout_lines).strip(), 0, 0
