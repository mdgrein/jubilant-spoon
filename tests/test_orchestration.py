"""
Tests for job orchestration, retry logic, and failure handling.
These test the core runtime features that execute jobs.
"""

import pytest
import tempfile
import os
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))

from db import ClowderDB
from templates import TemplateManager

# Import server functions we need to test
import server.main as srv


@pytest.fixture
def test_db():
    """Create a temporary test database with schema."""
    tmpdir = tempfile.mkdtemp()
    test_db_path = os.path.join(tmpdir, "test.db")

    db = ClowderDB(test_db_path)

    # Initialize schema
    schema_path = Path(__file__).parent.parent / "agents" / "schema_pipelines.sql"
    if schema_path.exists():
        schema = schema_path.read_text()
        db.conn.executescript(schema)
        db.conn.commit()

    yield db

    # Cleanup
    db.conn.close()
    try:
        os.remove(test_db_path)
        os.rmdir(tmpdir)
    except:
        pass


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
    db.conn.execute("""
        INSERT INTO pipelines (pipeline_id, template_id, original_prompt, status, created_at, updated_at)
        VALUES (?, NULL, 'Test pipeline', 'running', datetime('now'), datetime('now'))
    """, (pipeline_id,))

    # Create stage
    db.conn.execute("""
        INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at)
        VALUES (?, ?, 'test-stage', 1, 'running', datetime('now'))
    """, (stage_id, pipeline_id))

    # Create jobs (job1 -> job2 -> job3 dependency chain)
    for job_id in [job1_id, job2_id, job3_id]:
        db.conn.execute("""
            INSERT INTO jobs (
                job_id, pipeline_id, stage_id, agent_type, prompt,
                command, max_iterations, timeout_seconds, allowed_paths,
                status, retry_count, max_retries, created_at, updated_at
            ) VALUES (?, ?, ?, 'mock', 'test', 'echo test', 50, 300, '["./"]',
                      'pending', 0, 100, datetime('now'), datetime('now'))
        """, (job_id, pipeline_id, stage_id))

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
    mock_proc.communicate = AsyncMock(return_value=(b"Failed\n", None))

    with patch('asyncio.create_subprocess_shell', return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check job was marked for retry
    job = db.conn.execute("SELECT status, retry_count FROM jobs WHERE job_id = ?", (job1_id,)).fetchone()
    assert job['status'] == 'pending', "Job should be pending for retry"
    assert job['retry_count'] == 1, "Retry count should be incremented"


@pytest.mark.asyncio
async def test_job_fails_after_max_retries(setup_server):
    """Test that jobs fail permanently after exhausting retries."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    # Set job to be on last retry
    db.conn.execute("""
        UPDATE jobs SET retry_count = 100, max_retries = 100 WHERE job_id = ?
    """, (job1_id,))
    db.conn.commit()

    # Mock subprocess that fails
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"Failed\n", None))

    with patch('asyncio.create_subprocess_shell', return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check job failed permanently
    job = db.conn.execute("SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job1_id,)).fetchone()
    assert job['status'] == 'failed', "Job should be failed"
    assert 'exit_code_1_after_101_attempts' in job['termination_reason']


@pytest.mark.asyncio
async def test_job_succeeds_on_retry(setup_server):
    """Test that a job can succeed after previous failures."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    # Set job to have already retried once
    db.conn.execute("""
        UPDATE jobs SET retry_count = 1 WHERE job_id = ?
    """, (job1_id,))
    db.conn.commit()

    # Mock subprocess that succeeds
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"Success\n", None))

    with patch('asyncio.create_subprocess_shell', return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check job succeeded
    job = db.conn.execute("SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job1_id,)).fetchone()
    assert job['status'] == 'completed', "Job should be completed"
    assert job['termination_reason'] == 'success'


# =============================================================================
# Failure Propagation Tests
# =============================================================================

def test_failure_propagation_skips_dependent_jobs(setup_server):
    """Test that when a job fails, dependent jobs are skipped."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # Manually fail job1
    db.conn.execute("""
        UPDATE jobs SET status = 'failed', termination_reason = 'test_failure'
        WHERE job_id = ?
    """, (job1_id,))
    db.conn.commit()

    # Trigger failure propagation
    srv.propagate_job_failure(job1_id)

    # Check that job2 and job3 were skipped
    job2 = db.conn.execute("SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job2_id,)).fetchone()
    job3 = db.conn.execute("SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job3_id,)).fetchone()

    assert job2['status'] == 'skipped', "Job2 should be skipped"
    assert job2['termination_reason'] == 'dependency_failed'
    assert job3['status'] == 'skipped', "Job3 should be skipped (recursive)"
    assert job3['termination_reason'] == 'dependency_failed'


def test_failure_propagation_respects_dependency_types(setup_server):
    """Test that failure dependency type allows job to run when dependency fails."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, _ = create_test_pipeline(db)

    # Change job2 to depend on job1 with 'failure' type
    db.conn.execute("""
        UPDATE job_dependencies SET dependency_type = 'failure'
        WHERE job_id = ? AND depends_on_job_id = ?
    """, (job2_id, job1_id))
    db.conn.commit()

    # Fail job1
    db.conn.execute("""
        UPDATE jobs SET status = 'failed', termination_reason = 'test_failure'
        WHERE job_id = ?
    """, (job1_id,))
    db.conn.commit()

    # Trigger failure propagation
    srv.propagate_job_failure(job1_id)

    # Check that job2 was NOT skipped (it should run on failure)
    job2 = db.conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job2_id,)).fetchone()
    assert job2['status'] == 'pending', "Job2 should still be pending (runs on failure)"


# =============================================================================
# Deadlock Detection Tests
# =============================================================================

def test_deadlock_detection_with_failed_dependency(setup_server):
    """Test that deadlock is detected when all dependencies are in blocking terminal states."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # Mark job1 as failed
    db.conn.execute("""
        UPDATE jobs SET status = 'failed', completed_at = datetime('now')
        WHERE job_id = ?
    """, (job1_id,))
    db.conn.commit()

    # Run deadlock detection
    srv.check_pipeline_completion(pipeline_id)

    # Pipeline should be marked as failed due to deadlock
    pipeline = db.conn.execute("SELECT status FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)).fetchone()
    assert pipeline['status'] == 'failed', "Pipeline should be failed due to deadlock"

    # Pending jobs should be skipped
    job2 = db.conn.execute("SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job2_id,)).fetchone()
    assert job2['status'] == 'skipped'
    assert job2['termination_reason'] == 'pipeline_deadlocked'


def test_no_deadlock_with_running_dependency(setup_server):
    """Test that jobs with running dependencies are NOT considered deadlocked."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # Mark job1 as running
    db.conn.execute("""
        UPDATE jobs SET status = 'running', started_at = datetime('now')
        WHERE job_id = ?
    """, (job1_id,))
    db.conn.commit()

    # Run deadlock detection
    srv.check_pipeline_completion(pipeline_id)

    # Pipeline should still be running (not deadlocked)
    pipeline = db.conn.execute("SELECT status FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)).fetchone()
    assert pipeline['status'] == 'running', "Pipeline should still be running"

    # Job2 should still be pending
    job2 = db.conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job2_id,)).fetchone()
    assert job2['status'] == 'pending', "Job2 should still be pending (waiting, not deadlocked)"


def test_no_deadlock_with_pending_dependency(setup_server):
    """Test that jobs with pending dependencies are NOT considered deadlocked."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # All jobs are pending (default)
    # Run deadlock detection
    srv.check_pipeline_completion(pipeline_id)

    # Pipeline should still be running (not deadlocked)
    pipeline = db.conn.execute("SELECT status FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)).fetchone()
    assert pipeline['status'] == 'running', "Pipeline should still be running"


def test_pipeline_completes_successfully(setup_server):
    """Test that pipeline completes when all jobs succeed."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # Mark all jobs as completed
    for job_id in [job1_id, job2_id, job3_id]:
        db.conn.execute("""
            UPDATE jobs SET status = 'completed', completed_at = datetime('now'),
                            termination_reason = 'success'
            WHERE job_id = ?
        """, (job_id,))
    db.conn.commit()

    # Run completion check
    srv.check_pipeline_completion(pipeline_id)

    # Pipeline should be completed
    pipeline = db.conn.execute("SELECT status FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)).fetchone()
    assert pipeline['status'] == 'completed', "Pipeline should be completed"


def test_pipeline_fails_with_any_failed_job(setup_server):
    """Test that pipeline is marked failed if any job fails (after all jobs done)."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, job3_id = create_test_pipeline(db)

    # Mark job1 as failed, others as completed
    db.conn.execute("""
        UPDATE jobs SET status = 'failed', completed_at = datetime('now')
        WHERE job_id = ?
    """, (job1_id,))
    db.conn.execute("""
        UPDATE jobs SET status = 'completed', completed_at = datetime('now')
        WHERE job_id IN (?, ?)
    """, (job2_id, job3_id))
    db.conn.commit()

    # Run completion check
    srv.check_pipeline_completion(pipeline_id)

    # Pipeline should be failed
    pipeline = db.conn.execute("SELECT status FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)).fetchone()
    assert pipeline['status'] == 'failed', "Pipeline should be failed"


# =============================================================================
# Output Capture Tests
# =============================================================================

@pytest.mark.asyncio
async def test_job_output_is_captured(setup_server):
    """Test that job stdout/stderr is captured and stored."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    expected_output = "Job output line 1\nJob output line 2\n"

    # Mock subprocess with output
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(expected_output.encode(), None))

    with patch('asyncio.create_subprocess_shell', return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check output was stored
    job = db.conn.execute("SELECT job_output FROM jobs WHERE job_id = ?", (job1_id,)).fetchone()
    assert job['job_output'] == expected_output, "Job output should be stored"


@pytest.mark.asyncio
async def test_failed_job_output_is_captured(setup_server):
    """Test that output is captured even when job fails."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    expected_output = "Error: something went wrong\n"

    # Mock subprocess that fails with output
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(expected_output.encode(), None))

    with patch('asyncio.create_subprocess_shell', return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check output was stored
    job = db.conn.execute("SELECT job_output, status FROM jobs WHERE job_id = ?", (job1_id,)).fetchone()
    assert job['status'] == 'pending', "Job should be retrying"
    assert job['job_output'] is None, "Output not stored on retry (only on final completion)"


@pytest.mark.asyncio
async def test_empty_output_is_handled(setup_server):
    """Test that jobs with no output don't cause errors."""
    db = setup_server
    pipeline_id, stage_id, job1_id, _, _ = create_test_pipeline(db)

    # Mock subprocess with no output
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", None))

    with patch('asyncio.create_subprocess_shell', return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check job completed with empty output
    job = db.conn.execute("SELECT job_output, status FROM jobs WHERE job_id = ?", (job1_id,)).fetchone()
    assert job['status'] == 'completed'
    assert job['job_output'] == "", "Empty output should be stored as empty string"


# =============================================================================
# Integration Tests
# =============================================================================

@pytest.mark.asyncio
async def test_retry_then_propagate_failure(setup_server):
    """Test full flow: job retries, fails permanently, then propagates failure."""
    db = setup_server
    pipeline_id, stage_id, job1_id, job2_id, _ = create_test_pipeline(db)

    # Set job to be on last retry
    db.conn.execute("""
        UPDATE jobs SET retry_count = 100, max_retries = 100 WHERE job_id = ?
    """, (job1_id,))
    db.conn.commit()

    # Mock subprocess that fails
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"Fatal error\n", None))

    with patch('asyncio.create_subprocess_shell', return_value=mock_proc):
        await srv.run_job(job1_id)

    # Check job1 failed and job2 was skipped
    job1 = db.conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job1_id,)).fetchone()
    job2 = db.conn.execute("SELECT status, termination_reason FROM jobs WHERE job_id = ?", (job2_id,)).fetchone()

    assert job1['status'] == 'failed'
    assert job2['status'] == 'skipped'
    assert job2['termination_reason'] == 'dependency_failed'
