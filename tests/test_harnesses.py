"""
Integration tests for dev_harness.py, test_harness.py, and verify_harness.py.

Each harness is exercised by:
  1. Creating a real (temp) DB job record.
  2. Setting CLOWDER_OLLAMA_CMD to point at mock_model.py.
  3. Calling the harness main() in-process with a patched sys.argv and cwd.
  4. Asserting final DB state and workspace artefacts.

One additional subprocess test for dev_harness verifies that model output
lines are forwarded incrementally by the harness (i.e., each line appears on
the harness's stdout pipe as it is produced, not buffered until the end).
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

AGENTS_DIR = Path(__file__).parent.parent / "agents"
sys.path.insert(0, str(AGENTS_DIR))

from db import ClowderDB
import harness_common as hc

SCHEMA_SQL = AGENTS_DIR / "schema_pipelines.sql"
MOCK_MODEL = AGENTS_DIR / "mock_model.py"

# Minimal valid Python that passes ruff without needing real implementation
_GOOD_CODE = "def answer():\n    return 42\n"

# Minimal pytest test file; imports from parent so verify_harness can run it
_PASSING_TESTS = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).parent.parent))\n"
    "from solution import answer\n"
    "\n"
    "def test_answer_returns_42():\n"
    "    assert answer() == 42\n"
)

_FAILING_TESTS = (
    "def test_always_fails():\n"
    "    assert False\n"
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def harness_env(tmp_path, monkeypatch):
    """
    Full harness environment:
      - cwd = tmp_path        (so ClowderDB('clowder.db') opens our test DB)
      - workspace/            subdirectory for generated files
      - workspace/tests/      for test files
      - CLOWDER_OLLAMA_CMD    points at mock_model.py
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLOWDER_OLLAMA_CMD", f"{sys.executable} {MOCK_MODEL}")
    monkeypatch.setenv("MOCK_MODEL_DELAY", "0")  # fast by default in unit tests

    db_path = tmp_path / "clowder.db"
    db = ClowderDB(str(db_path))
    db.conn.executescript(SCHEMA_SQL.read_text())
    db.conn.commit()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tests").mkdir()

    yield {"db": db, "workspace": workspace, "tmp_path": tmp_path}
    db.close()


def _insert_job(
    db,
    *,
    job_id,
    workspace_path,
    pipeline_id="pipe-1",
    stage_id="stage-1",
    prompt,
    original_prompt=None,
    agent_type="dev",
    parent_job_id=None,
):
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
            parent_job_id,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, 50, 300, ?, ?, 'pending', ?, ?)
        """,
        (
            job_id,
            pipeline_id,
            stage_id,
            agent_type,
            prompt,
            original_prompt,
            json.dumps([str(workspace_path)]),
            parent_job_id,
            ts,
            ts,
        ),
    )
    db.conn.commit()


def _reopen_db(tmp_path) -> ClowderDB:
    """Reopen the test DB after a harness main() has closed it."""
    return ClowderDB(str(tmp_path / "clowder.db"))


# ---------------------------------------------------------------------------
# dev_harness
# ---------------------------------------------------------------------------


class TestDevHarness:
    """Tests for agents/dev_harness.py."""

    def _run_main(self, job_id, monkeypatch):
        """Call dev_harness.main() in-process."""
        import dev_harness
        monkeypatch.setattr(sys, "argv", ["dev_harness.py", job_id])
        dev_harness.main()

    def test_writes_generated_code_to_workspace(self, harness_env, monkeypatch):
        """The artifact returned by the model must be written to workspace/<filename>."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        monkeypatch.setenv("MOCK_MODEL_STDOUT", _GOOD_CODE)
        monkeypatch.setenv("MOCK_MODEL_STDERR", "Thinking...|done.")
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite an answer function.",
        )
        db.close()

        self._run_main(job_id, monkeypatch)

        output_file = workspace / "solution.py"
        assert output_file.exists(), "Harness must write the artifact file"
        assert "def answer" in output_file.read_text()

    def test_completes_job_in_db(self, harness_env, monkeypatch):
        """Job status must be 'completed' and job_output set after success."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        monkeypatch.setenv("MOCK_MODEL_STDOUT", _GOOD_CODE)
        monkeypatch.setenv("MOCK_MODEL_STDERR", "")
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite something.",
        )
        db.close()

        self._run_main(job_id, monkeypatch)

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status, termination_reason, job_output FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        db2.close()

        assert row["status"] == "completed"
        assert row["termination_reason"] == "success"
        assert row["job_output"]  # non-empty

    def test_fails_when_no_filename_in_prompt(self, harness_env, monkeypatch):
        """Without a FILENAME: line the harness must fail the job."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="Just write something without a filename.",
        )
        db.close()

        with pytest.raises(SystemExit) as exc:
            self._run_main(job_id, monkeypatch)
        assert exc.value.code != 0

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "failed"

    def test_retries_on_ruff_failure_then_succeeds(self, harness_env, monkeypatch):
        """If first model response fails ruff, harness retries and succeeds on next call."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        bad_code = "def broken(\n"   # syntax error — ruff will reject this
        good_code = _GOOD_CODE

        responses = [bad_code, good_code]
        call_count = [0]

        def _fake_ollama(prompt, **_kw):
            resp = responses[call_count[0] % len(responses)]
            call_count[0] += 1
            return resp

        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite something.",
        )
        db.close()

        with patch("dev_harness.call_model", side_effect=_fake_ollama):
            import dev_harness
            monkeypatch.setattr(sys, "argv", ["dev_harness.py", job_id])
            dev_harness.main()

        assert call_count[0] >= 2, "Should have made at least two model calls"
        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "completed"

    def test_fails_job_after_max_ruff_failures(self, harness_env, monkeypatch):
        """Harness must fail the job if ruff never passes within MAX_ATTEMPTS."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        def _always_bad_ollama(prompt, **_kw):
            return "def broken(\n"  # perpetual syntax error

        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite something.",
        )
        db.close()

        with patch("dev_harness.call_model", side_effect=_always_bad_ollama):
            import dev_harness
            monkeypatch.setattr(sys, "argv", ["dev_harness.py", job_id])
            with pytest.raises(SystemExit) as exc:
                dev_harness.main()
            assert exc.value.code != 0

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "failed"

    def test_model_output_forwarded_incrementally_via_subprocess(
        self, harness_env, tmp_path, monkeypatch
    ):
        """
        When dev_harness runs as a subprocess (as server/main.py does),
        model lines must appear on the harness stdout pipe as they are produced,
        not buffered until the process exits.

        The mock model emits N stderr lines with a small delay.  We read the
        harness pipe and verify that each marker line arrives individually,
        confirming the harness does not accumulate output before forwarding it.
        """
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        markers = ["MARKER_A", "MARKER_B", "MARKER_C", "MARKER_D"]
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite something.",
        )
        db.close()

        env = {
            **os.environ,
            "CLOWDER_OLLAMA_CMD": f"{sys.executable} {MOCK_MODEL}",
            "MOCK_MODEL_STDERR": "|".join(markers),
            "MOCK_MODEL_STDOUT": _GOOD_CODE,
            "MOCK_MODEL_DELAY": "0.05",   # 50 ms per line
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(AGENTS_DIR),
        }

        proc = subprocess.Popen(
            [sys.executable, "-u", str(AGENTS_DIR / "dev_harness.py"), job_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(tmp_path),
            env=env,
        )

        lines_with_time: list[tuple[float, str]] = []
        start = time.monotonic()
        for raw_line in proc.stdout:
            lines_with_time.append((time.monotonic() - start, raw_line.strip()))

        proc.wait(timeout=30)
        assert proc.returncode == 0, "Harness subprocess must exit 0 on success"

        # Every marker must appear somewhere in the harness output
        all_output = " ".join(l for _, l in lines_with_time)
        for marker in markers:
            assert marker in all_output, f"Marker '{marker}' not found in harness output"

        # The lines must have arrived at genuinely different timestamps
        # (if the harness were buffering, they'd all arrive at once at the end).
        marker_times = [t for t, l in lines_with_time if any(m in l for m in markers)]
        assert len(marker_times) >= 2
        assert max(marker_times) - min(marker_times) >= 0.03, (
            "Marker lines should be spread over time, not all arrive simultaneously"
        )

    def test_fails_when_internal_tests_fail(self, harness_env, monkeypatch):
        """If the test file exists and pytest keeps failing, the job must hard-fail."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        # Write a test that always fails
        (workspace / "tests" / "test_solution.py").write_text(_FAILING_TESTS, encoding="utf-8")

        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite an answer function.",
        )
        db.close()

        with patch("dev_harness.call_model", return_value=_GOOD_CODE):
            import dev_harness
            monkeypatch.setattr(sys, "argv", ["dev_harness.py", job_id])
            with pytest.raises(SystemExit) as exc:
                dev_harness.main()
            assert exc.value.code != 0

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "failed"
        assert "tests still failing" in row["termination_reason"]

    def test_stops_early_when_internal_tests_pass(self, harness_env, monkeypatch):
        """If tests pass on the first attempt, the harness completes with a single LLM call."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        # Write a passing test suite
        (workspace / "tests" / "test_solution.py").write_text(_PASSING_TESTS, encoding="utf-8")

        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite an answer function.",
        )
        db.close()

        call_count = [0]

        def _counting_ollama(prompt, **_kw):
            call_count[0] += 1
            return _GOOD_CODE

        with patch("dev_harness.call_model", side_effect=_counting_ollama):
            import dev_harness
            monkeypatch.setattr(sys, "argv", ["dev_harness.py", job_id])
            dev_harness.main()

        assert call_count[0] == 1, "Should have called the LLM exactly once"

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status, job_output FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "completed"
        assert "tests passed" in row["job_output"]

    def test_uses_test_failure_prompt_on_retry(self, harness_env, monkeypatch):
        """After a pytest failure, the second LLM call must use the test-failure prompt."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        (workspace / "tests" / "test_solution.py").write_text(_FAILING_TESTS, encoding="utf-8")

        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite an answer function.",
        )
        db.close()

        prompts_seen = []

        def _recording_ollama(prompt, **_kw):
            prompts_seen.append(prompt)
            return _GOOD_CODE

        with patch("dev_harness.call_model", side_effect=_recording_ollama):
            import dev_harness
            monkeypatch.setattr(sys, "argv", ["dev_harness.py", job_id])
            with pytest.raises(SystemExit):
                dev_harness.main()

        assert len(prompts_seen) >= 2, "Should have made at least two LLM calls"
        assert "FAILING TESTS OUTPUT" in prompts_seen[1]

    def test_completes_without_test_file(self, harness_env, monkeypatch):
        """When no test file exists, the harness completes after ruff passes."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        # No test file written — workspace/tests/ is empty
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite an answer function.",
        )
        db.close()

        with patch("dev_harness.call_model", return_value=_GOOD_CODE):
            import dev_harness
            monkeypatch.setattr(sys, "argv", ["dev_harness.py", job_id])
            dev_harness.main()

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status, job_output FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "completed"
        assert "no test file" in row["job_output"]


# ---------------------------------------------------------------------------
# test_harness
# ---------------------------------------------------------------------------


class TestTestHarness:
    """Tests for agents/test_harness.py."""

    def _run_main(self, job_id, monkeypatch):
        import test_harness
        monkeypatch.setattr(sys, "argv", ["test_harness.py", job_id])
        test_harness.main()

    def test_writes_test_file_to_workspace_tests_dir(self, harness_env, monkeypatch):
        """Test file must be written to workspace/tests/test_<filename>."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        monkeypatch.setenv("MOCK_MODEL_STDOUT", _FAILING_TESTS)
        monkeypatch.setenv("MOCK_MODEL_STDERR", "writing tests...")
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite a function that returns 42.",
        )
        db.close()

        self._run_main(job_id, monkeypatch)

        test_file = workspace / "tests" / "test_solution.py"
        assert test_file.exists(), "Test file must be created at workspace/tests/test_<filename>"

    def test_completes_job_in_db(self, harness_env, monkeypatch):
        """Job must be marked completed after valid tests are written and collected."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        monkeypatch.setenv("MOCK_MODEL_STDOUT", _FAILING_TESTS)
        monkeypatch.setenv("MOCK_MODEL_STDERR", "")
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite tests.",
        )
        db.close()

        self._run_main(job_id, monkeypatch)

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "completed"

    def test_fails_when_no_filename_in_prompt(self, harness_env, monkeypatch):
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="No filename here — just a bare task.",
        )
        db.close()

        with pytest.raises(SystemExit) as exc:
            self._run_main(job_id, monkeypatch)
        assert exc.value.code != 0

    def test_retries_on_ruff_failure(self, harness_env, monkeypatch):
        """If the first model response fails ruff, harness retries and succeeds."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        responses = ["def broken(\n", _FAILING_TESTS]
        call_count = [0]

        def _fake_ollama(prompt, **_kw):
            resp = responses[call_count[0] % len(responses)]
            call_count[0] += 1
            return resp

        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite tests.",
        )
        db.close()

        with patch("test_harness.call_model", side_effect=_fake_ollama):
            import test_harness
            monkeypatch.setattr(sys, "argv", ["test_harness.py", job_id])
            test_harness.main()

        assert call_count[0] >= 2
        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "completed"

    def test_accepts_tests_that_fail_at_runtime(self, harness_env, monkeypatch):
        """
        Tests that fail at runtime (returncode 1) are acceptable — implementation
        does not exist yet.  The harness must still complete the job.
        """
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        monkeypatch.setenv("MOCK_MODEL_STDOUT", _FAILING_TESTS)
        monkeypatch.setenv("MOCK_MODEL_STDERR", "")
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: missing_impl.py\n\nWrite tests for a missing module.",
        )
        db.close()

        self._run_main(job_id, monkeypatch)

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        # Even though pytest reports failures, the harness should complete the job
        assert row["status"] == "completed"

    def test_fails_job_on_collection_error(self, harness_env, monkeypatch):
        """A test file with import/syntax errors (returncode 2+) must fail the job."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        # ruff will accept this (it's syntactically valid), but pytest collection fails
        # because the import inside the test body is malformed at runtime.
        # We simulate collection error by patching run_pytest to return code 2.
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite tests.",
        )
        db.close()

        monkeypatch.setenv("MOCK_MODEL_STDOUT", _FAILING_TESTS)

        with patch("test_harness.run_pytest", return_value=(2, "collection error")):
            import test_harness
            monkeypatch.setattr(sys, "argv", ["test_harness.py", job_id])
            with pytest.raises(SystemExit) as exc:
                test_harness.main()
            assert exc.value.code != 0

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "failed"

    def test_retries_when_contradiction_detected(self, harness_env, monkeypatch):
        """
        When detect_test_contradictions returns a contradiction on iteration 1, the
        harness must log status='contradiction', increment the attempt, and retry.
        On the second attempt (no contradiction), the job completes successfully.
        """
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        detect_calls = [0]

        def _fake_detect(path):
            detect_calls[0] += 1
            if detect_calls[0] == 1:
                from dev_utils import Contradiction
                return [
                    Contradiction(
                        test_a="test_zero_ok",
                        test_b="test_zero_raises",
                        call_repr="fibonacci(0)",
                        outcome_a="== 0",
                        outcome_b="raises ValueError",
                    )
                ]
            return []

        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite tests.",
        )
        db.close()

        with patch("test_harness.call_model", return_value=_FAILING_TESTS):
            with patch("test_harness.detect_test_contradictions", side_effect=_fake_detect):
                import test_harness
                monkeypatch.setattr(sys, "argv", ["test_harness.py", job_id])
                test_harness.main()

        assert detect_calls[0] >= 2, "detect_test_contradictions must be called at least twice"

        db2 = _reopen_db(harness_env["tmp_path"])
        job_row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        action_rows = db2.conn.execute(
            "SELECT results FROM actions WHERE job_id = ? ORDER BY iteration",
            (job_id,),
        ).fetchall()
        db2.close()

        assert job_row["status"] == "completed"

        import json
        statuses = [json.loads(r["results"]).get("status") for r in action_rows if r["results"]]
        assert "contradiction" in statuses, (
            "At least one action must be logged with status='contradiction'"
        )

    def test_contradiction_feedback_included_in_retry_prompt(self, harness_env, monkeypatch):
        """
        When a contradiction is detected, the retry prompt shown to the LLM must
        include the CONTRADICTION(S) DETECTED message describing the conflicting calls.
        """
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        detect_calls = [0]

        def _fake_detect(path):
            detect_calls[0] += 1
            if detect_calls[0] == 1:
                from dev_utils import Contradiction
                return [
                    Contradiction(
                        test_a="test_ok",
                        test_b="test_raises",
                        call_repr="foo(0)",
                        outcome_a="== 0",
                        outcome_b="raises ValueError",
                    )
                ]
            return []

        prompts_seen = []

        def _recording_ollama(prompt, **_kw):
            prompts_seen.append(prompt)
            return _FAILING_TESTS

        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite tests.",
        )
        db.close()

        with patch("test_harness.call_model", side_effect=_recording_ollama):
            with patch("test_harness.detect_test_contradictions", side_effect=_fake_detect):
                import test_harness
                monkeypatch.setattr(sys, "argv", ["test_harness.py", job_id])
                test_harness.main()

        assert len(prompts_seen) >= 2, "Must have retried at least once"
        assert "CONTRADICTION" in prompts_seen[1], (
            "Retry prompt must contain the contradiction feedback"
        )
        assert "foo(0)" in prompts_seen[1], (
            "Retry prompt must name the contradicted call"
        )


# ---------------------------------------------------------------------------
# verify_harness
# ---------------------------------------------------------------------------


class TestVerifyHarness:
    """Tests for agents/verify_harness.py."""

    @pytest.fixture(autouse=True)
    def _default_llm_pass(self):
        """Default: LLM judge returns PASS. Tests needing FAIL override explicitly."""
        with patch("verify_harness.call_model", return_value="PASS\nCode is correct."):
            yield

    def _run_main(self, job_id, monkeypatch):
        import verify_harness
        monkeypatch.setattr(sys, "argv", ["verify_harness.py", job_id])
        verify_harness.main()

    def _seed(self, harness_env, *, filename="solution.py", test_content, impl_content=None):
        """Write test and optional impl files into the workspace."""
        workspace = harness_env["workspace"]
        test_file = workspace / "tests" / f"test_{filename}"
        test_file.write_text(test_content, encoding="utf-8")
        if impl_content:
            (workspace / filename).write_text(impl_content, encoding="utf-8")
        return workspace

    def test_completes_job_when_tests_pass(self, harness_env, monkeypatch):
        """When all tests pass, job must be marked completed."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        self._seed(harness_env, test_content=_PASSING_TESTS, impl_content=_GOOD_CODE)
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite an answer function.",
            agent_type="verifier",
        )
        db.close()

        self._run_main(job_id, monkeypatch)

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "completed"

    def test_spawns_retry_jobs_when_tests_fail(self, harness_env, monkeypatch):
        """When tests fail and dev-attempt count < MAX_DEV_RETRIES, retry jobs are spawned."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())
        pipeline_id = "pipe-retry"

        # Failing tests, no implementation
        self._seed(harness_env, test_content=_FAILING_TESTS)
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            pipeline_id=pipeline_id,
            stage_id="stage-1",
            prompt="FILENAME: solution.py\n\nWrite an answer function.",
            agent_type="verifier",
        )
        db.close()

        with patch("verify_harness.call_model", return_value="FAIL\nLogic is wrong."):
            self._run_main(job_id, monkeypatch)

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "waiting", "Verifier should be waiting while child dev runs"

        # Exactly one child dev job must be spawned
        new_jobs = db2.conn.execute(
            "SELECT agent_type, parent_job_id FROM jobs WHERE pipeline_id = ? AND job_id != ?",
            (pipeline_id, job_id),
        ).fetchall()
        assert len(new_jobs) == 1, "Exactly one child job should be spawned"
        assert new_jobs[0]["agent_type"] == "dev"
        assert new_jobs[0]["parent_job_id"] == job_id
        db2.close()

    def test_fails_job_when_max_dev_retries_exceeded(self, harness_env, monkeypatch):
        """After MAX_DEV_RETRIES dev attempts the verifier must fail permanently."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())
        pipeline_id = "pipe-max"
        original_prompt = "FILENAME: solution.py\n\nWrite something."

        self._seed(harness_env, test_content=_FAILING_TESTS)

        # Insert the verifier job first (dev jobs reference it via parent_job_id FK)
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            pipeline_id=pipeline_id,
            stage_id="stage-1",
            prompt=original_prompt,
            original_prompt=original_prompt,
            agent_type="verifier",
        )

        # Insert MAX_DEV_RETRIES existing dev jobs as children of this verifier
        # (count is now by parent_job_id, not by pipeline+prompt)
        for _ in range(hc.MAX_DEV_RETRIES):
            _insert_job(
                db,
                job_id=str(uuid.uuid4()),
                workspace_path=workspace,
                pipeline_id=pipeline_id,
                stage_id="stage-1",
                prompt=original_prompt,
                agent_type="dev",
                parent_job_id=job_id,
            )
        db.close()

        with patch("verify_harness.call_model", return_value="FAIL\nLogic is wrong."):
            with pytest.raises(SystemExit) as exc:
                self._run_main(job_id, monkeypatch)
        assert exc.value.code != 0

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "failed"

    def test_fails_when_test_file_missing(self, harness_env, monkeypatch):
        """If the expected test file does not exist, job must fail."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        # No test file is written — workspace/tests/ is empty
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite something.",
            agent_type="verifier",
        )
        db.close()

        with pytest.raises(SystemExit) as exc:
            self._run_main(job_id, monkeypatch)
        assert exc.value.code != 0

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        assert row["status"] == "failed"

    def test_fails_when_no_filename_in_prompt(self, harness_env, monkeypatch):
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="No filename line here.",
            agent_type="verifier",
        )
        db.close()

        with pytest.raises(SystemExit) as exc:
            self._run_main(job_id, monkeypatch)
        assert exc.value.code != 0

    def test_retries_on_pytest_collection_error(self, harness_env, monkeypatch):
        """A pytest collection error (returncode >= 2) should spawn a retry cycle,
        not hard-fail. The dev likely produced wrong function names or missing exports."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        self._seed(harness_env, test_content=_FAILING_TESTS)
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nTask.",
            agent_type="verifier",
        )
        db.close()

        call_count = [0]

        def _counting_ollama(prompt, **_kw):
            call_count[0] += 1
            return "PASS\nCode is correct."

        with patch("verify_harness.run_pytest", return_value=(2, "collection error")):
            with patch("verify_harness.call_model", side_effect=_counting_ollama):
                import verify_harness
                monkeypatch.setattr(sys, "argv", ["verify_harness.py", job_id])
                verify_harness.main()

        assert call_count[0] == 0, "LLM judge must not be called for collection errors"

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        new_jobs = db2.conn.execute(
            "SELECT agent_type, parent_job_id FROM jobs WHERE job_id != ? ORDER BY created_at",
            (job_id,),
        ).fetchall()
        db2.close()
        assert row["status"] == "waiting", "verifier should be waiting while child dev runs"
        assert len(new_jobs) == 1, "exactly one child dev job should be spawned"
        assert new_jobs[0]["agent_type"] == "dev"
        assert new_jobs[0]["parent_job_id"] == job_id

    def test_overrides_pass_when_pytest_fails(self, harness_env, monkeypatch):
        """When LLM says PASS but pytest failed (rc=1), verdict is overridden to UNKNOWN
        and a retry cycle is spawned instead of completing the job."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())
        pipeline_id = "pipe-override"

        self._seed(harness_env, test_content=_FAILING_TESTS, impl_content=_GOOD_CODE)
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            pipeline_id=pipeline_id,
            stage_id="stage-1",
            prompt="FILENAME: solution.py\n\nWrite an answer function.",
            agent_type="verifier",
        )
        db.close()

        # LLM says PASS, but pytest will return rc=1 (tests fail)
        with patch("verify_harness.call_model", return_value="PASS\nImplementation is correct."):
            self._run_main(job_id, monkeypatch)

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        new_jobs = db2.conn.execute(
            "SELECT agent_type, parent_job_id FROM jobs WHERE pipeline_id = ? AND job_id != ?",
            (pipeline_id, job_id),
        ).fetchall()
        db2.close()

        assert row["status"] == "waiting", (
            "Verifier should be waiting (not completed) when LLM PASS is overridden"
        )
        assert len(new_jobs) == 1, "A retry dev job must be spawned after the override"
        assert new_jobs[0]["agent_type"] == "dev"
        assert new_jobs[0]["parent_job_id"] == job_id

    def test_llm_judge_timeout_falls_back_to_pytest(self, harness_env, monkeypatch):
        """When LLM judge times out, pytest result is used as fallback."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        self._seed(harness_env, test_content=_PASSING_TESTS, impl_content=_GOOD_CODE)
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nWrite an answer function.",
            agent_type="verifier",
        )
        db.close()

        with patch(
            "verify_harness.call_model",
            side_effect=subprocess.TimeoutExpired(cmd="ollama", timeout=300),
        ):
            self._run_main(job_id, monkeypatch)

        db2 = _reopen_db(harness_env["tmp_path"])
        row = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        db2.close()
        # Tests pass, so fallback verdict is PASS → job completes
        assert row["status"] == "completed"

    def test_failure_context_includes_llm_critique(self, harness_env, monkeypatch):
        """When LLM says FAIL, its explanation is included in the retry dev job's prompt."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())
        pipeline_id = "pipe-critique"

        self._seed(harness_env, test_content=_FAILING_TESTS, impl_content=_GOOD_CODE)
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            pipeline_id=pipeline_id,
            stage_id="stage-1",
            prompt="FILENAME: solution.py\n\nWrite an answer function.",
            agent_type="verifier",
        )
        db.close()

        critique = "The function returns the wrong type."
        with patch("verify_harness.call_model", return_value=f"FAIL\n{critique}"):
            self._run_main(job_id, monkeypatch)

        db2 = _reopen_db(harness_env["tmp_path"])
        new_dev = db2.conn.execute(
            "SELECT prompt FROM jobs WHERE pipeline_id = ? AND agent_type = 'dev'",
            (pipeline_id,),
        ).fetchone()
        db2.close()
        assert new_dev is not None
        assert critique in new_dev["prompt"]
        assert "VERIFIER CRITIQUE" in new_dev["prompt"]

    def test_collection_error_skips_llm_judge(self, harness_env, monkeypatch):
        """A pytest collection error bypasses the LLM judge and goes straight to retry."""
        db = harness_env["db"]
        workspace = harness_env["workspace"]
        job_id = str(uuid.uuid4())

        self._seed(harness_env, test_content=_FAILING_TESTS)
        _insert_job(
            db,
            job_id=job_id,
            workspace_path=workspace,
            prompt="FILENAME: solution.py\n\nTask.",
            agent_type="verifier",
        )
        db.close()

        call_count = [0]

        def _counting_ollama(prompt, **_kw):
            call_count[0] += 1
            return "PASS\nCode is correct."

        with patch("verify_harness.run_pytest", return_value=(2, "collection error")):
            with patch("verify_harness.call_model", side_effect=_counting_ollama):
                import verify_harness
                monkeypatch.setattr(sys, "argv", ["verify_harness.py", job_id])
                verify_harness.main()

        assert call_count[0] == 0, "LLM judge must not be called for collection errors"

        db2 = _reopen_db(harness_env["tmp_path"])
        verifier_job_status = db2.conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        new_jobs = db2.conn.execute(
            "SELECT agent_type, parent_job_id FROM jobs WHERE job_id != ? ORDER BY created_at",
            (job_id,),
        ).fetchall()
        db2.close()

        assert verifier_job_status["status"] == "waiting", "verifier should be waiting while child dev runs"
        assert len(new_jobs) == 1, "exactly one child dev job should be spawned"
        assert new_jobs[0]["agent_type"] == "dev"
        assert new_jobs[0]["parent_job_id"] == job_id
