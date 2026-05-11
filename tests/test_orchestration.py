"""
Tests for job orchestration, retry logic, and failure handling.
These test the core runtime features that execute jobs.
"""

import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

from db import ClowderDB

# Import server functions we need to test
import server.main as srv


@pytest.fixture
def test_db():
    """Create an in-memory test database with schema."""
    db = ClowderDB(":memory:")
    db.init_pipeline_schema()
    yield db
    db.conn.close()


@pytest.fixture
def setup_server(test_db):
    """Setup server module with test database."""
    # Replace global db
    original_db = srv.db
    srv.db = test_db

    yield test_db

    # Restore
    srv.db = original_db


def create_test_pipeline(db):
    """Helper to create a minimal pipeline with jobs for testing."""
    pipeline_id = "test-pipeline-1"
    stage_id = "test-stage-1"
    job1_id = "job-1"
    job2_id = "job-2"
    job3_id = "job-3"

    # Create pipeline (template_id can be NULL for tests)
    db.conn.execute(
        """
        INSERT INTO pipelines (pipeline_id, template_id, original_prompt, status, created_at, updated_at)
        VALUES (?, NULL, 'Test pipeline', 'running', datetime('now'), datetime('now'))
    """,
        (pipeline_id,),
    )

    # Create stage
    db.conn.execute(
        """
        INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at)
        VALUES (?, ?, 'test-stage', 1, 'running', datetime('now'))
    """,
        (stage_id, pipeline_id),
    )

    # Create jobs (job1 -> job2 -> job3 dependency chain)
    for job_id in [job1_id, job2_id, job3_id]:
        db.conn.execute(
            """
            INSERT INTO jobs (
                job_id, pipeline_id, stage_id, agent_type, prompt,
                command, max_iterations, timeout_seconds, allowed_paths,
                status, retry_count, max_retries, created_at, updated_at
            ) VALUES (?, ?, ?, 'mock', 'test', 'echo test', 50, 300, '["./"]',
                      'pending', 0, 100, datetime('now'), datetime('now'))
        """,
            (job_id, pipeline_id, stage_id),
        )

    # Add dependencies: job2 depends on job1, job3 depends on job2
    db.conn.execute("""
        INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type)
        VALUES ('job-2', 'job-1', 'success')
    """)
    db.conn.execute("""
        INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type)
        VALUES ('job-3', 'job-2', 'success')
    """)

    db.conn.commit()

    return pipeline_id, stage_id, job1_id, job2_id, job3_id


# =============================================================================
# Job Retry Logic Tests
# =============================================================================


@pytest.mark.asyncio
async def test_job_retries_on_failure(setup_server):
    """Test that jobs retry when they fail with non-zero exit code."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    # Mock subprocess that fails
    mock_proc = AsyncMock()
    mock_proc.returncode = 1  # Failure
    mock_proc.stdout.readline = AsyncMock(side_effect=[b"Failed\n", b""])

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check job was marked for retry
    job = db.conn.execute(
        "SELECT status, retry_count FROM jobs WHERE job_id = ?", (job1_id,)
    ).fetchone()
    assert job["status"] == "pending", "Job should be pending for retry"
    assert job["retry_count"] == 1, "Retry count should be incremented"


@pytest.mark.asyncio
async def test_job_fails_after_max_retries(setup_server):
    """Test that jobs fail permanently after exhausting retries."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    # Set job to be on last retry
    db.conn.execute(
        """
        UPDATE jobs SET retry_count = 100, max_retries = 100 WHERE job_id = ?
    """,
        (job1_id,),
    )
    db.conn.commit()

    # Mock subprocess that fails
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.stdout.readline = AsyncMock(side_effect=[b"Failed\n", b""])

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check job failed permanently
    job = db.conn.execute(
        "SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job1_id,)
    ).fetchone()
    assert job["status"] == "failed", "Job should be failed"
    assert "exit_code_1_after_101_attempts" in job["termination_reason"]


@pytest.mark.asyncio
async def test_job_succeeds_on_retry(setup_server):
    """Test that a job can succeed after previous failures."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    # Set job to have already retried once
    db.conn.execute(
        """
        UPDATE jobs SET retry_count = 1 WHERE job_id = ?
    """,
        (job1_id,),
    )
    db.conn.commit()

    # Mock subprocess that succeeds
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(side_effect=[b"Success\n", b""])

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check job succeeded
    job = db.conn.execute(
        "SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job1_id,)
    ).fetchone()
    assert job["status"] == "completed", "Job should be completed"
    assert job["termination_reason"] == "success"


# =============================================================================
# Failure Propagation Tests
# =============================================================================


def test_failure_propagation_skips_dependent_jobs(setup_server):
    """Test that when a job fails, dependent jobs are skipped."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # Manually fail job1
    db.conn.execute(
        """
        UPDATE jobs SET status = 'failed', termination_reason = 'test_failure'
        WHERE job_id = ?
    """,
        (job1_id,),
    )
    db.conn.commit()

    # Trigger failure propagation
    srv.propagate_job_failure(job1_id)

    # Check that job2 and job3 were skipped
    job2 = db.conn.execute(
        "SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job2_id,)
    ).fetchone()
    job3 = db.conn.execute(
        "SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job3_id,)
    ).fetchone()

    assert job2["status"] == "skipped", "Job2 should be skipped"
    assert job2["termination_reason"] == "dependency_failed"
    assert job3["status"] == "skipped", "Job3 should be skipped (recursive)"
    assert job3["termination_reason"] == "dependency_failed"


def test_failure_propagation_respects_dependency_types(setup_server):
    """Test that failure dependency type allows job to run when dependency fails."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, _ = create_test_pipeline(db)

    # Change job2 to depend on job1 with 'failure' type
    db.conn.execute(
        """
        UPDATE job_dependencies SET dependency_type = 'failure'
        WHERE job_id = ? AND depends_on_job_id = ?
    """,
        (job2_id, job1_id),
    )
    db.conn.commit()

    # Fail job1
    db.conn.execute(
        """
        UPDATE jobs SET status = 'failed', termination_reason = 'test_failure'
        WHERE job_id = ?
    """,
        (job1_id,),
    )
    db.conn.commit()

    # Trigger failure propagation
    srv.propagate_job_failure(job1_id)

    # Check that job2 was NOT skipped (it should run on failure)
    job2 = db.conn.execute(
        "SELECT status FROM jobs WHERE job_id = ?", (job2_id,)
    ).fetchone()
    assert job2["status"] == "pending", "Job2 should still be pending (runs on failure)"


def test_failure_propagation_cascades_through_skipped_to_completed_type_deps(
    setup_server,
):
    """
    When a job is skipped (never ran), downstream jobs with 'completed' dep type must also
    be skipped — not left pending as if they could run.

    Scenario:
      job-x (fails) → job-y ('success' dep on x → skipped) → job-z ('completed' dep on y)
    job-z must be skipped because job-y never ran and produced no output.
    """
    db = setup_server

    # Build a 3-job pipeline: x → y (success) → z (completed)
    db.conn.execute("""
        INSERT INTO pipelines (pipeline_id, template_id, original_prompt, status, created_at, updated_at)
        VALUES ('pipe-cascade', NULL, 'test', 'running', datetime('now'), datetime('now'))
    """)
    db.conn.execute("""
        INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at)
        VALUES ('stage-cascade', 'pipe-cascade', 's', 1, 'running', datetime('now'))
    """)
    for jid in ("job-x", "job-y", "job-z"):
        db.conn.execute(
            """
            INSERT INTO jobs (job_id, pipeline_id, stage_id, agent_type, prompt,
                command, max_iterations, timeout_seconds, allowed_paths,
                status, retry_count, max_retries, created_at, updated_at)
            VALUES (?, 'pipe-cascade', 'stage-cascade', 'mock', 'test', 'echo test', 50, 300,
                '["./"]', 'pending', 0, 100, datetime('now'), datetime('now'))
        """,
            (jid,),
        )
    db.conn.execute("""
        INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type)
        VALUES ('job-y', 'job-x', 'success')
    """)
    db.conn.execute("""
        INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type)
        VALUES ('job-z', 'job-y', 'completed')
    """)

    # Fail job-x
    db.conn.execute(
        "UPDATE jobs SET status='failed', termination_reason='test_failure' WHERE job_id='job-x'"
    )
    db.conn.commit()

    srv.propagate_job_failure("job-x")

    job_y = db.conn.execute("SELECT status FROM jobs WHERE job_id='job-y'").fetchone()
    job_z = db.conn.execute("SELECT status FROM jobs WHERE job_id='job-z'").fetchone()

    assert job_y["status"] == "skipped", (
        "job-y should be skipped (success dep on failed job-x)"
    )
    assert job_z["status"] == "skipped", (
        "job-z should be skipped — job-y never ran so 'completed' semantics don't apply"
    )


# =============================================================================
# Deadlock Detection Tests — dependency_type = 'completed'
# =============================================================================


def _make_completed_type_pipeline(db):
    """Two-job pipeline: job-b depends on job-a with dependency_type='completed'."""
    db.conn.execute("""
        INSERT INTO pipelines (pipeline_id, template_id, original_prompt, status, created_at, updated_at)
        VALUES ('pipe-c', NULL, 'test', 'running', datetime('now'), datetime('now'))
    """)
    db.conn.execute("""
        INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at)
        VALUES ('stage-c', 'pipe-c', 's', 1, 'running', datetime('now'))
    """)
    for jid in ("job-a", "job-b"):
        db.conn.execute(
            """
            INSERT INTO jobs (job_id, pipeline_id, stage_id, agent_type, prompt,
                command, max_iterations, timeout_seconds, allowed_paths,
                status, retry_count, max_retries, created_at, updated_at)
            VALUES (?, 'pipe-c', 'stage-c', 'mock', 'test', 'echo test', 50, 300, '["./"]',
                'pending', 0, 100, datetime('now'), datetime('now'))
        """,
            (jid,),
        )
    db.conn.execute("""
        INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type)
        VALUES ('job-b', 'job-a', 'completed')
    """)
    db.conn.commit()


def test_no_deadlock_completed_type_dep_when_prereq_completed(setup_server):
    """A 'completed'-type dependency whose prereq finished successfully must NOT deadlock."""
    db = setup_server
    _make_completed_type_pipeline(db)
    db.conn.execute(
        "UPDATE jobs SET status='completed', completed_at=datetime('now') WHERE job_id='job-a'"
    )
    db.conn.commit()

    srv.check_pipeline_completion("pipe-c")

    pipe = db.conn.execute(
        "SELECT status FROM pipelines WHERE pipeline_id='pipe-c'"
    ).fetchone()
    job_b = db.conn.execute("SELECT status FROM jobs WHERE job_id='job-b'").fetchone()
    # job-b is still pending (not deadlocked) because its 'completed'-type dep is satisfied
    assert pipe["status"] == "running", (
        "Pipeline should still be running, not deadlocked"
    )
    assert job_b["status"] == "pending", "job-b should remain pending, ready to run"


def test_no_deadlock_completed_type_dep_when_prereq_failed(setup_server):
    """A 'completed'-type dependency whose prereq failed must NOT deadlock either."""
    db = setup_server
    _make_completed_type_pipeline(db)
    db.conn.execute(
        "UPDATE jobs SET status='failed', completed_at=datetime('now') WHERE job_id='job-a'"
    )
    db.conn.commit()

    srv.check_pipeline_completion("pipe-c")

    pipe = db.conn.execute(
        "SELECT status FROM pipelines WHERE pipeline_id='pipe-c'"
    ).fetchone()
    job_b = db.conn.execute("SELECT status FROM jobs WHERE job_id='job-b'").fetchone()
    assert pipe["status"] == "running", (
        "Pipeline should still be running, not deadlocked"
    )
    assert job_b["status"] == "pending", (
        "job-b should remain pending (runs after failed dep too)"
    )


def test_ready_job_completed_type_dep_when_prereq_failed(setup_server):
    """find_ready_job must return job-b when its 'completed'-type prereq has failed."""
    db = setup_server
    _make_completed_type_pipeline(db)
    db.conn.execute(
        "UPDATE jobs SET status='failed', completed_at=datetime('now') WHERE job_id='job-a'"
    )
    db.conn.commit()

    ready = srv.find_ready_job()
    assert ready is not None, "Should find job-b as ready"
    assert ready["job_id"] == "job-b"


def test_ready_job_completed_type_dep_when_prereq_completed(setup_server):
    """find_ready_job must return job-b when its 'completed'-type prereq has completed."""
    db = setup_server
    _make_completed_type_pipeline(db)
    db.conn.execute(
        "UPDATE jobs SET status='completed', completed_at=datetime('now') WHERE job_id='job-a'"
    )
    db.conn.commit()

    ready = srv.find_ready_job()
    assert ready is not None, "Should find job-b as ready"
    assert ready["job_id"] == "job-b"


def test_ready_job_completed_type_dep_not_ready_while_prereq_pending(setup_server):
    """find_ready_job must NOT return job-b while its 'completed'-type prereq is still pending."""
    db = setup_server
    _make_completed_type_pipeline(db)
    # job-a stays pending

    ready = srv.find_ready_job()
    # Only job-a (no deps) should be ready, not job-b
    if ready is not None:
        assert ready["job_id"] == "job-a", (
            "Only job-a should be ready while job-b's dep is pending"
        )


# =============================================================================
# Deadlock Detection Tests
# =============================================================================


def test_deadlock_detection_with_failed_dependency(setup_server):
    """Test that deadlock is detected when all dependencies are in blocking terminal states."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # Mark job1 as failed
    db.conn.execute(
        """
        UPDATE jobs SET status = 'failed', completed_at = datetime('now')
        WHERE job_id = ?
    """,
        (job1_id,),
    )
    db.conn.commit()

    # Run deadlock detection
    srv.check_pipeline_completion(pipeline_id)

    # Pipeline should be marked as failed due to deadlock
    pipeline = db.conn.execute(
        "SELECT status FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)
    ).fetchone()
    assert pipeline["status"] == "failed", "Pipeline should be failed due to deadlock"

    # Pending jobs should be skipped
    job2 = db.conn.execute(
        "SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job2_id,)
    ).fetchone()
    assert job2["status"] == "skipped"
    assert job2["termination_reason"] == "pipeline_deadlocked"


def test_no_deadlock_with_running_dependency(setup_server):
    """Test that jobs with running dependencies are NOT considered deadlocked."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # Mark job1 as running
    db.conn.execute(
        """
        UPDATE jobs SET status = 'running', started_at = datetime('now')
        WHERE job_id = ?
    """,
        (job1_id,),
    )
    db.conn.commit()

    # Run deadlock detection
    srv.check_pipeline_completion(pipeline_id)

    # Pipeline should still be running (not deadlocked)
    pipeline = db.conn.execute(
        "SELECT status FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)
    ).fetchone()
    assert pipeline["status"] == "running", "Pipeline should still be running"

    # Job2 should still be pending
    job2 = db.conn.execute(
        "SELECT status FROM jobs WHERE job_id = ?", (job2_id,)
    ).fetchone()
    assert job2["status"] == "pending", (
        "Job2 should still be pending (waiting, not deadlocked)"
    )


def test_no_deadlock_with_pending_dependency(setup_server):
    """Test that jobs with pending dependencies are NOT considered deadlocked."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # All jobs are pending (default)
    # Run deadlock detection
    srv.check_pipeline_completion(pipeline_id)

    # Pipeline should still be running (not deadlocked)
    pipeline = db.conn.execute(
        "SELECT status FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)
    ).fetchone()
    assert pipeline["status"] == "running", "Pipeline should still be running"


def test_pipeline_completes_successfully(setup_server):
    """Test that pipeline completes when all jobs succeed."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # Mark all jobs as completed
    for job_id in [job1_id, job2_id, job3_id]:
        db.conn.execute(
            """
            UPDATE jobs SET status = 'completed', completed_at = datetime('now'),
                            termination_reason = 'success'
            WHERE job_id = ?
        """,
            (job_id,),
        )
    db.conn.commit()

    # Run completion check
    srv.check_pipeline_completion(pipeline_id)

    # Pipeline should be completed
    pipeline = db.conn.execute(
        "SELECT status FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)
    ).fetchone()
    assert pipeline["status"] == "completed", "Pipeline should be completed"


def test_pipeline_fails_with_any_failed_job(setup_server):
    """Test that pipeline is marked failed if any job fails (after all jobs done)."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # Mark job1 as failed, others as completed
    db.conn.execute(
        """
        UPDATE jobs SET status = 'failed', completed_at = datetime('now')
        WHERE job_id = ?
    """,
        (job1_id,),
    )
    db.conn.execute(
        """
        UPDATE jobs SET status = 'completed', completed_at = datetime('now')
        WHERE job_id IN (?, ?)
    """,
        (job2_id, job3_id),
    )
    db.conn.commit()

    # Run completion check
    srv.check_pipeline_completion(pipeline_id)

    # Pipeline should be failed
    pipeline = db.conn.execute(
        "SELECT status FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)
    ).fetchone()
    assert pipeline["status"] == "failed", "Pipeline should be failed"


# =============================================================================
# ANSI / Terminal Escape Stripping Tests
# =============================================================================


@pytest.mark.asyncio
async def test_ansi_sequences_stripped_from_job_output(setup_server):
    """ANSI terminal escape sequences must be stripped before storing job output."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    # Typical Ollama spinner frame: cursor-hide, col-1, spinner-char, erase-EOL, cursor-show
    spinner_frame = b"\x1b[?25l\x1b[1G\xe2\xa0\x8b \x1b[K\x1b[?25h\n"
    # Line that becomes entirely empty after ANSI stripping (cursor show/hide only)
    empty_after_strip = b"\x1b[?25h\x1b[?25l\n"
    real_line = b"fibonacci(10) = 55\n"

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(
        side_effect=[spinner_frame, empty_after_strip, real_line, b""]
    )

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        await srv.run_job(job1_id)

    job = db.conn.execute(
        "SELECT job_output FROM jobs WHERE job_id=?", (job1_id,)
    ).fetchone()
    output = job["job_output"]
    assert "\x1b" not in output, "ANSI ESC bytes should be stripped"
    assert "[?25h" not in output, "Terminal control codes must not appear in output"
    assert "[?25l" not in output
    assert "fibonacci(10) = 55" in output, "Real output must be preserved"


@pytest.mark.asyncio
async def test_ansi_only_lines_not_stored(setup_server):
    """Lines that contain only ANSI sequences (empty after strip) must not be stored."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    ansi_only = b"\x1b[?25h\x1b[?25l\x1b[?25h\x1b[?25l\n"
    real_line = b"done\n"

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(side_effect=[ansi_only, real_line, b""])

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        await srv.run_job(job1_id)

    output = db.conn.execute(
        "SELECT job_output FROM jobs WHERE job_id=?", (job1_id,)
    ).fetchone()["job_output"]
    lines = output.splitlines()
    assert lines == ["done"], f"Only 'done' should be stored, got: {lines}"


# =============================================================================
# LimitOverrunError Handling Tests
# =============================================================================


@pytest.mark.asyncio
async def test_oversized_line_does_not_crash_job(setup_server):
    """An asyncio LimitOverrunError (line > buffer limit) must not crash the job.

    The oversized chunk must be drained and skipped; subsequent lines must still
    be captured and the job must complete normally.
    """
    db = setup_server
    _, _, job1_id, _, _ = create_test_pipeline(db)

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    limit_err = asyncio.LimitOverrunError("chunk exceed the limit", consumed=65536)
    mock_proc.stdout.readline = AsyncMock(
        side_effect=[limit_err, b"real output\n", b""]
    )
    mock_proc.stdout.read = AsyncMock(return_value=b"x" * 65536)

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        await srv.run_job(job1_id)

    job = db.conn.execute(
        "SELECT status, job_output FROM jobs WHERE job_id=?", (job1_id,)
    ).fetchone()
    assert job["status"] == "completed"
    assert "real output" in job["job_output"]


# =============================================================================
# Output Capture Tests
# =============================================================================


@pytest.mark.asyncio
async def test_job_output_is_captured(setup_server):
    """Test that job stdout/stderr is captured and stored."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    expected_output = "Job output line 1\nJob output line 2"

    # Mock subprocess with output (readline returns lines as bytes, empty bytes signals EOF)
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(
        side_effect=[b"Job output line 1\n", b"Job output line 2\n", b""]
    )

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check output was stored
    job = db.conn.execute(
        "SELECT job_output FROM jobs WHERE job_id = ?", (job1_id,)
    ).fetchone()
    assert job["job_output"] == expected_output, "Job output should be stored"


@pytest.mark.asyncio
async def test_failed_job_output_is_captured(setup_server):
    """Test that output is captured and persisted even when job fails (retries)."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    # Mock subprocess that fails with output
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.stdout.readline = AsyncMock(
        side_effect=[b"Error: something went wrong\n", b""]
    )

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        await srv.run_job(job1_id)

    # On retry, job_output IS written (so live log streaming works)
    job = db.conn.execute(
        "SELECT job_output, status FROM jobs WHERE job_id = ?", (job1_id,)
    ).fetchone()
    assert job["status"] == "pending", "Job should be retrying"
    assert "Error: something went wrong" in (job["job_output"] or ""), (
        "Output should be stored on retry for live log streaming"
    )


@pytest.mark.asyncio
async def test_empty_output_is_handled(setup_server):
    """Test that jobs with no output don't cause errors."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    # Mock subprocess with no output (readline immediately returns empty bytes = EOF)
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(return_value=b"")

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check job completed with empty output
    job = db.conn.execute(
        "SELECT job_output, status FROM jobs WHERE job_id = ?", (job1_id,)
    ).fetchone()
    assert job["status"] == "completed"
    assert job["job_output"] == "", "Empty output should be stored as empty string"


# =============================================================================
# Command Routing Tests
# =============================================================================


@pytest.mark.asyncio
async def test_run_job_uses_harness_by_default(setup_server):
    """Jobs with no command and no special vendor use the default harness."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    # Clear the baked-in command so routing falls through to default
    db.conn.execute("UPDATE jobs SET command = NULL WHERE job_id = ?", (job1_id,))
    db.conn.commit()

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(return_value=b"")

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc) as mock_shell:
        await srv.run_job(job1_id)

    called_cmd = mock_shell.call_args[0][0]
    assert called_cmd == f"python harnesses/harness.py {job1_id}"


@pytest.mark.asyncio
async def test_run_job_routes_mock_vendor_to_mock_model(setup_server):
    """Jobs with vendor='mock' and no command use mock_model harness."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    db.conn.execute(
        "UPDATE jobs SET command = NULL, vendor = 'mock' WHERE job_id = ?", (job1_id,)
    )
    db.conn.commit()

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(return_value=b"")

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc) as mock_shell:
        await srv.run_job(job1_id)

    called_cmd = mock_shell.call_args[0][0]
    assert called_cmd == "python harnesses/mock_model.py"


@pytest.mark.asyncio
async def test_run_job_explicit_command_takes_priority_over_vendor(setup_server):
    """An explicit command column always wins, even when vendor='mock'."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    db.conn.execute(
        "UPDATE jobs SET command = 'echo custom', vendor = 'mock' WHERE job_id = ?",
        (job1_id,),
    )
    db.conn.commit()

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(return_value=b"")

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc) as mock_shell:
        await srv.run_job(job1_id)

    called_cmd = mock_shell.call_args[0][0]
    assert called_cmd == "echo custom"


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.mark.asyncio
async def test_retry_then_propagate_failure(setup_server):
    """Test full flow: job retries, fails permanently, then propagates failure."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, _ = create_test_pipeline(db)

    # Set job to be on last retry
    db.conn.execute(
        """
        UPDATE jobs SET retry_count = 100, max_retries = 100 WHERE job_id = ?
    """,
        (job1_id,),
    )
    db.conn.commit()

    # Mock subprocess that fails
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.stdout.readline = AsyncMock(side_effect=[b"Fatal error\n", b""])

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check job1 failed and job2 was skipped
    job1 = db.conn.execute(
        "SELECT status FROM jobs WHERE job_id = ?", (job1_id,)
    ).fetchone()
    job2 = db.conn.execute(
        "SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job2_id,)
    ).fetchone()

    assert job1["status"] == "failed"
    assert job2["status"] == "skipped"
    assert job2["termination_reason"] == "dependency_failed"


# =============================================================================
# Live Log Streaming Tests
# =============================================================================


@pytest.mark.asyncio
async def test_time_based_flush_fires_under_line_threshold(setup_server):
    """Flush fires after 1 s even when fewer than LOG_FLUSH_EVERY lines accumulated."""
    db = setup_server
    _, _, job1_id, _, _ = create_test_pipeline(db)

    # Only 1 line — well below LOG_FLUSH_EVERY, but time says 1.5 s elapsed
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(side_effect=[b"slow line\n", b""])

    # Spy by wrapping db.conn (sqlite3.Connection.execute is read-only in 3.14)
    mid_run_flushes = []
    real_conn = db.conn

    class SpyConn:
        def execute(self, sql, params=()):
            if sql.strip().startswith("UPDATE jobs SET job_output"):
                mid_run_flushes.append(True)
            return real_conn.execute(sql, params)

        def commit(self):
            return real_conn.commit()

        def __getattr__(self, name):
            return getattr(real_conn, name)

    db.conn = SpyConn()
    try:
        time_seq = iter([0.0, 1.5])  # init=0, check after line=1.5 → triggers flush
        with (
            patch("server.main.time") as mock_time,
            patch("asyncio.create_subprocess_shell", return_value=mock_proc),
        ):
            mock_time.monotonic.side_effect = lambda: next(time_seq)
            await srv.run_job(job1_id)
    finally:
        db.conn = real_conn

    assert mid_run_flushes, "Expected at least one time-based mid-run flush"


@pytest.mark.asyncio
async def test_live_log_flush_written_during_execution(setup_server):
    """job_output is flushed to DB mid-run (every LOG_FLUSH_EVERY lines)."""
    db = setup_server
    _, _, job1_id, _, _ = create_test_pipeline(db)

    # Produce exactly LOG_FLUSH_EVERY lines so a flush fires, then EOF
    flush_every = srv.LOG_FLUSH_EVERY
    lines = [f"line {i}\n".encode() for i in range(flush_every)] + [b""]

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout.readline = AsyncMock(side_effect=lines)

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        await srv.run_job(job1_id)

    job = db.conn.execute(
        "SELECT job_output FROM jobs WHERE job_id = ?", (job1_id,)
    ).fetchone()
    assert job["job_output"] is not None, "job_output should be written"
    for i in range(flush_every):
        assert f"line {i}" in job["job_output"]


@pytest.mark.asyncio
async def test_retry_appends_not_replaces_log(setup_server):
    """On retry, the previous attempt's output is preserved with a separator."""
    db = setup_server
    _, _, job1_id, _, _ = create_test_pipeline(db)

    # First run: fails (retry_count=0 → incremented to 1, status→pending)
    mock_proc_fail = AsyncMock()
    mock_proc_fail.returncode = 1
    mock_proc_fail.stdout.readline = AsyncMock(
        side_effect=[b"attempt one output\n", b""]
    )

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc_fail):
        await srv.run_job(job1_id)

    job_after_retry = db.conn.execute(
        "SELECT status, retry_count, job_output FROM jobs WHERE job_id = ?", (job1_id,)
    ).fetchone()
    assert job_after_retry["status"] == "pending"
    assert job_after_retry["retry_count"] == 1
    assert "attempt one output" in (job_after_retry["job_output"] or "")

    # Second run: succeeds
    mock_proc_ok = AsyncMock()
    mock_proc_ok.returncode = 0
    mock_proc_ok.stdout.readline = AsyncMock(side_effect=[b"attempt two output\n", b""])

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc_ok):
        await srv.run_job(job1_id)

    job_final = db.conn.execute(
        "SELECT status, job_output FROM jobs WHERE job_id = ?", (job1_id,)
    ).fetchone()
    assert job_final["status"] == "completed"
    assert "attempt one output" in job_final["job_output"], (
        "first attempt must be preserved"
    )
    assert "attempt two output" in job_final["job_output"], (
        "second attempt must be present"
    )
    assert "--- Attempt 2 ---" in job_final["job_output"], "separator must appear"


# =============================================================================
# Scheduler Loop Tests
# =============================================================================


@pytest.mark.asyncio
async def test_tick_scheduler_calls_record_fired_for_due_schedule(setup_server):
    """tick_scheduler fires due schedules and calls record_fired."""
    due_schedule = {
        "schedule_id": "sched-1",
        "template_id": "tpl-1",
        "prompt": "do work",
        "workspace_path": "/workspace",
        "name": "Test",
    }

    mock_scheduler = MagicMock()
    mock_scheduler.get_due_schedules.return_value = [due_schedule]

    mock_tm = MagicMock()
    mock_tm.instantiate_template.return_value = "pipe-1"

    original_scheduler = srv.scheduler_service
    original_tm = srv.template_manager
    srv.scheduler_service = mock_scheduler
    srv.template_manager = mock_tm

    try:
        await srv.tick_scheduler()
    finally:
        srv.scheduler_service = original_scheduler
        srv.template_manager = original_tm

    mock_tm.instantiate_template.assert_called_once_with(
        template_id="tpl-1",
        original_prompt="do work",
        workspace_path="/workspace",
    )
    mock_scheduler.record_fired.assert_called_once()
    call_kwargs = mock_scheduler.record_fired.call_args
    assert call_kwargs[1]["schedule_id"] == "sched-1"
    assert call_kwargs[1]["pipeline_id"] == "pipe-1"


@pytest.mark.asyncio
async def test_tick_scheduler_no_due_schedules(setup_server):
    """tick_scheduler does nothing when no schedules are due."""
    mock_scheduler = MagicMock()
    mock_scheduler.get_due_schedules.return_value = []

    mock_pipeline = MagicMock()

    original_scheduler = srv.scheduler_service
    original_pipeline = srv.pipeline_service
    srv.scheduler_service = mock_scheduler
    srv.pipeline_service = mock_pipeline

    try:
        await srv.tick_scheduler()
    finally:
        srv.scheduler_service = original_scheduler
        srv.pipeline_service = original_pipeline

    mock_pipeline.create_pipeline.assert_not_called()
    mock_scheduler.record_fired.assert_not_called()


@pytest.mark.asyncio
async def test_tick_scheduler_one_failure_does_not_block_others(setup_server):
    """A failing schedule does not prevent subsequent schedules from firing."""
    schedules = [
        {
            "schedule_id": "s1",
            "template_id": "tpl-a",
            "prompt": "a",
            "workspace_path": "/w",
            "name": "A",
        },
        {
            "schedule_id": "s2",
            "template_id": "tpl-b",
            "prompt": "b",
            "workspace_path": "/w",
            "name": "B",
        },
    ]

    mock_scheduler = MagicMock()
    mock_scheduler.get_due_schedules.return_value = schedules

    mock_tm = MagicMock()
    # First call raises, second succeeds
    mock_tm.instantiate_template.side_effect = [
        ValueError("template missing"),
        "pipe-b",
    ]

    original_scheduler = srv.scheduler_service
    original_tm = srv.template_manager
    srv.scheduler_service = mock_scheduler
    srv.template_manager = mock_tm

    try:
        await srv.tick_scheduler()  # must not raise
    finally:
        srv.scheduler_service = original_scheduler
        srv.template_manager = original_tm

    assert mock_tm.instantiate_template.call_count == 2
    # record_fired should only have been called for the second (successful) schedule
    assert mock_scheduler.record_fired.call_count == 1
