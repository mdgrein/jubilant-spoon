import json
import os
import sqlite3
import tempfile
import pytest
import sys
from pathlib import Path

_PYTEST_TMP = Path("D:/development/pytest-tmp")
_PYTEST_TMP.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session", autouse=True)
def redirect_temp_to_d_drive():
    """Redirect all temp file creation to D: drive to avoid filling C:."""
    prev_tmpdir = tempfile.tempdir
    prev_env = {k: os.environ.get(k) for k in ("TMP", "TEMP", "TMPDIR")}
    tmpdir_str = str(_PYTEST_TMP)
    tempfile.tempdir = tmpdir_str
    os.environ["TMP"] = tmpdir_str
    os.environ["TEMP"] = tmpdir_str
    os.environ["TMPDIR"] = tmpdir_str
    yield
    tempfile.tempdir = prev_tmpdir
    for k, v in prev_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))
from db import ClowderDB  # noqa: E402


@pytest.fixture
def clowder_db():
    """Provides a temporary, in-memory ClowderDB instance for tests."""
    db = ClowderDB(":memory:")
    # Initialize the schema for the in-memory database
    db.init_schema()
    yield db
    db.close()


@pytest.fixture
def harness_env(tmp_path, monkeypatch):
    """
    Provides a isolated environment for harness tests:
    - A temporary directory for the workspace.
    - A temporary, in-memory ClowderDB instance.
    - monkeypatch for sys.argv and db.py ClowderDB instance.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Use a separate in-memory DB for harness tests too, to prevent global state pollution
    db = ClowderDB(":memory:")
    db.init_schema()

    # Patch ClowderDB's internal connection to use our in-memory DB
    # This ensures that _any_ ClowderDB instance created within the harness's scope
    # (e.g., inside load_job) uses this temporary connection.
    monkeypatch.setattr(ClowderDB, "conn", db.conn)

    yield {
        "db": db,
        "tmp_path": tmp_path,
        "workspace": workspace,
    }
    db.close()


# Helper function for inserting jobs in tests
def _insert_job(
    db: ClowderDB,
    job_id: str,
    workspace_path: Path,
    prompt: str,
    template_id: str = "test-template",
):
    """Inserts a job directly into the provided test database."""
    # Ensure all necessary tables exist if not already created by init_schema
    # This might be redundant if init_schema is always called, but good safeguard.
    try:
        db.conn.execute("SELECT * FROM jobs LIMIT 1")
    except sqlite3.OperationalError:
        # If jobs table doesn't exist, re-initialize schema
        db.init_schema()

    db.conn.execute(
        """
        INSERT INTO jobs (job_id, pipeline_id, stage_name, agent_type, prompt, original_prompt,
                          status, created_at, updated_at, allowed_paths)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            job_id,  # job_id
            "pipeline-123",  # pipeline_id
            "stage-1",  # stage_name
            "test_agent",  # agent_type
            prompt,  # prompt
            prompt,  # original_prompt
            "pending",  # status
            db._timestamp(),  # created_at
            db._timestamp(),  # updated_at
            json.dumps([str(workspace_path)]),  # allowed_paths
        ),
    )
    db.conn.commit()


# Mocking related to _FAILING_TESTS and _PASSING_TESTS
_FAILING_TESTS = """
import pytest
def test_solution_fails():
    assert False
"""
_PASSING_TESTS = """
import pytest
def test_solution_passes():
    assert True
"""
