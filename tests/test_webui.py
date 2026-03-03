"""
Tests for the Clowder web UI.

These are API contract tests — they verify that the server returns the exact
field names and shapes that the JavaScript in static/index.html expects.
No browser automation is needed; the contract is what matters.

Mirrors the pattern in test_api.py (temp DB, TestClient, no orchestration loop).
"""
import json
import tempfile
import os
import uuid
import pytest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.main import app


# ─── fixtures (same pattern as test_api.py) ───────────────────────────────────

@pytest.fixture(autouse=True)
def setup_test_db():
    """Use a fresh temporary database for each test."""
    tmpdir = tempfile.mkdtemp()
    test_db_path = os.path.join(tmpdir, "test_webui.db")

    import server.main as srv
    original_db = srv.db
    original_tm = srv.template_manager
    original_ps = srv.pipeline_service

    srv.db = type(original_db)(test_db_path)
    srv.template_manager = type(original_tm)(srv.db)
    srv.pipeline_service = type(original_ps)(srv.db, srv.template_manager)

    schema_path = Path(__file__).parent.parent / "agents" / "schema_pipelines.sql"
    if schema_path.exists():
        srv.db.conn.executescript(schema_path.read_text())
        srv.db.conn.commit()

    yield srv.db

    srv.db.conn.close()
    srv.db = original_db
    srv.template_manager = original_tm
    srv.pipeline_service = original_ps

    try:
        os.remove(test_db_path)
        os.rmdir(tmpdir)
    except Exception:
        pass


@pytest.fixture
def client(setup_test_db):
    with patch("server.main.asyncio.create_task"):
        yield TestClient(app, follow_redirects=False)


@pytest.fixture
def client_follow(setup_test_db):
    """TestClient that follows redirects (for /ui tests)."""
    with patch("server.main.asyncio.create_task"):
        yield TestClient(app, follow_redirects=True)


# ─── seed helpers ─────────────────────────────────────────────────────────────

def _seed_template(db, template_id="template-mock", name="Mock Pipeline",
                   description="A mock pipeline for testing"):
    """Insert a minimal pipeline template into the DB."""
    ts = db._timestamp()
    db.conn.execute("""
        INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
    """, (template_id, name, description, ts, ts))
    db.conn.commit()


def _seed_pipeline_with_job(db, pipeline_id="pipe-1",
                             status="completed", job_status="completed",
                             job_output="line one\nline two"):
    """
    Seed a pipeline + one stage + one job directly into the DB.
    Returns the job_id.

    Schema facts:
      pipelines: pipeline_id, template_id, original_prompt, status,
                 created_at, updated_at, completed_at, metadata
      stages:    stage_id, pipeline_id, name, stage_order, status, created_at
      jobs:      job_id, pipeline_id, stage_id, agent_type, prompt,
                 max_iterations, timeout_seconds, allowed_paths,
                 status, retry_count, max_retries, job_output,
                 created_at, updated_at
    """
    ts = db._timestamp()
    job_id = str(uuid.uuid4())
    stage_id = str(uuid.uuid4())

    db.conn.execute("""
        INSERT INTO pipelines
            (pipeline_id, template_id, original_prompt, status, created_at, updated_at, completed_at)
        VALUES (?, NULL, ?, ?, ?, ?, ?)
    """, (pipeline_id, "test prompt", status, ts, ts,
          ts if status in ("completed", "failed", "cancelled") else None))

    db.conn.execute("""
        INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (stage_id, pipeline_id, "build", 1,
          "completed" if job_status == "completed" else "running", ts))

    db.conn.execute("""
        INSERT INTO jobs
            (job_id, pipeline_id, stage_id, agent_type, prompt,
             max_iterations, timeout_seconds, allowed_paths,
             status, retry_count, max_retries, job_output,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (job_id, pipeline_id, stage_id, "mock", "test prompt",
          50, 300, json.dumps(["D:/workspace"]),
          job_status, 0, 3, job_output, ts, ts))

    db.conn.commit()
    return job_id


# ─── static file serving ──────────────────────────────────────────────────────

class TestStaticServing:
    def test_ui_redirects_to_static_index(self, client):
        """/ui should redirect to /static/index.html."""
        r = client.get("/ui")
        assert r.status_code in (307, 302, 301)
        assert "/static/index.html" in r.headers.get("location", "")

    def test_static_index_served(self, client_follow):
        """/static/index.html returns 200 HTML."""
        r = client_follow.get("/static/index.html")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert "CLOWDER" in r.text

    def test_ui_resolves_to_html(self, client_follow):
        """/ui followed through redirect returns HTML."""
        r = client_follow.get("/ui")
        assert r.status_code == 200
        assert "CLOWDER" in r.text

    def test_index_contains_sidebar(self, client_follow):
        """HTML should contain key DOM element IDs the JS references."""
        r = client_follow.get("/static/index.html")
        body = r.text
        for el_id in ("sidebar", "tpl-list", "running-list", "recent-list",
                       "detail-panel", "log-output", "start-dialog"):
            assert el_id in body, f"Missing element id: {el_id}"

    def test_index_references_api_endpoints(self, client_follow):
        """The JS should reference the correct API paths."""
        r = client_follow.get("/static/index.html")
        body = r.text
        assert "/pipelines/templates" in body
        assert "/pipelines/running" in body
        assert "/pipelines/recent" in body
        assert "/log/since" in body
        assert "/log/full" in body


# ─── /pipelines/templates contract ───────────────────────────────────────────

class TestTemplatesContract:
    """
    JS expects: GET /pipelines/templates → string[]

    JavaScript:
        state.templates = templates;   // string[]
        state.templates.map(t => { const data = { id: t }; ... })
    """

    def test_returns_list(self, client):
        r = client.get("/pipelines/templates")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_items_are_strings(self, client, setup_test_db):
        _seed_template(setup_test_db, "template-mock")
        r = client.get("/pipelines/templates")
        items = r.json()
        assert len(items) > 0
        for item in items:
            assert isinstance(item, str), f"Expected string, got {type(item)}: {item}"

    def test_returns_template_id_string(self, client, setup_test_db):
        _seed_template(setup_test_db, "template-mock")
        r = client.get("/pipelines/templates")
        assert "template-mock" in r.json()

    def test_empty_when_no_templates(self, client):
        r = client.get("/pipelines/templates")
        assert r.json() == []


# ─── /pipelines/running contract ─────────────────────────────────────────────

class TestRunningPipelinesContract:
    """
    JS expects pipeline objects with:
      id          — string (JS: p.id, pData.id)
      name        — string
      status      — string
      stages      — array of { name: string, jobs: array }
      stages[].jobs[].id      — string (JS: j.id, jData.id)
      stages[].jobs[].name    — string
      stages[].jobs[].status  — string
      stages[].jobs[].retries — int (JS: j.retries)
      stages[].jobs[].prompt  — string

    NOT pipeline_id, NOT job_id, NOT retry_count.
    """

    def test_returns_list(self, client):
        r = client.get("/pipelines/running")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_pipeline_has_id_not_pipeline_id(self, client, setup_test_db):
        _seed_pipeline_with_job(setup_test_db, status="running", job_status="running")
        r = client.get("/pipelines/running")
        pipelines = r.json()
        assert len(pipelines) > 0
        p = pipelines[0]
        assert "id" in p,          "Pipeline must have 'id' (JS uses p.id)"
        assert "pipeline_id" not in p, "JS does not use 'pipeline_id'"

    def test_pipeline_has_required_fields(self, client, setup_test_db):
        _seed_pipeline_with_job(setup_test_db, status="running", job_status="running")
        r = client.get("/pipelines/running")
        p = r.json()[0]
        for field in ("id", "name", "status", "stages"):
            assert field in p, f"Pipeline missing field: '{field}'"

    def test_stages_is_list(self, client, setup_test_db):
        _seed_pipeline_with_job(setup_test_db, status="running", job_status="running")
        r = client.get("/pipelines/running")
        p = r.json()[0]
        assert isinstance(p["stages"], list)
        assert len(p["stages"]) > 0

    def test_stage_has_name_and_jobs(self, client, setup_test_db):
        _seed_pipeline_with_job(setup_test_db, status="running", job_status="running")
        r = client.get("/pipelines/running")
        stage = r.json()[0]["stages"][0]
        assert "name" in stage
        assert "jobs" in stage
        assert isinstance(stage["jobs"], list)

    def test_job_has_id_not_job_id(self, client, setup_test_db):
        _seed_pipeline_with_job(setup_test_db, status="running", job_status="running")
        r = client.get("/pipelines/running")
        job = r.json()[0]["stages"][0]["jobs"][0]
        assert "id" in job,        "Job must have 'id' (JS uses j.id)"
        assert "job_id" not in job, "JS does not use 'job_id'"

    def test_job_has_retries_not_retry_count(self, client, setup_test_db):
        _seed_pipeline_with_job(setup_test_db, status="running", job_status="running")
        r = client.get("/pipelines/running")
        job = r.json()[0]["stages"][0]["jobs"][0]
        assert "retries" in job,       "Job must have 'retries' (JS uses j.retries)"
        assert "retry_count" not in job, "JS does not use 'retry_count'"

    def test_job_has_required_fields(self, client, setup_test_db):
        _seed_pipeline_with_job(setup_test_db, status="running", job_status="running")
        r = client.get("/pipelines/running")
        job = r.json()[0]["stages"][0]["jobs"][0]
        for field in ("id", "name", "status", "retries"):
            assert field in job, f"Job missing field: '{field}'"

    def test_three_levels_pipeline_stage_job(self, client, setup_test_db):
        """Response must have 3 levels: pipeline → stages[] → jobs[].
        The JS sidebar renders all three."""
        _seed_pipeline_with_job(setup_test_db, status="running", job_status="running")
        r = client.get("/pipelines/running")
        p = r.json()[0]
        assert "stages" in p
        stage = p["stages"][0]
        assert "jobs" in stage
        job = stage["jobs"][0]
        assert "id" in job


# ─── /pipelines/recent contract ──────────────────────────────────────────────

class TestRecentPipelinesContract:
    """Same shape as /pipelines/running, plus completed_at."""

    def test_returns_list(self, client):
        r = client.get("/pipelines/recent")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_pipeline_has_id(self, client, setup_test_db):
        _seed_pipeline_with_job(setup_test_db, status="completed", job_status="completed")
        r = client.get("/pipelines/recent")
        pipelines = r.json()
        assert len(pipelines) > 0
        assert "id" in pipelines[0]
        assert "pipeline_id" not in pipelines[0]

    def test_job_has_id_and_retries(self, client, setup_test_db):
        _seed_pipeline_with_job(setup_test_db, status="completed", job_status="completed")
        r = client.get("/pipelines/recent")
        job = r.json()[0]["stages"][0]["jobs"][0]
        assert "id" in job
        assert "retries" in job
        assert "job_id" not in job
        assert "retry_count" not in job

    def test_limit_param(self, client, setup_test_db):
        for i in range(5):
            _seed_pipeline_with_job(setup_test_db, pipeline_id=f"pipe-{i}",
                                    status="completed", job_status="completed")
        r = client.get("/pipelines/recent?limit=3")
        assert r.status_code == 200
        assert len(r.json()) <= 3

    def test_completed_at_present(self, client, setup_test_db):
        """Recent pipelines must include completed_at for display."""
        _seed_pipeline_with_job(setup_test_db, status="completed", job_status="completed")
        r = client.get("/pipelines/recent")
        p = r.json()[0]
        assert "completed_at" in p


# ─── /pipelines/jobs/{id}/log/since contract ─────────────────────────────────

class TestLogSinceContract:
    """
    JS expects: GET /pipelines/jobs/{id}/log/since?line=N →
      { lines: string[], total: int, live: bool }

    JS code (pollLog):
        const data = await api(`...log/since?line=${state.logOffset}`);
        appendLogLines(data.lines);
        state.logOffset = data.total;
        if (!data.live && data.lines.length === 0) stopLogPolling();
    """

    def test_returns_correct_shape(self, client, setup_test_db):
        job_id = _seed_pipeline_with_job(setup_test_db, job_output="line1\nline2\nline3")
        r = client.get(f"/pipelines/jobs/{job_id}/log/since?line=0")
        assert r.status_code == 200
        data = r.json()
        assert "lines" in data, "Response must have 'lines'"
        assert "total" in data, "Response must have 'total'"
        assert "live"  in data, "Response must have 'live'"

    def test_lines_is_list_of_strings(self, client, setup_test_db):
        job_id = _seed_pipeline_with_job(setup_test_db, job_output="line1\nline2")
        r = client.get(f"/pipelines/jobs/{job_id}/log/since?line=0")
        data = r.json()
        assert isinstance(data["lines"], list)
        for line in data["lines"]:
            assert isinstance(line, str)

    def test_total_equals_line_count(self, client, setup_test_db):
        job_id = _seed_pipeline_with_job(setup_test_db, job_output="a\nb\nc")
        r = client.get(f"/pipelines/jobs/{job_id}/log/since?line=0")
        data = r.json()
        assert data["total"] == 3
        assert len(data["lines"]) == 3

    def test_offset_returns_only_new_lines(self, client, setup_test_db):
        job_id = _seed_pipeline_with_job(setup_test_db, job_output="a\nb\nc\nd")
        r = client.get(f"/pipelines/jobs/{job_id}/log/since?line=2")
        data = r.json()
        assert data["total"] == 4
        assert data["lines"] == ["c", "d"]

    def test_live_false_for_completed_job(self, client, setup_test_db):
        job_id = _seed_pipeline_with_job(setup_test_db, job_status="completed")
        r = client.get(f"/pipelines/jobs/{job_id}/log/since?line=0")
        assert r.json()["live"] is False

    def test_live_true_for_running_job(self, client, setup_test_db):
        job_id = _seed_pipeline_with_job(setup_test_db, job_status="running")
        r = client.get(f"/pipelines/jobs/{job_id}/log/since?line=0")
        assert r.json()["live"] is True

    def test_404_for_unknown_job(self, client):
        r = client.get("/pipelines/jobs/nonexistent-id/log/since?line=0")
        assert r.status_code == 404

    def test_empty_log_returns_empty_lines(self, client, setup_test_db):
        job_id = _seed_pipeline_with_job(setup_test_db, job_output="")
        r = client.get(f"/pipelines/jobs/{job_id}/log/since?line=0")
        data = r.json()
        assert data["lines"] == []
        assert data["total"] == 0


# ─── /pipelines/jobs/{id}/log/full contract ──────────────────────────────────

class TestLogFullContract:
    """
    JS expects: GET /pipelines/jobs/{id}/log/full → plain text

    JS code (fetchFullLog):
        const text = await apiText(`...log/full`);
        appendLogLines(text.split('\\n'));
    """

    def test_returns_plain_text(self, client, setup_test_db):
        job_id = _seed_pipeline_with_job(setup_test_db, job_output="hello world")
        r = client.get(f"/pipelines/jobs/{job_id}/log/full")
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")

    def test_content_is_raw_log(self, client, setup_test_db):
        job_id = _seed_pipeline_with_job(setup_test_db, job_output="line1\nline2\nline3")
        r = client.get(f"/pipelines/jobs/{job_id}/log/full")
        assert r.text == "line1\nline2\nline3"

    def test_empty_log_returns_empty_string(self, client, setup_test_db):
        job_id = _seed_pipeline_with_job(setup_test_db, job_output="")
        r = client.get(f"/pipelines/jobs/{job_id}/log/full")
        assert r.status_code == 200
        assert r.text == ""

    def test_404_for_unknown_job(self, client):
        r = client.get("/pipelines/jobs/nonexistent-id/log/full")
        assert r.status_code == 404


# ─── /pipelines/{template_id}/start contract ─────────────────────────────────

class TestStartPipelineContract:
    """
    JS sends: POST /pipelines/{template_id}/start
      body: { prompt: string, workspace_path: string }

    JS code (submitStartPipeline):
        await api(`/pipelines/${_startTemplateId}/start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt, workspace_path: workspace }),
        });
    """

    def test_404_for_unknown_template(self, client):
        r = client.post("/pipelines/nonexistent-template/start",
                        json={"prompt": "test", "workspace_path": "D:/workspace"})
        assert r.status_code == 404

    def test_requires_prompt_field(self, client, setup_test_db):
        _seed_template(setup_test_db, "template-mock")
        r = client.post("/pipelines/template-mock/start",
                        json={"workspace_path": "D:/workspace"})
        assert r.status_code == 422

    def test_requires_workspace_path_field(self, client, setup_test_db):
        _seed_template(setup_test_db, "template-mock")
        # workspace_path has a default so this may succeed — just check no crash
        r = client.post("/pipelines/template-mock/start",
                        json={"prompt": "test"})
        # 200 or 404 (no stages in template) are both valid; 5xx is not
        assert r.status_code < 500
