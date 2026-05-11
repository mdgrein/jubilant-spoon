"""
Unit and integration tests for agents/harness_common.py.

call_ollama() tests use the real mock_model.py script, invoked via the
CLOWDER_OLLAMA_CMD env-var override.  Everything else either uses the DB
helpers directly or is a pure-function test.
"""

import json
import sys
import uuid
from pathlib import Path

import pytest

# harnesses/ and pipeline/ must be on sys.path before any harness imports.
HARNESSES_DIR = Path(__file__).parent.parent / "harnesses"
PIPELINE_DIR = Path(__file__).parent.parent / "pipeline"
sys.path.insert(0, str(HARNESSES_DIR))
sys.path.insert(0, str(PIPELINE_DIR))

import harness_common as hc  # noqa: E402
from db import ClowderDB  # noqa: E402

MOCK_MODEL = HARNESSES_DIR / "mock_model.py"
_PIPELINE_SCHEMA = (PIPELINE_DIR / "schema_pipelines.sql").read_text()


# ---------------------------------------------------------------------------
# Shared DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path):
    """A file-based ClowderDB with the pipeline schema applied (required for multi-connection tests)."""
    db_path = tmp_path / "clowder.db"
    db = ClowderDB(str(db_path))
    db.conn.executescript(_PIPELINE_SCHEMA)
    db.conn.commit()
    yield db
    db.close()


def _insert_job(
    db: ClowderDB,
    *,
    job_id: str,
    workspace_path: str,
    pipeline_id: str = "pipe-1",
    stage_id: str = "stage-1",
    prompt: str = "FILENAME: hello.py\n\nWrite hello world.",
    original_prompt: str | None = None,
    agent_type: str = "dev",
) -> None:
    """Insert the minimal pipeline/stage/job rows needed by harness tests."""
    ts = db._timestamp()
    if original_prompt is None:
        original_prompt = prompt

    db.conn.execute(
        """
        INSERT OR IGNORE INTO pipelines
            (pipeline_id, original_prompt, status, created_at, updated_at)
        VALUES (?, 'test', 'running', ?, ?)
        """,
        (pipeline_id, ts, ts),
    )
    db.conn.execute(
        """
        INSERT OR IGNORE INTO stages
            (stage_id, pipeline_id, name, stage_order, status, created_at)
        VALUES (?, ?, 'dev', 1, 'running', ?)
        """,
        (stage_id, pipeline_id, ts),
    )
    db.conn.execute(
        """
        INSERT INTO jobs (
            job_id, pipeline_id, stage_id, agent_type,
            prompt, original_prompt, command,
            max_iterations, timeout_seconds, allowed_paths,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, 50, 300, ?, 'pending', ?, ?)
        """,
        (
            job_id,
            pipeline_id,
            stage_id,
            agent_type,
            prompt,
            original_prompt,
            json.dumps([workspace_path]),
            ts,
            ts,
        ),
    )
    db.conn.commit()


@pytest.fixture()
def ollama_env(monkeypatch):
    """Point CLOWDER_OLLAMA_CMD at mock_model.py so call_ollama() is testable."""
    monkeypatch.setenv("CLOWDER_OLLAMA_CMD", f"{sys.executable} {MOCK_MODEL}")
    yield


# ---------------------------------------------------------------------------
# Output-cleaning functions
# ---------------------------------------------------------------------------


class TestStripThinking:
    def test_plaintext_format(self):
        text = "Thinking...\nsome reasoning here\n...done thinking.\nresult = 1"
        assert hc.strip_thinking(text) == "result = 1"

    def test_xml_format(self):
        text = "<think>internal thoughts</think>\nresult = 2"
        assert hc.strip_thinking(text) == "result = 2"

    def test_no_thinking_block(self):
        code = "def foo():\n    pass"
        assert hc.strip_thinking(code) == code

    def test_multiple_xml_blocks(self):
        text = "<think>a</think>code<think>b</think>"
        assert hc.strip_thinking(text) == "code"

    def test_plaintext_across_multiple_lines(self):
        text = "Thinking...\nstep 1\nstep 2\n...done thinking.\nx = 1"
        result = hc.strip_thinking(text)
        assert "step 1" not in result
        assert "x = 1" in result


class TestExtractCode:
    def test_python_fence(self):
        code = "```python\ndef hello():\n    pass\n```"
        assert hc.extract_code(code) == "def hello():\n    pass"

    def test_plain_fence(self):
        code = "```\nresult = 1\n```"
        assert hc.extract_code(code) == "result = 1"

    def test_no_fence_unchanged(self):
        code = "result = 1"
        assert hc.extract_code(code) == code

    def test_fence_without_language_tag(self):
        code = "```\nx = 1\n```"
        assert "x = 1" in hc.extract_code(code)


class TestTruncateAtProse:
    def test_truncates_at_horizontal_rule(self):
        text = "def foo():\n    return 1\n---\n### Explanation\nSome prose here."
        assert hc._truncate_at_prose(text) == "def foo():\n    return 1"

    def test_truncates_at_heading(self):
        text = "x = 1\n### Usage\nSome explanation."
        assert hc._truncate_at_prose(text) == "x = 1"

    def test_double_hash_heading_truncates(self):
        # ## is unambiguously a markdown heading, never valid Python
        text = "x = 1\n## Section\nProse here."
        assert hc._truncate_at_prose(text) == "x = 1"

    def test_single_hash_comment_not_truncated(self):
        # Single # is a Python comment — must NOT truncate
        code = "# This is a comment\nx = 1"
        assert hc._truncate_at_prose(code) == code

    def test_no_marker_returns_unchanged(self):
        code = "def foo():\n    return 1\n"
        assert hc._truncate_at_prose(code) == code  # returned as-is, no rstrip

    def test_equals_rule_truncates(self):
        text = "x = 1\n===\nMore text."
        assert hc._truncate_at_prose(text) == "x = 1"

    def test_marker_only_output_returns_empty(self):
        # Degenerate: model output is nothing but a divider
        text = "---\nAll prose, no code."
        assert hc._truncate_at_prose(text) == ""

    def test_inline_dashes_not_truncated(self):
        # Mid-line dashes must not trigger truncation
        code = "x = a - b  # inline comment with -- dashes\ny = 1"
        assert hc._truncate_at_prose(code) == code

    def test_real_chatbot_response_keeps_leading_code(self):
        """Reproduce the job-7a41a9ad failure: correct code first, chatbot prose after."""
        text = (
            "def run_all():\n"
            "    print('hello')\n"
            "\n"
            "---\n"
            "### \u2705 Corrected Function\n"
            "```python\n"
            "def fibonacci(n):\n"
            "    return []\n"
            "```\n"
            "Let me know if you meant something else.\n"
        )
        result = hc._truncate_at_prose(text)
        assert "run_all" in result
        assert "fibonacci" not in result
        assert "Let me know" not in result


class TestCleanOutput:
    def test_strips_thinking_and_fence(self):
        raw = "<think>reasoning</think>\n```python\nx = 1\n```"
        assert hc.clean_output(raw) == "x = 1"

    def test_plain_code_passes_through(self):
        code = "x = 1"
        assert hc.clean_output(code) == code

    def test_truncates_prose_before_fence_extraction(self):
        """Fence inside chatbot prose must not be extracted; leading code is used instead."""
        raw = (
            "def answer():\n"
            "    return 42\n"
            "---\n"
            "```python\n"
            "def wrong():\n"
            "    return 0\n"
            "```\n"
        )
        result = hc.clean_output(raw)
        assert "answer" in result
        assert "wrong" not in result


# ---------------------------------------------------------------------------
# parse_prompt
# ---------------------------------------------------------------------------


class TestParsePrompt:
    def test_with_filename(self):
        prompt = "FILENAME: fibonacci.py\n\nWrite a fibonacci function."
        filename, task = hc.parse_prompt(prompt)
        assert filename == "fibonacci.py"
        assert "fibonacci function" in task

    def test_without_filename_returns_none(self):
        prompt = "Do something without a filename."
        filename, task = hc.parse_prompt(prompt)
        assert filename is None
        assert task == prompt

    def test_filename_with_surrounding_whitespace(self):
        prompt = "FILENAME:   hello.py   \n\nTask here."
        filename, _ = hc.parse_prompt(prompt)
        assert filename == "hello.py"

    def test_multiline_task_preserved(self):
        prompt = "FILENAME: foo.py\n\nLine 1\nLine 2\nLine 3"
        filename, task = hc.parse_prompt(prompt)
        assert filename == "foo.py"
        assert "Line 1" in task
        assert "Line 3" in task

    def test_filename_not_consumed_into_task(self):
        prompt = "FILENAME: bar.py\n\nSome task."
        _, task = hc.parse_prompt(prompt)
        assert "FILENAME" not in task


# ---------------------------------------------------------------------------
# call_ollama — full round-trip through mock_model.py
# ---------------------------------------------------------------------------


class TestCallModel:
    def test_returns_stdout_only(self, ollama_env, monkeypatch, capsys):
        """Return value must be stdout (the artifact), not stderr (thinking)."""
        monkeypatch.setenv("MOCK_MODEL_STDOUT", "result = 42\n")
        monkeypatch.setenv("MOCK_MODEL_STDERR", "thinking...|still thinking...")
        monkeypatch.setenv("MOCK_MODEL_DELAY", "0")

        result = hc.call_model("test prompt", vendor="local-ollama", model="qwen3:8b")

        assert result == "result = 42"  # stripped by .strip()
        assert "thinking" not in result  # stderr must not leak into return

    def test_stderr_forwarded_to_process_stdout(self, ollama_env, monkeypatch, capsys):
        """Both model stdout and stderr must be printed to the harness process stdout."""
        monkeypatch.setenv("MOCK_MODEL_STDOUT", "x = 1\n")
        monkeypatch.setenv("MOCK_MODEL_STDERR", "THINKING_MARKER_A|THINKING_MARKER_B")
        monkeypatch.setenv("MOCK_MODEL_DELAY", "0")

        hc.call_model("test prompt", vendor="local-ollama", model="qwen3:8b")

        out = capsys.readouterr().out
        assert "THINKING_MARKER_A" in out  # stderr forwarded
        assert "THINKING_MARKER_B" in out
        assert "x = 1" in out  # stdout also forwarded

    def test_staggered_lines_all_arrive(self, ollama_env, monkeypatch, capsys):
        """All staggered stderr lines must be captured even with delays."""
        lines = ["step_1", "step_2", "step_3", "step_4", "step_5"]
        monkeypatch.setenv("MOCK_MODEL_STDERR", "|".join(lines))
        monkeypatch.setenv("MOCK_MODEL_STDOUT", "done = True\n")
        monkeypatch.setenv("MOCK_MODEL_DELAY", "0.02")  # 20 ms per line

        result = hc.call_model("test prompt", vendor="local-ollama", model="qwen3:8b")

        out = capsys.readouterr().out
        for line in lines:
            assert line in out, f"Expected '{line}' in forwarded output"
        assert result == "done = True"

    def test_stderr_and_stdout_interleaved_correctly(
        self, ollama_env, monkeypatch, capsys
    ):
        """Stdout artifact arrives after all stderr thinking lines."""
        monkeypatch.setenv("MOCK_MODEL_STDERR", "THINKING_LINE")
        monkeypatch.setenv("MOCK_MODEL_STDOUT", "ARTIFACT_LINE\n")
        monkeypatch.setenv("MOCK_MODEL_DELAY", "0")

        result = hc.call_model("test prompt", vendor="local-ollama", model="qwen3:8b")

        out = capsys.readouterr().out
        assert "THINKING_LINE" in out
        assert "ARTIFACT_LINE" in out
        # Return value is only the artifact
        assert result == "ARTIFACT_LINE"
        assert "THINKING_LINE" not in result

    def test_empty_stderr_does_not_crash(self, ollama_env, monkeypatch):
        """Empty stderr spec must not raise."""
        monkeypatch.setenv("MOCK_MODEL_STDERR", "")
        monkeypatch.setenv("MOCK_MODEL_STDOUT", "pass\n")
        monkeypatch.setenv("MOCK_MODEL_DELAY", "0")

        result = hc.call_model("prompt", vendor="local-ollama", model="qwen3:8b")
        assert result == "pass"

    def test_thinking_block_in_stdout_returned_raw(self, ollama_env, monkeypatch):
        """call_ollama() returns raw stdout; harnesses call clean_output() separately."""
        monkeypatch.setenv("MOCK_MODEL_STDOUT", "<think>reasoning</think>\ncode = 1\n")
        monkeypatch.setenv("MOCK_MODEL_STDERR", "")
        monkeypatch.setenv("MOCK_MODEL_DELAY", "0")

        result = hc.call_model("prompt", vendor="local-ollama", model="qwen3:8b")
        # Raw return includes the think tag — stripping is the harness's job
        assert "<think>" in result

    def test_unicode_in_model_output_does_not_crash(
        self, ollama_env, monkeypatch, capsys
    ):
        """Unicode characters (e.g. Braille spinner \u2819) must not raise UnicodeEncodeError."""
        monkeypatch.setenv("MOCK_MODEL_STDERR", "\u2819 spinning \u2819")
        monkeypatch.setenv("MOCK_MODEL_STDOUT", "x = 1\n")
        monkeypatch.setenv("MOCK_MODEL_DELAY", "0")

        result = hc.call_model("prompt", vendor="local-ollama", model="qwen3:8b")
        assert result == "x = 1"

    def test_multiline_stdout_joined_correctly(self, ollama_env, monkeypatch):
        """Multi-line artifact must be returned in full."""
        code = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        monkeypatch.setenv("MOCK_MODEL_STDOUT", code)
        monkeypatch.setenv("MOCK_MODEL_STDERR", "")
        monkeypatch.setenv("MOCK_MODEL_DELAY", "0")

        result = hc.call_model("prompt", vendor="local-ollama", model="qwen3:8b")
        assert "def foo" in result
        assert "def bar" in result

    def test_raises_when_model_is_none(self):
        """call_model must raise ValueError when model is not specified."""
        with pytest.raises(ValueError, match="model"):
            hc.call_model("prompt", vendor="local-ollama", model=None)

    def test_raises_when_model_is_empty(self):
        """Empty string model is also invalid."""
        with pytest.raises(ValueError, match="model"):
            hc.call_model("prompt", vendor="local-ollama", model="")


# ---------------------------------------------------------------------------
# DB helpers: load_job
# ---------------------------------------------------------------------------


class TestLoadJob:
    def test_found(self, tmp_db, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        job_id = str(uuid.uuid4())
        _insert_job(tmp_db, job_id=job_id, workspace_path=str(tmp_path))
        tmp_db.close()

        db2, job = hc.load_job(job_id)
        assert job["job_id"] == job_id
        db2.close()

    def test_not_found_exits(self, tmp_db, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        tmp_db.close()

        with pytest.raises(SystemExit):
            hc.load_job("no-such-id")

    def test_returns_all_job_fields(self, tmp_db, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        job_id = str(uuid.uuid4())
        prompt = "FILENAME: foo.py\n\ntask"
        _insert_job(tmp_db, job_id=job_id, workspace_path=str(tmp_path), prompt=prompt)
        tmp_db.close()

        db2, job = hc.load_job(job_id)
        assert job["prompt"] == prompt
        assert job["status"] == "pending"
        db2.close()


# ---------------------------------------------------------------------------
# DB helpers: fail_job / complete_job
# ---------------------------------------------------------------------------


class TestFailJob:
    def test_sets_failed_status_and_reason(self, tmp_db, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        job_id = str(uuid.uuid4())
        _insert_job(tmp_db, job_id=job_id, workspace_path=str(tmp_path))

        with pytest.raises(SystemExit) as exc:
            hc.fail_job(tmp_db, job_id, "something broke", tag="test")
        assert exc.value.code == 1

        # fail_job closes the DB — reopen to verify
        db2 = ClowderDB(str(tmp_path / "clowder.db"))
        row = db2.conn.execute(
            "SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert row["termination_reason"] == "something broke"
        db2.close()

    def test_reason_stored_regardless_of_tag(self, tmp_db, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        job_id = str(uuid.uuid4())
        _insert_job(tmp_db, job_id=job_id, workspace_path=str(tmp_path))

        with pytest.raises(SystemExit):
            hc.fail_job(tmp_db, job_id, "my_reason", tag="harness")

        db2 = ClowderDB(str(tmp_path / "clowder.db"))
        row = db2.conn.execute(
            "SELECT termination_reason FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row["termination_reason"] == "my_reason"
        db2.close()


class TestCompleteJob:
    def test_sets_completed_status(self, tmp_db, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        job_id = str(uuid.uuid4())
        _insert_job(tmp_db, job_id=job_id, workspace_path=str(tmp_path))

        hc.complete_job(tmp_db, job_id, "result output here")

        db2 = ClowderDB(str(tmp_path / "clowder.db"))
        row = db2.conn.execute(
            "SELECT status, termination_reason, job_output FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert row["status"] == "completed"
        assert row["termination_reason"] == "success"
        assert "result output here" in row["job_output"]
        db2.close()

    def test_output_truncated_to_2000_chars(self, tmp_db, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        job_id = str(uuid.uuid4())
        _insert_job(tmp_db, job_id=job_id, workspace_path=str(tmp_path))

        long_output = "x" * 5000
        hc.complete_job(tmp_db, job_id, long_output)

        db2 = ClowderDB(str(tmp_path / "clowder.db"))
        row = db2.conn.execute(
            "SELECT job_output FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert len(row["job_output"]) == 2000
        db2.close()


# ---------------------------------------------------------------------------
# DB helpers: count_dev_attempts
# ---------------------------------------------------------------------------


class TestCountDevAttempts:
    def test_zero_when_no_dev_jobs(self, tmp_db):
        n = hc.count_dev_attempts(tmp_db.conn, "pipe-1", "FILENAME: foo.py\n\ntask")
        assert n == 0

    def test_counts_dev_jobs_for_pipeline_and_prompt(self, tmp_db, tmp_path):
        prompt = "FILENAME: foo.py\n\ntask"
        for _ in range(3):
            _insert_job(
                tmp_db,
                job_id=str(uuid.uuid4()),
                workspace_path=str(tmp_path),
                pipeline_id="pipe-1",
                prompt=prompt,
                agent_type="dev",
            )
        assert hc.count_dev_attempts(tmp_db.conn, "pipe-1", prompt) == 3

    def test_ignores_other_pipelines(self, tmp_db, tmp_path):
        prompt = "FILENAME: foo.py\n\ntask"
        _insert_job(
            tmp_db,
            job_id=str(uuid.uuid4()),
            workspace_path=str(tmp_path),
            pipeline_id="other-pipe",
            prompt=prompt,
            agent_type="dev",
        )
        assert hc.count_dev_attempts(tmp_db.conn, "pipe-1", prompt) == 0

    def test_ignores_non_dev_agent_types(self, tmp_db, tmp_path):
        prompt = "FILENAME: foo.py\n\ntask"
        _insert_job(
            tmp_db,
            job_id=str(uuid.uuid4()),
            workspace_path=str(tmp_path),
            pipeline_id="pipe-1",
            prompt=prompt,
            agent_type="tester",
        )
        assert hc.count_dev_attempts(tmp_db.conn, "pipe-1", prompt) == 0


# ---------------------------------------------------------------------------
# DB helpers: spawn_retry_jobs
# ---------------------------------------------------------------------------


class TestSpawnRetryJobs:
    def test_creates_dev_and_verifier_jobs(self, tmp_db, tmp_path):
        _insert_job(
            tmp_db,
            job_id=str(uuid.uuid4()),
            workspace_path=str(tmp_path),
            pipeline_id="pipe-1",
            stage_id="stage-1",
        )
        dev_id, verify_id = hc.spawn_retry_jobs(
            db=tmp_db,
            pipeline_id="pipe-1",
            stage_id="stage-1",
            original_prompt="FILENAME: foo.py\n\ntask",
            filename="foo.py",
            task="task description",
            failure_context="FAILED test_foo.py — assertion failed",
            attempt_num=2,
            workspace_path=str(tmp_path),
        )

        jobs = tmp_db.conn.execute(
            "SELECT job_id, agent_type FROM jobs WHERE pipeline_id = 'pipe-1'"
        ).fetchall()
        job_ids = {j["job_id"] for j in jobs}
        types = {j["agent_type"] for j in jobs}

        assert dev_id in job_ids
        assert verify_id in job_ids
        assert "dev" in types
        assert "verifier" in types

    def test_verifier_depends_on_dev_with_completed_type(self, tmp_db, tmp_path):
        _insert_job(
            tmp_db,
            job_id=str(uuid.uuid4()),
            workspace_path=str(tmp_path),
            pipeline_id="pipe-1",
            stage_id="stage-1",
        )
        dev_id, verify_id = hc.spawn_retry_jobs(
            db=tmp_db,
            pipeline_id="pipe-1",
            stage_id="stage-1",
            original_prompt="FILENAME: foo.py\n\ntask",
            filename="foo.py",
            task="task",
            failure_context="failure",
            attempt_num=1,
            workspace_path=str(tmp_path),
        )

        dep = tmp_db.conn.execute(
            "SELECT * FROM job_dependencies WHERE job_id = ? AND depends_on_job_id = ?",
            (verify_id, dev_id),
        ).fetchone()
        assert dep is not None
        assert dep["dependency_type"] == "completed"

    def test_dev_prompt_includes_failure_context(self, tmp_db, tmp_path):
        _insert_job(
            tmp_db,
            job_id=str(uuid.uuid4()),
            workspace_path=str(tmp_path),
            pipeline_id="pipe-1",
            stage_id="stage-1",
        )
        failure_msg = "AssertionError: expected 1 got 2"
        dev_id, _ = hc.spawn_retry_jobs(
            db=tmp_db,
            pipeline_id="pipe-1",
            stage_id="stage-1",
            original_prompt="FILENAME: foo.py\n\ntask",
            filename="foo.py",
            task="task",
            failure_context=failure_msg,
            attempt_num=1,
            workspace_path=str(tmp_path),
        )

        job = tmp_db.conn.execute(
            "SELECT prompt FROM jobs WHERE job_id = ?", (dev_id,)
        ).fetchone()
        assert failure_msg in job["prompt"]
        assert "VERIFIER FEEDBACK" in job["prompt"]

    def test_verifier_uses_original_prompt(self, tmp_db, tmp_path):
        _insert_job(
            tmp_db,
            job_id=str(uuid.uuid4()),
            workspace_path=str(tmp_path),
            pipeline_id="pipe-1",
            stage_id="stage-1",
        )
        original = "FILENAME: foo.py\n\nThe real task."
        _, verify_id = hc.spawn_retry_jobs(
            db=tmp_db,
            pipeline_id="pipe-1",
            stage_id="stage-1",
            original_prompt=original,
            filename="foo.py",
            task="The real task.",
            failure_context="failure",
            attempt_num=1,
            workspace_path=str(tmp_path),
        )

        job = tmp_db.conn.execute(
            "SELECT prompt FROM jobs WHERE job_id = ?", (verify_id,)
        ).fetchone()
        assert job["prompt"] == original


# ---------------------------------------------------------------------------
# run_ruff — tests that need actual ruff in the venv
# ---------------------------------------------------------------------------


class TestRunRuff:
    def test_passes_on_valid_code(self, tmp_path):
        f = tmp_path / "good.py"
        f.write_text("def hello():\n    return 42\n")
        passed, msg = hc.run_ruff(f)
        assert passed, f"Expected ruff to pass; got: {msg}"

    def test_fails_on_syntax_error(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("def broken(\n")  # syntax error
        passed, msg = hc.run_ruff(f)
        assert not passed
        assert msg  # feedback message must be non-empty

    def test_returns_skipped_when_ruff_absent(self, tmp_path, monkeypatch):
        """If ruff cannot be found the harness must not crash."""
        f = tmp_path / "any.py"
        f.write_text("x = 1\n")
        # Make _find_ruff return None to simulate missing ruff
        monkeypatch.setattr(hc, "_find_ruff", lambda: None)
        passed, msg = hc.run_ruff(f)
        assert passed
        assert msg == "SKIPPED"


# ---------------------------------------------------------------------------
# generate_stub
# ---------------------------------------------------------------------------


class TestGenerateStub:
    """Tests for harness_common.generate_stub()."""

    def test_extracts_imported_names(self, tmp_path):
        test_file = tmp_path / "test_mymod.py"
        test_file.write_text(
            "from mymod import foo, bar\n\ndef test_foo():\n    assert foo(1) == 1\n"
        )
        stub = hc.generate_stub(test_file, "mymod")
        assert "def foo(" in stub
        assert "def bar(" in stub
        assert "raise NotImplementedError" in stub

    def test_ignores_other_module_imports(self, tmp_path):
        test_file = tmp_path / "test_mymod.py"
        test_file.write_text(
            "from other import baz\nfrom mymod import qux\n\ndef test_qux():\n    pass\n"
        )
        stub = hc.generate_stub(test_file, "mymod")
        assert "def qux(" in stub
        assert "baz" not in stub

    def test_no_imports_returns_comment_stub(self, tmp_path):
        test_file = tmp_path / "test_mymod.py"
        test_file.write_text("def test_plain():\n    assert True\n")
        stub = hc.generate_stub(test_file, "mymod")
        assert "mymod" in stub
        assert "def " not in stub  # no function stubs needed

    def test_syntax_error_returns_empty(self, tmp_path):
        test_file = tmp_path / "test_bad.py"
        test_file.write_text("def broken(\n")
        stub = hc.generate_stub(test_file, "bad")
        assert stub == ""

    def test_star_import_ignored(self, tmp_path):
        test_file = tmp_path / "test_mymod.py"
        test_file.write_text("from mymod import *\n\ndef test_x():\n    pass\n")
        stub = hc.generate_stub(test_file, "mymod")
        # star imports can't be stubbed — function produces comment-only stub
        assert "def " not in stub

    def test_deduplicates_names(self, tmp_path):
        test_file = tmp_path / "test_mymod.py"
        test_file.write_text(
            "from mymod import foo\nfrom mymod import foo\n\ndef test_foo():\n    pass\n"
        )
        stub = hc.generate_stub(test_file, "mymod")
        assert stub.count("def foo(") == 1
