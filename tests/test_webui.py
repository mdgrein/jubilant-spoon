"""
Tests for the Clowder web UI.

These are API contract tests — they verify that the server returns the exact
field names and shapes that the JavaScript in static/index.html expects.
No browser automation is needed; the contract is what matters.

Mirrors the pattern in test_api.py (temp DB, TestClient, no orchestration loop).
"""

import json
import uuid
import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.main import app


# ─── fixtures (same pattern as test_api.py) ───────────────────────────────────


@pytest.fixture(autouse=True)
def setup_test_db():
    """Use a fresh in-memory database for each test."""
    import server.main as srv

    original_db = srv.db
    original_tm = srv.template_manager
    original_ps = srv.pipeline_service

    srv.db = type(original_db)(":memory:")
    srv.db.init_pipeline_schema()
    srv.template_manager = type(original_tm)(srv.db)
    srv.pipeline_service = type(original_ps)(srv.db, srv.template_manager)

    yield srv.db

    srv.db.conn.close()
    srv.db = original_db
    srv.template_manager = original_tm
    srv.pipeline_service = original_ps


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


def _seed_template(
    db,
    template_id="template-mock",
    name="Mock Pipeline",
    description="A mock pipeline for testing",
):
    """Insert a minimal pipeline template into the DB."""
    ts = db._timestamp()
    db.conn.execute(
        """
        INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
    """,
        (template_id, name, description, ts, ts),
    )
    db.conn.commit()


def _seed_pipeline_with_job(
    db,
    pipeline_id="pipe-1",
    status="completed",
    job_status="completed",
    job_output="line one\nline two",
):
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

    db.conn.execute(
        """
        INSERT INTO pipelines
            (pipeline_id, template_id, original_prompt, status, created_at, updated_at, completed_at)
        VALUES (?, NULL, ?, ?, ?, ?, ?)
    """,
        (
            pipeline_id,
            "test prompt",
            status,
            ts,
            ts,
            ts if status in ("completed", "failed", "cancelled") else None,
        ),
    )

    db.conn.execute(
        """
        INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (
            stage_id,
            pipeline_id,
            "build",
            1,
            "completed" if job_status == "completed" else "running",
            ts,
        ),
    )

    db.conn.execute(
        """
        INSERT INTO jobs
            (job_id, pipeline_id, stage_id, agent_type, prompt,
             max_iterations, timeout_seconds, allowed_paths,
             status, retry_count, max_retries, job_output,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            job_id,
            pipeline_id,
            stage_id,
            "mock",
            "test prompt",
            50,
            300,
            json.dumps(["D:/workspace"]),
            job_status,
            0,
            3,
            job_output,
            ts,
            ts,
        ),
    )

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
        for el_id in (
            "sidebar",
            "tpl-list",
            "running-list",
            "recent-list",
            "detail-panel",
            "log-output",
            "start-dialog",
        ):
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
    JS expects: GET /pipelines/templates → { template_id, name, description, category }[]

    JavaScript:
        state.templates = templates;   // list of dicts
        state.templates.map(t => { const data = { id: t.template_id, name: t.name }; ... })
    """

    def test_returns_list(self, client):
        r = client.get("/pipelines/templates")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_items_are_dicts_with_required_keys(self, client, setup_test_db):
        _seed_template(setup_test_db, "template-mock")
        r = client.get("/pipelines/templates")
        items = r.json()
        assert len(items) > 0
        for item in items:
            assert isinstance(item, dict), f"Expected dict, got {type(item)}: {item}"
            assert "template_id" in item
            assert "name" in item
            assert "category" in item

    def test_returns_template_id_in_dict(self, client, setup_test_db):
        _seed_template(setup_test_db, "template-mock")
        r = client.get("/pipelines/templates")
        ids = [t["template_id"] for t in r.json()]
        assert "template-mock" in ids

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
        assert "id" in p, "Pipeline must have 'id' (JS uses p.id)"
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
        assert "id" in job, "Job must have 'id' (JS uses j.id)"
        assert "job_id" not in job, "JS does not use 'job_id'"

    def test_job_has_retries_not_retry_count(self, client, setup_test_db):
        _seed_pipeline_with_job(setup_test_db, status="running", job_status="running")
        r = client.get("/pipelines/running")
        job = r.json()[0]["stages"][0]["jobs"][0]
        assert "retries" in job, "Job must have 'retries' (JS uses j.retries)"
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
        _seed_pipeline_with_job(
            setup_test_db, status="completed", job_status="completed"
        )
        r = client.get("/pipelines/recent")
        pipelines = r.json()
        assert len(pipelines) > 0
        assert "id" in pipelines[0]
        assert "pipeline_id" not in pipelines[0]

    def test_job_has_id_and_retries(self, client, setup_test_db):
        _seed_pipeline_with_job(
            setup_test_db, status="completed", job_status="completed"
        )
        r = client.get("/pipelines/recent")
        job = r.json()[0]["stages"][0]["jobs"][0]
        assert "id" in job
        assert "retries" in job
        assert "job_id" not in job
        assert "retry_count" not in job

    def test_limit_param(self, client, setup_test_db):
        for i in range(5):
            _seed_pipeline_with_job(
                setup_test_db,
                pipeline_id=f"pipe-{i}",
                status="completed",
                job_status="completed",
            )
        r = client.get("/pipelines/recent?limit=3")
        assert r.status_code == 200
        assert len(r.json()) <= 3

    def test_completed_at_present(self, client, setup_test_db):
        """Recent pipelines must include completed_at for display."""
        _seed_pipeline_with_job(
            setup_test_db, status="completed", job_status="completed"
        )
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
        job_id = _seed_pipeline_with_job(
            setup_test_db, job_output="line1\nline2\nline3"
        )
        r = client.get(f"/pipelines/jobs/{job_id}/log/since?line=0")
        assert r.status_code == 200
        data = r.json()
        assert "lines" in data, "Response must have 'lines'"
        assert "total" in data, "Response must have 'total'"
        assert "live" in data, "Response must have 'live'"

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
        job_id = _seed_pipeline_with_job(
            setup_test_db, job_output="line1\nline2\nline3"
        )
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


# ─── /pipelines/templates/{id}/as-spec contract ──────────────────────────────


class TestAsSpecContract:
    """
    JS calls GET /pipelines/templates/{id}/as-spec to expand a template before
    posting to /pipelines/run.

    JS code (submitStartPipeline):
        const spec = await api(`/pipelines/templates/${_startTemplateId}/as-spec`);
        await api('/pipelines/run', { ..., stages: spec.stages, dependencies: spec.dependencies });
    """

    def _seed_named_template(self, db):
        ts = db._timestamp()
        tid = "tpl-as-spec"
        sid = "stage-as-spec-1"
        jid = "job-as-spec-1"
        db.conn.execute(
            "INSERT INTO pipeline_templates (template_id, name, description, default_vendor, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (tid, "As-Spec Test", "", "anthropic", ts, ts),
        )
        db.conn.execute(
            "INSERT INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES (?, ?, ?, ?)",
            (sid, tid, "build", 1),
        )
        db.conn.execute(
            """INSERT INTO template_jobs
               (template_job_id, template_stage_id, agent_type, name, chain_id,
                prompt_template, max_iterations, timeout_seconds, vendor)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (jid, sid, "dev", "builder", "build-chain", "{{original_prompt}}", 15, 300),
        )
        db.conn.commit()
        return tid, jid

    @pytest.mark.webui_contract
    def test_returns_200_with_stages_and_dependencies(self, client, setup_test_db):
        tid, _ = self._seed_named_template(setup_test_db)
        r = client.get(f"/pipelines/templates/{tid}/as-spec")
        assert r.status_code == 200
        data = r.json()
        assert "stages" in data
        assert "dependencies" in data

    @pytest.mark.webui_contract
    def test_404_for_unknown_template(self, client):
        r = client.get("/pipelines/templates/nonexistent/as-spec")
        assert r.status_code == 404

    @pytest.mark.webui_contract
    def test_job_has_required_spec_fields(self, client, setup_test_db):
        tid, jid = self._seed_named_template(setup_test_db)
        r = client.get(f"/pipelines/templates/{tid}/as-spec")
        job = r.json()["stages"][0]["jobs"][0]
        for field in (
            "ref",
            "agent_type",
            "name",
            "chain_id",
            "vendor",
            "prompt_template",
        ):
            assert field in job, f"Job spec missing field: '{field}'"

    @pytest.mark.webui_contract
    def test_job_ref_is_template_job_id(self, client, setup_test_db):
        tid, jid = self._seed_named_template(setup_test_db)
        r = client.get(f"/pipelines/templates/{tid}/as-spec")
        job = r.json()["stages"][0]["jobs"][0]
        assert job["ref"] == jid

    @pytest.mark.webui_contract
    def test_template_default_vendor_applied_to_null_job_vendor(
        self, client, setup_test_db
    ):
        """Jobs with no explicit vendor inherit the template's default_vendor."""
        tid, _ = self._seed_named_template(setup_test_db)
        r = client.get(f"/pipelines/templates/{tid}/as-spec")
        job = r.json()["stages"][0]["jobs"][0]
        assert job["vendor"] == "anthropic"

    @pytest.mark.webui_contract
    def test_as_spec_then_run_creates_pipeline(self, client, setup_test_db):
        """Full two-step flow: GET as-spec → POST /pipelines/run → pipeline exists."""
        tid, _ = self._seed_named_template(setup_test_db)
        spec = client.get(f"/pipelines/templates/{tid}/as-spec").json()
        r = client.post(
            "/pipelines/run",
            json={
                "prompt": "do the thing",
                "workspace_path": "D:/workspace",
                "stages": spec["stages"],
                "dependencies": spec["dependencies"],
            },
        )
        assert r.status_code == 200
        assert "pipeline_id" in r.json()


# ─── /pipelines/run contract ─────────────────────────────────────────────────


class TestRunPipelineContract:
    """
    CONTRACT: static/index.html submitBuildPipeline() sends POST /pipelines/run
    with name and chain_id per job. The server must accept and preserve them.

    If you change these tests, you MUST update BOTH sides (JS and server) or
    the UI will break.
    """

    _spec = {
        "prompt": "build fibonacci",
        "workspace_path": "D:/workspace",
        "stages": [
            {
                "name": "build",
                "stage_order": 1,
                "jobs": [
                    {
                        "ref": "fib-dev",
                        "agent_type": "dev",
                        "name": "fibonacci tester",
                        "chain_id": "fibonacci",
                        "vendor": "local-ollama",
                        "prompt_template": "{{original_prompt}}",
                    }
                ],
            }
        ],
        "dependencies": [],
    }

    @pytest.mark.webui_contract
    def test_job_name_and_chain_id_accepted(self, client):
        """CONTRACT: submitBuildPipeline() sends `name` and `chain_id` per job
        → server must accept them in JobSpec."""
        r = client.post("/pipelines/run", json=self._spec)
        assert r.status_code == 200
        assert "pipeline_id" in r.json()

    @pytest.mark.webui_contract
    def test_job_name_preserved_in_running_response(self, client):
        """CONTRACT: name/chain_id sent via Build Pipeline must appear in
        GET /pipelines/running so renderJobsForStage can group them."""
        r = client.post("/pipelines/run", json=self._spec)
        assert r.status_code == 200

        r2 = client.get("/pipelines/running")
        pipelines = r2.json()
        assert len(pipelines) > 0
        job = pipelines[0]["stages"][0]["jobs"][0]
        assert job["name"] == "fibonacci tester"
        assert job["chain_id"] == "fibonacci"

    @pytest.mark.webui_contract
    def test_both_start_and_build_produce_name_in_jobs(self, client, setup_test_db):
        """CONTRACT: Both the as-spec+run path and inline spec POST /pipelines/run
        must produce jobs where name != agent_type (not falling back).
        This is the convergence test — both paths go through /pipelines/run."""
        # Seed a template with a named job
        ts = setup_test_db._timestamp()
        tid = "tpl-convergence"
        sid = "stage-conv-1"
        jid = "job-conv-1"
        setup_test_db.conn.execute(
            "INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (tid, "Convergence Test", "", ts, ts),
        )
        setup_test_db.conn.execute(
            "INSERT INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES (?, ?, ?, ?)",
            (sid, tid, "build", 1),
        )
        setup_test_db.conn.execute(
            """INSERT INTO template_jobs
               (template_job_id, template_stage_id, agent_type, name, chain_id,
                vendor, prompt_template, max_iterations, timeout_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                jid,
                sid,
                "dev",
                "fibonacci tester",
                "fibonacci",
                "local-ollama",
                "{{original_prompt}}",
                15,
                300,
            ),
        )
        setup_test_db.conn.commit()

        # Start via template (as-spec + run — the new Start Pipeline path)
        spec = client.get(f"/pipelines/templates/{tid}/as-spec").json()
        r1 = client.post(
            "/pipelines/run",
            json={
                "prompt": "build fib",
                "workspace_path": "D:/workspace",
                "stages": spec["stages"],
                "dependencies": spec["dependencies"],
            },
        )
        assert r1.status_code == 200
        pid1 = r1.json()["pipeline_id"]

        # Start via inline spec (Build Pipeline path)
        r2 = client.post("/pipelines/run", json=self._spec)
        assert r2.status_code == 200
        pid2 = r2.json()["pipeline_id"]

        running = client.get("/pipelines/running").json()
        by_id = {p["id"]: p for p in running}

        for pid in (pid1, pid2):
            assert pid in by_id, f"Pipeline {pid} not in running"
            job = by_id[pid]["stages"][0]["jobs"][0]
            assert job["name"] == "fibonacci tester", (
                f"Pipeline {pid}: expected name 'fibonacci tester', got '{job['name']}'"
            )

    @pytest.mark.webui_contract
    def test_running_jobs_have_chain_id_and_depends_on(self, client, setup_test_db):
        """CONTRACT: GET /pipelines/running job objects must include chain_id
        and depends_on so the JS can group and render dependencies."""
        r = client.post("/pipelines/run", json=self._spec)
        assert r.status_code == 200
        running = client.get("/pipelines/running").json()
        job = running[0]["stages"][0]["jobs"][0]
        assert "chain_id" in job, "Job must have 'chain_id'"
        assert "depends_on" in job, "Job must have 'depends_on'"
