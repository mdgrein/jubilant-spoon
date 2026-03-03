#!/usr/bin/env python3
"""
Mock model for harness testing.

Emulates Ollama: reads the prompt from stdin, emits staggered stderr lines
(simulating model thinking/status progress), then writes the code artifact
to stdout. The staggered delays let tests verify that output is forwarded
incrementally by the harness rather than accumulated and flushed at the end.

Configuration — all optional, via environment variables:

    MOCK_MODEL_STDOUT
        Content to write on stdout (the artifact).
        Default: a minimal valid Python function.

    MOCK_MODEL_STDERR
        Pipe-separated list of lines to emit on stderr.
        Default: three "Thinking..." progress lines.

    MOCK_MODEL_DELAY
        Seconds to sleep between stderr lines.  Use "0" in fast unit tests.
        Default: 0.05  (50 ms)

    MOCK_MODEL_EXITCODE
        Process exit code.  Set to "1" to simulate a model failure.
        Default: 0
"""

import os
import sys
import time

# Drain stdin — mirrors real Ollama which consumes the full prompt before responding.
_prompt = sys.stdin.read()  # noqa: F841  (intentionally unused)

delay = float(os.environ.get("MOCK_MODEL_DELAY", "0.05"))
exit_code = int(os.environ.get("MOCK_MODEL_EXITCODE", "0"))

stderr_spec = os.environ.get(
    "MOCK_MODEL_STDERR",
    "Thinking...|...pondering...|...done thinking.",
)

stdout_content = os.environ.get(
    "MOCK_MODEL_STDOUT",
    "def answer():\n    return 42\n",
)

# Emit each stderr line with a small sleep between them.
# The sleep makes the output genuinely staggered so that consumers reading
# the harness pipe receive lines one at a time, not as a single burst.
for _line in stderr_spec.split("|"):
    stripped = _line.strip()
    if stripped:
        print(stripped, file=sys.stderr, flush=True)
    if delay > 0:
        time.sleep(delay)

# Emit the artifact on stdout (no extra newline added by us).
sys.stdout.write(stdout_content)
sys.stdout.flush()

sys.exit(exit_code)
