import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.main import app


@pytest.fixture(autouse=True)
def setup_test_db():
    """Use an in-memory database for each test."""
    import server.main as srv

    original_db = srv.db
    original_template_manager = srv.template_manager
    original_pipeline_service = srv.pipeline_service
    original_scheduler_service = srv.scheduler_service

    srv.db = type(original_db)(":memory:")
    srv.db.init_pipeline_schema()
    srv.template_manager = type(original_template_manager)(srv.db)
    srv.pipeline_service = type(original_pipeline_service)(srv.db, srv.template_manager)
    srv.scheduler_service = type(original_scheduler_service)(
        srv.db, srv.pipeline_service
    )

    yield srv.db

    # Cleanup
    srv.db.conn.close()
    srv.db = original_db
    srv.template_manager = original_template_manager
    srv.pipeline_service = original_pipeline_service
    srv.scheduler_service = original_scheduler_service


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
    db.conn.execute(
        """
        INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at)
        VALUES ('build', 'Build Pipeline', 'Compiles the project', ?, ?)
    """,
        (ts, ts),
    )

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
    template_ids = [t["template_id"] for t in resp.json()]
    assert "build" in template_ids


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


# --- as-spec endpoint ---


def test_as_spec_returns_stages_and_dependencies(client, sample_template):
    resp = client.get("/pipelines/templates/build/as-spec")
    assert resp.status_code == 200
    data = resp.json()
    assert "stages" in data
    assert "dependencies" in data


def test_as_spec_not_found(client):
    resp = client.get("/pipelines/templates/nonexistent/as-spec")
    assert resp.status_code == 404


def test_as_spec_then_run_creates_jobs(client, sample_template, setup_test_db):
    """GET as-spec + POST /run should create jobs in the database."""
    spec = client.get("/pipelines/templates/build/as-spec").json()
    resp = client.post(
        "/pipelines/run",
        json={
            "prompt": "Build my project",
            "workspace_path": "/tmp/workspace",
            "stages": spec["stages"],
            "dependencies": spec["dependencies"],
        },
    )
    assert resp.status_code == 200
    pipeline_id = resp.json()["pipeline_id"]

    jobs = setup_test_db.conn.execute(
        "SELECT job_id, agent_type, status FROM jobs WHERE pipeline_id = ?",
        (pipeline_id,),
    ).fetchall()

    assert len(jobs) == 2
    assert jobs[0]["status"] == "pending"
    assert jobs[1]["status"] == "pending"


# --- Running pipelines ---


def test_list_running_empty(client):
    resp = client.get("/pipelines/running")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_running_after_start(client, sample_template):
    spec = client.get("/pipelines/templates/build/as-spec").json()
    run_body = {
        "stages": spec["stages"],
        "dependencies": spec["dependencies"],
        "workspace_path": "/tmp",
    }
    client.post("/pipelines/run", json={**run_body, "prompt": "Build 1"})
    client.post("/pipelines/run", json={**run_body, "prompt": "Build 2"})

    resp = client.get("/pipelines/running")
    assert resp.status_code == 200
    pipelines = resp.json()
    assert len(pipelines) == 2


# --- Get pipeline ---


def test_get_pipeline(client, sample_template):
    spec = client.get("/pipelines/templates/build/as-spec").json()
    start_resp = client.post(
        "/pipelines/run",
        json={
            "prompt": "Build my project",
            "workspace_path": "/tmp",
            "stages": spec["stages"],
            "dependencies": spec["dependencies"],
        },
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
    spec = client.get("/pipelines/templates/build/as-spec").json()
    start_resp = client.post(
        "/pipelines/run",
        json={
            "prompt": "Build my project",
            "workspace_path": "/tmp",
            "stages": spec["stages"],
            "dependencies": spec["dependencies"],
        },
    )
    pipeline_id = start_resp.json()["pipeline_id"]

    resp = client.post(f"/pipelines/{pipeline_id}/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["pipeline_id"] == pipeline_id


def test_stop_pipeline_updates_database(client, sample_template, setup_test_db):
    """Stopping a pipeline should update its status in the database."""
    spec = client.get("/pipelines/templates/build/as-spec").json()
    start_resp = client.post(
        "/pipelines/run",
        json={
            "prompt": "Build my project",
            "workspace_path": "/tmp",
            "stages": spec["stages"],
            "dependencies": spec["dependencies"],
        },
    )
    pipeline_id = start_resp.json()["pipeline_id"]

    client.post(f"/pipelines/{pipeline_id}/stop")

    # Check database
    row = setup_test_db.conn.execute(
        """
        SELECT status FROM pipelines WHERE pipeline_id = ?
    """,
        (pipeline_id,),
    ).fetchone()

    assert row["status"] == "cancelled"


# ── Schedule endpoints ─────────────────────────────────────────────────────────


@pytest.fixture
def schedule_template(setup_test_db):
    """Insert a minimal template for schedule tests."""
    db = setup_test_db
    ts = db._timestamp()
    db.conn.execute(
        "INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at) VALUES ('sched-tpl', 'Sched Tpl', 'desc', ?, ?)",
        (ts, ts),
    )
    db.conn.execute(
        "INSERT INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES ('sched-ts-1', 'sched-tpl', 'work', 1)"
    )
    db.conn.execute(
        "INSERT INTO template_jobs (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds) VALUES ('sched-tj-1', 'sched-ts-1', 'dev', 'worker', '{{original_prompt}}', 5, 60)"
    )
    db.conn.commit()


def test_list_schedules_empty(client):
    resp = client.get("/schedules")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_schedule_success(client, schedule_template):
    resp = client.post(
        "/schedules",
        json={
            "template_id": "sched-tpl",
            "name": "Nightly",
            "cron_expr": "0 2 * * *",
            "prompt": "do work",
            "workspace_path": "/workspace",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Nightly"
    assert data["cron_expr"] == "0 2 * * *"
    assert "schedule_id" in data


def test_create_schedule_invalid_cron(client, schedule_template):
    resp = client.post(
        "/schedules",
        json={
            "template_id": "sched-tpl",
            "name": "Bad",
            "cron_expr": "not a cron",
            "prompt": "work",
        },
    )
    assert resp.status_code == 422


def test_create_schedule_unknown_template(client):
    resp = client.post(
        "/schedules",
        json={
            "template_id": "no-such-template",
            "name": "X",
            "cron_expr": "* * * * *",
            "prompt": "work",
        },
    )
    assert resp.status_code == 404


def test_get_schedule(client, schedule_template):
    create_resp = client.post(
        "/schedules",
        json={
            "template_id": "sched-tpl",
            "name": "S",
            "cron_expr": "* * * * *",
            "prompt": "p",
        },
    )
    schedule_id = create_resp.json()["schedule_id"]

    resp = client.get(f"/schedules/{schedule_id}")
    assert resp.status_code == 200
    assert resp.json()["schedule_id"] == schedule_id


def test_get_schedule_not_found(client):
    resp = client.get("/schedules/nonexistent")
    assert resp.status_code == 404


def test_list_schedules_after_create(client, schedule_template):
    client.post(
        "/schedules",
        json={
            "template_id": "sched-tpl",
            "name": "A",
            "cron_expr": "0 0 * * *",
            "prompt": "a",
        },
    )
    client.post(
        "/schedules",
        json={
            "template_id": "sched-tpl",
            "name": "B",
            "cron_expr": "0 1 * * *",
            "prompt": "b",
        },
    )
    resp = client.get("/schedules")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_patch_schedule(client, schedule_template):
    create_resp = client.post(
        "/schedules",
        json={
            "template_id": "sched-tpl",
            "name": "Old",
            "cron_expr": "0 0 * * *",
            "prompt": "p",
        },
    )
    schedule_id = create_resp.json()["schedule_id"]

    resp = client.patch(f"/schedules/{schedule_id}", json={"name": "New"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "New"


def test_patch_schedule_not_found(client):
    resp = client.patch("/schedules/nonexistent", json={"name": "X"})
    assert resp.status_code == 404


def test_delete_schedule(client, schedule_template):
    create_resp = client.post(
        "/schedules",
        json={
            "template_id": "sched-tpl",
            "name": "Del",
            "cron_expr": "* * * * *",
            "prompt": "p",
        },
    )
    schedule_id = create_resp.json()["schedule_id"]

    resp = client.delete(f"/schedules/{schedule_id}")
    assert resp.status_code == 204

    get_resp = client.get(f"/schedules/{schedule_id}")
    assert get_resp.status_code == 404


def test_enable_disable_schedule(client, schedule_template):
    create_resp = client.post(
        "/schedules",
        json={
            "template_id": "sched-tpl",
            "name": "E",
            "cron_expr": "* * * * *",
            "prompt": "p",
        },
    )
    schedule_id = create_resp.json()["schedule_id"]

    resp = client.post(f"/schedules/{schedule_id}/disable")
    assert resp.status_code == 200
    assert resp.json()["enabled"] == 0

    resp = client.post(f"/schedules/{schedule_id}/enable")
    assert resp.status_code == 200
    assert resp.json()["enabled"] == 1


def test_get_schedule_pipelines_empty(client, schedule_template):
    create_resp = client.post(
        "/schedules",
        json={
            "template_id": "sched-tpl",
            "name": "P",
            "cron_expr": "* * * * *",
            "prompt": "p",
        },
    )
    schedule_id = create_resp.json()["schedule_id"]

    resp = client.get(f"/schedules/{schedule_id}/pipelines")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_schedule_pipelines_not_found(client):
    resp = client.get("/schedules/nonexistent/pipelines")
    assert resp.status_code == 404


# ── Template CRUD endpoints ────────────────────────────────────────────────────

TEMPLATE_SPEC = {
    "name": "API Test Template",
    "description": "Created via API",
    "category": "testing",
    "stages": [
        {
            "name": "work",
            "stage_order": 1,
            "jobs": [
                {
                    "ref": "j1",
                    "agent_type": "dev",
                    "name": "worker",
                    "prompt_template": "{{original_prompt}}",
                    "max_iterations": 5,
                    "timeout_seconds": 60,
                }
            ],
        }
    ],
    "dependencies": [],
}


def test_create_template_success(client):
    resp = client.post("/pipelines/templates", json=TEMPLATE_SPEC)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "API Test Template"
    assert data["category"] == "testing"
    assert len(data["stages"]) == 1


def test_create_template_missing_name_returns_422(client):
    resp = client.post("/pipelines/templates", json={"description": "no name"})
    assert resp.status_code == 422


def test_create_template_appears_in_list(client):
    client.post("/pipelines/templates", json=TEMPLATE_SPEC)
    resp = client.get("/pipelines/templates")
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()]
    assert "API Test Template" in names


def test_patch_template_metadata_name(client):
    create_resp = client.post("/pipelines/templates", json=TEMPLATE_SPEC)
    tid = create_resp.json()["template_id"]

    resp = client.patch(f"/pipelines/templates/{tid}", json={"name": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"


def test_patch_template_metadata_not_found(client):
    resp = client.patch("/pipelines/templates/nonexistent", json={"name": "x"})
    assert resp.status_code == 404


def test_patch_template_structure(client):
    create_resp = client.post("/pipelines/templates", json=TEMPLATE_SPEC)
    tid = create_resp.json()["template_id"]

    new_structure = {
        "stages": [
            {
                "name": "new-stage",
                "stage_order": 1,
                "jobs": [
                    {
                        "ref": "j2",
                        "agent_type": "tester",
                        "name": "t",
                        "prompt_template": "run",
                        "max_iterations": 3,
                        "timeout_seconds": 30,
                    }
                ],
            }
        ],
        "dependencies": [],
    }
    resp = client.patch(f"/pipelines/templates/{tid}/structure", json=new_structure)
    assert resp.status_code == 200
    data = resp.json()
    assert data["stages"][0]["name"] == "new-stage"


def test_patch_template_structure_not_found(client):
    resp = client.patch(
        "/pipelines/templates/nonexistent/structure",
        json={"stages": [], "dependencies": []},
    )
    assert resp.status_code == 404


def test_delete_template_success(client):
    create_resp = client.post("/pipelines/templates", json=TEMPLATE_SPEC)
    tid = create_resp.json()["template_id"]

    resp = client.delete(f"/pipelines/templates/{tid}")
    assert resp.status_code == 204

    get_resp = client.get(f"/pipelines/templates/{tid}")
    assert get_resp.status_code == 404


def test_delete_template_not_found(client):
    resp = client.delete("/pipelines/templates/nonexistent")
    assert resp.status_code == 404


def test_delete_template_with_schedule_returns_409(client, setup_test_db):
    create_resp = client.post("/pipelines/templates", json=TEMPLATE_SPEC)
    tid = create_resp.json()["template_id"]

    ts = setup_test_db._timestamp()
    setup_test_db.conn.execute(
        """INSERT INTO pipeline_schedules
           (schedule_id, template_id, name, cron_expr, prompt, enabled, created_at, updated_at)
           VALUES ('sched-x', ?, 'blocker', '* * * * *', 'p', 1, ?, ?)""",
        (tid, ts, ts),
    )
    setup_test_db.conn.commit()

    resp = client.delete(f"/pipelines/templates/{tid}")
    assert resp.status_code == 409


# ── Pipeline delete ─────────────────────────────────────────────────────────────


def _run_from_template(client, template_id, prompt="x", workspace="/tmp"):
    """Helper: GET as-spec then POST /run to create a pipeline from a template."""
    spec = client.get(f"/pipelines/templates/{template_id}/as-spec").json()
    resp = client.post(
        "/pipelines/run",
        json={
            "prompt": prompt,
            "workspace_path": workspace,
            "stages": spec["stages"],
            "dependencies": spec["dependencies"],
        },
    )
    return resp


def test_delete_pipeline_returns_204(client, sample_template):
    pid = _run_from_template(client, "build").json()["pipeline_id"]
    resp = client.delete(f"/pipelines/{pid}")
    assert resp.status_code == 204


def test_delete_pipeline_removes_from_db(client, sample_template, setup_test_db):
    pid = _run_from_template(client, "build").json()["pipeline_id"]
    client.delete(f"/pipelines/{pid}")
    row = setup_test_db.conn.execute(
        "SELECT * FROM pipelines WHERE pipeline_id=?", (pid,)
    ).fetchone()
    assert row is None


def test_delete_pipeline_cascades_stages_and_jobs(
    client, sample_template, setup_test_db
):
    pid = _run_from_template(client, "build").json()["pipeline_id"]
    # Confirm stages and jobs exist
    assert (
        setup_test_db.conn.execute(
            "SELECT COUNT(*) FROM stages WHERE pipeline_id=?", (pid,)
        ).fetchone()[0]
        > 0
    )
    assert (
        setup_test_db.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE pipeline_id=?", (pid,)
        ).fetchone()[0]
        > 0
    )

    client.delete(f"/pipelines/{pid}")

    assert (
        setup_test_db.conn.execute(
            "SELECT COUNT(*) FROM stages WHERE pipeline_id=?", (pid,)
        ).fetchone()[0]
        == 0
    )
    assert (
        setup_test_db.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE pipeline_id=?", (pid,)
        ).fetchone()[0]
        == 0
    )


def test_delete_pipeline_not_found_returns_404(client):
    resp = client.delete("/pipelines/nonexistent-pipeline")
    assert resp.status_code == 404
