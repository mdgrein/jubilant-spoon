import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient

from server.main import app, db, template_manager


@pytest.fixture(autouse=True)
def setup_test_db():
    """Use a temporary database for each test."""
    # Create a temporary database file
    tmpdir = tempfile.mkdtemp()
    test_db_path = os.path.join(tmpdir, "test.db")

    # Replace the global db connection
    import server.main as srv
    original_db = srv.db
    original_template_manager = srv.template_manager
    original_pipeline_service = srv.pipeline_service

    srv.db = type(original_db)(test_db_path)
    srv.template_manager = type(original_template_manager)(srv.db)
    srv.pipeline_service = type(original_pipeline_service)(srv.db, srv.template_manager)

    # Initialize schema
    schema_path = Path(__file__).parent.parent / "agents" / "schema_pipelines.sql"
    if schema_path.exists():
        schema = schema_path.read_text()
        srv.db.conn.executescript(schema)
        srv.db.conn.commit()

    yield srv.db

    # Cleanup
    srv.db.conn.close()
    srv.db = original_db
    srv.template_manager = original_template_manager
    srv.pipeline_service = original_pipeline_service

    # Remove temp database
    try:
        os.remove(test_db_path)
        os.rmdir(tmpdir)
    except:
        pass


@pytest.fixture
def client():
    """TestClient with orchestration loop disabled."""
    # Prevent the orchestration loop from starting
    with patch("server.main.asyncio.create_task"):
        yield TestClient(app)


@pytest.fixture
def sample_template(setup_test_db):
    """Create a sample template in the database."""
    db = setup_test_db
    ts = db._timestamp()

    # Insert template
    db.conn.execute("""
        INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at)
        VALUES ('build', 'Build Pipeline', 'Compiles the project', ?, ?)
    """, (ts, ts))

    # Insert stage
    db.conn.execute("""
        INSERT INTO template_stages (template_stage_id, template_id, name, stage_order)
        VALUES ('build_stage_1', 'build', 'Default Stage', 0)
    """)

    # Insert jobs
    db.conn.execute("""
        INSERT INTO template_jobs (template_job_id, template_stage_id, agent_type, name, prompt_template)
        VALUES
            ('build_job_1', 'build_stage_1', 'dev', 'compiler', 'Compile the code'),
            ('build_job_2', 'build_stage_1', 'tester', 'tester', 'Run tests')
    """)

    db.conn.commit()


# --- Root ---


def test_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"message": "Clowder Server is running!"}


def test_ping(client):
    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"pong": True}


# --- Templates ---


def test_list_templates_empty(client):
    resp = client.get("/pipelines/templates")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_templates(client, sample_template):
    resp = client.get("/pipelines/templates")
    assert resp.status_code == 200
    assert "build" in resp.json()


def test_get_template_details(client, sample_template):
    resp = client.get("/pipelines/templates/build")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "build"
    assert data["name"] == "Build Pipeline"
    assert data["description"] == "Compiles the project"
    assert len(data["stages"]) == 1
    assert data["stages"][0]["name"] == "Default Stage"
    assert len(data["stages"][0]["jobs"]) == 2


def test_get_template_not_found(client):
    resp = client.get("/pipelines/templates/nonexistent")
    assert resp.status_code == 404


# --- Start pipeline ---


def test_start_pipeline(client, sample_template):
    resp = client.post(
        "/pipelines/build/start",
        json={"prompt": "Build my project", "workspace_path": "/tmp/workspace"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["template_id"] == "build"
    assert data["prompt"] == "Build my project"
    assert data["status"] == "pending"
    assert "pipeline_id" in data


def test_start_pipeline_not_found(client):
    resp = client.post(
        "/pipelines/nonexistent/start",
        json={"prompt": "Test", "workspace_path": "/tmp"}
    )
    assert resp.status_code == 404


def test_start_pipeline_creates_jobs(client, sample_template, setup_test_db):
    """Starting a pipeline should create jobs in the database."""
    resp = client.post(
        "/pipelines/build/start",
        json={"prompt": "Build my project", "workspace_path": "/tmp/workspace"}
    )
    pipeline_id = resp.json()["pipeline_id"]

    # Check that jobs were created
    jobs = setup_test_db.conn.execute("""
        SELECT job_id, agent_type, status FROM jobs WHERE pipeline_id = ?
    """, (pipeline_id,)).fetchall()

    assert len(jobs) == 2
    assert jobs[0]["status"] == "pending"
    assert jobs[1]["status"] == "pending"


# --- Running pipelines ---


def test_list_running_empty(client):
    resp = client.get("/pipelines/running")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_running_after_start(client, sample_template):
    client.post(
        "/pipelines/build/start",
        json={"prompt": "Build 1", "workspace_path": "/tmp"}
    )
    client.post(
        "/pipelines/build/start",
        json={"prompt": "Build 2", "workspace_path": "/tmp"}
    )

    resp = client.get("/pipelines/running")
    assert resp.status_code == 200
    pipelines = resp.json()
    assert len(pipelines) == 2


# --- Get pipeline ---


def test_get_pipeline(client, sample_template):
    start_resp = client.post(
        "/pipelines/build/start",
        json={"prompt": "Build my project", "workspace_path": "/tmp"}
    )
    pipeline_id = start_resp.json()["pipeline_id"]

    resp = client.get(f"/pipelines/{pipeline_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pipeline"]["pipeline_id"] == pipeline_id
    assert len(data["jobs"]) == 2


def test_get_pipeline_not_found(client):
    resp = client.get("/pipelines/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


# --- Stop pipeline ---


def test_stop_pipeline(client, sample_template):
    start_resp = client.post(
        "/pipelines/build/start",
        json={"prompt": "Build my project", "workspace_path": "/tmp"}
    )
    pipeline_id = start_resp.json()["pipeline_id"]

    resp = client.post(f"/pipelines/{pipeline_id}/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["pipeline_id"] == pipeline_id


def test_stop_pipeline_updates_database(client, sample_template, setup_test_db):
    """Stopping a pipeline should update its status in the database."""
    start_resp = client.post(
        "/pipelines/build/start",
        json={"prompt": "Build my project", "workspace_path": "/tmp"}
    )
    pipeline_id = start_resp.json()["pipeline_id"]

    client.post(f"/pipelines/{pipeline_id}/stop")

    # Check database
    row = setup_test_db.conn.execute("""
        SELECT status FROM pipelines WHERE pipeline_id = ?
    """, (pipeline_id,)).fetchone()

    assert row["status"] == "cancelled"
