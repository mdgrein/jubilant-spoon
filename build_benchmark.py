#!/usr/bin/env python3
"""Assemble the distributable clowder-benchmark/ folder and zip it.

Run this once to produce the bundle for friends:

    python build_benchmark.py          # builds folder + zip
    python build_benchmark.py --no-zip # folder only (faster, for testing)

Output:
    clowder-benchmark/          distributable folder
    clowder-benchmark.zip       ready to send


Usage:
  # Production (friends)
  python run_benchmark.py qwen3:14b qwen3:32b

  # Testing locally (no Ollama needed)
  set CLOWDER_OLLAMA_CMD=python agents/mock_model.py
  python run_benchmark.py qwen3:14b

  # Build the distributable bundle
  python build_benchmark.py
"""

import argparse
import shutil
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.resolve()
BUNDLE_DIR = REPO_ROOT / "clowder-benchmark"

# Agent files to copy into agents/ subdirectory of the bundle.
# Order doesn't matter — all are copied flat into agents/.
AGENT_FILES = [
    "agents/dev_harness.py",
    "agents/test_harness.py",
    "agents/verify_harness.py",
    "agents/harness_common.py",
    "agents/dev_utils.py",
    "agents/db.py",
    "agents/log_levels.py",
    "agents/mock_model.py",
    "agents/schema_pipelines.sql",
]

REQUIREMENTS = """\
pytest>=7.0
ruff>=0.1.0
"""

README = """\
Clowder Benchmark
=================

Runs qwen3:14b (or other models) against the case_converter and all_join_in
tasks using the tester→dev→verifier TDD chain and reports the results.

Requirements
------------
  - Python 3.10+
  - Ollama installed and running
  - The model(s) you want to test pulled into Ollama
  - pytest and ruff

Setup (one-time)
----------------
1. Install Python dependencies:

       pip install -r requirements.txt

2. Pull the model(s) you plan to benchmark:

       ollama pull qwen3:14b
       ollama pull qwen3:32b      # optional

Run
---
Windows (Ollama in WSL):

    python run_benchmark.py qwen3:14b
    python run_benchmark.py qwen3:14b qwen3:32b

Linux / Mac (Ollama native):

    python run_benchmark.py qwen3:14b --ollama-cmd "ollama run"

Test without Ollama (uses built-in mock model)
----------------------------------------------
Windows:

    set CLOWDER_OLLAMA_CMD=python agents/mock_model.py
    python run_benchmark.py qwen3:14b

Linux / Mac:

    CLOWDER_OLLAMA_CMD="python agents/mock_model.py" python run_benchmark.py qwen3:14b

Results
-------
benchmark_results/
  <ISO-timestamp>/
    <model-name>/
      workspace_final/        final workspace state
      logs/                   per-task, per-job logs
        case_converter/
          <id>_tester_attempt0.log
          <id>_dev_attempt0.log
          <id>_verifier_attempt0.log
          ...
        all_join_in/
          ...
      benchmark.db            raw SQLite DB
      summary.json            machine-readable results
      summary.txt             human-readable results
    report.txt                cross-model comparison
"""


def build() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="Skip creating the zip archive (just build the folder)",
    )
    args = parser.parse_args()

    # Clear and recreate bundle directory
    if BUNDLE_DIR.exists():
        shutil.rmtree(BUNDLE_DIR)
    BUNDLE_DIR.mkdir()
    (BUNDLE_DIR / "agents").mkdir()

    # Copy run_benchmark.py (the main entry point)
    src = REPO_ROOT / "run_benchmark.py"
    dst = BUNDLE_DIR / "run_benchmark.py"
    shutil.copy2(src, dst)
    print(f"  Copied: run_benchmark.py")

    # Copy agent files
    for rel_path in AGENT_FILES:
        src = REPO_ROOT / rel_path
        if not src.exists():
            print(f"  WARNING: {rel_path} not found — skipping")
            continue
        dst = BUNDLE_DIR / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  Copied: {rel_path}")

    # Write requirements.txt
    (BUNDLE_DIR / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    print("  Wrote:  requirements.txt")

    # Write README.txt
    (BUNDLE_DIR / "README.txt").write_text(README, encoding="utf-8")
    print("  Wrote:  README.txt")

    print(f"\nBundle assembled: {BUNDLE_DIR}")

    if args.no_zip:
        return

    # Create zip archive
    zip_path = REPO_ROOT / "clowder-benchmark.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(BUNDLE_DIR.rglob("*")):
            if file_path.is_file():
                arcname = file_path.relative_to(REPO_ROOT)
                zf.write(file_path, arcname)
    print(f"Zipped:  {zip_path}")


if __name__ == "__main__":
    build()
