"""
Unit tests for server business logic (service layer).
Tests business logic directly without HTTP layer.
"""

import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

from db import ClowderDB
from templates import TemplateManager
from server.services import PipelineService


@pytest.fixture
def test_db():
    """Create an in-memory test database."""
    db = ClowderDB(":memory:")
    db.init_pipeline_schema()
    yield db
    db.conn.close()


@pytest.fixture
def service(test_db):
    """Create a PipelineService with test database."""
    template_manager = TemplateManager(test_db)
    return PipelineService(test_db, template_manager)


@pytest.fixture
def sample_template(test_db):
    """Create a sample template in the database."""
    ts = test_db._timestamp()

    # Insert template
    test_db.conn.execute(
        """
        INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at)
        VALUES ('build', 'Build Pipeline', 'Compiles the project', ?, ?)
    """,
        (ts, ts),
    )

    # Insert stage
    test_db.conn.execute("""
        INSERT INTO template_stages (template_stage_id, template_id, name, stage_order)
        VALUES ('build_stage_1', 'build', 'Default Stage', 0)
    """)

    # Insert jobs
    test_db.conn.execute("""
        INSERT INTO template_jobs (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds)
        VALUES
            ('build_job_1', 'build_stage_1', 'dev', 'compiler', 'Compile the code', 10, 600),
            ('build_job_2', 'build_stage_1', 'tester', 'tester', 'Run tests', 5, 300)
    """)

    test_db.conn.commit()


# --- list_templates ---


def test_list_templates_empty(service):
    """Should return empty list when no templates exist."""
    templates = service.list_templates()
    assert templates == []


def test_list_templates(service, sample_template):
    """Should return list of template dicts."""
    templates = service.list_templates()
    assert isinstance(templates, list)
    template_ids = [t["template_id"] for t in templates]
    assert "build" in template_ids


def test_list_templates_has_required_keys(service, sample_template):
    """Each template dict must have template_id, name, description, category."""
    templates = service.list_templates()
    assert len(templates) == 1
    t = templates[0]
    assert "template_id" in t
    assert "name" in t
    assert "description" in t
    assert "category" in t


def test_list_templates_category_is_none_when_unset(service, sample_template):
    """category should be None when not set."""
    templates = service.list_templates()
    assert templates[0]["category"] is None


# --- get_template_details ---


def test_get_template_details_not_found(service):
    """Should return None for nonexistent template."""
    template = service.get_template_details("nonexistent")
    assert template is None


def test_get_template_details(service, sample_template):
    """Should return full template structure."""
    template = service.get_template_details("build")

    assert template is not None
    assert template["id"] == "build"
    assert template["name"] == "Build Pipeline"
    assert template["description"] == "Compiles the project"
    assert len(template["stages"]) == 1
    assert template["stages"][0]["name"] == "Default Stage"
    assert len(template["stages"][0]["jobs"]) == 2


def test_get_template_details_includes_jobs(service, sample_template):
    """Should include job details in template."""
    template = service.get_template_details("build")

    jobs = template["stages"][0]["jobs"]
    job_agents = [j["agent_type"] for j in jobs]
    assert "dev" in job_agents
    assert "tester" in job_agents


def test_get_template_details_includes_chain_id(service, sample_template):
    """Each job dict should include a chain_id key (None when not set)."""
    template = service.get_template_details("build")
    jobs = template["stages"][0]["jobs"]
    for job in jobs:
        assert "chain_id" in job
        assert job["chain_id"] is None  # sample_template has no chain_id


def test_get_template_details_chain_id_value(four_tasks_service):
    """Jobs in the four-tasks template should have their chain_id set."""
    tpl = four_tasks_service.get_template_details("template-four-tasks")
    assert tpl is not None

    all_jobs = [j for stage in tpl["stages"] for j in stage["jobs"]]
    chain_ids = {j["chain_id"] for j in all_jobs}
    assert "fibonacci" in chain_ids
    assert "binary_sort" in chain_ids
    assert "case_converter" in chain_ids
    assert "all_join_in" in chain_ids
    # Every job should have a non-None chain_id
    assert all(j["chain_id"] is not None for j in all_jobs)


# --- stop_pipeline ---


def test_stop_pipeline(service, sample_template, test_db):
    """Should update pipeline status to cancelled."""
    pipeline_id = service.template_manager.instantiate_template(
        "build", "Build my project", "/tmp"
    )

    # Stop it
    stop_result = service.stop_pipeline(pipeline_id)

    assert stop_result["status"] == "cancelled"
    assert stop_result["pipeline_id"] == pipeline_id
    assert "name" in stop_result

    # Verify in database
    row = test_db.conn.execute(
        """
        SELECT status FROM pipelines WHERE pipeline_id = ?
    """,
        (pipeline_id,),
    ).fetchone()

    assert row["status"] == "cancelled"


def test_stop_pipeline_truncates_name(service, sample_template):
    """Should truncate long pipeline names in response."""
    long_prompt = "a" * 100
    pipeline_id = service.template_manager.instantiate_template(
        "build", long_prompt, "/tmp"
    )

    stop_result = service.stop_pipeline(pipeline_id)

    assert len(stop_result["name"]) == 50


# --- get_running_pipelines ---


def test_get_running_pipelines_empty(service):
    """Should return empty list when no running pipelines."""
    pipelines = service.get_running_pipelines()
    assert pipelines == []


def test_get_running_pipelines(service, sample_template):
    """Should return list of active pipelines."""
    service.template_manager.instantiate_template("build", "Build 1", "/tmp")
    service.template_manager.instantiate_template("build", "Build 2", "/tmp")

    pipelines = service.get_running_pipelines()

    assert len(pipelines) == 2
    assert pipelines[0]["status"] in ("pending", "running")
    assert pipelines[1]["status"] in ("pending", "running")


def test_get_running_pipelines_includes_stages(service, sample_template):
    """Should include nested stages and jobs."""
    service.template_manager.instantiate_template("build", "Build 1", "/tmp")

    pipelines = service.get_running_pipelines()

    assert len(pipelines) == 1
    assert "stages" in pipelines[0]
    assert len(pipelines[0]["stages"]) == 1
    assert "jobs" in pipelines[0]["stages"][0]


def test_get_running_pipelines_excludes_cancelled(service, sample_template):
    """Should not include cancelled pipelines."""
    pipeline_id = service.template_manager.instantiate_template(
        "build", "Build 1", "/tmp"
    )

    service.stop_pipeline(pipeline_id)

    pipelines = service.get_running_pipelines()

    assert len(pipelines) == 0


def test_get_running_pipelines_job_name_from_name_column(service, sample_template):
    """Job 'name' field should come from the jobs.name column, not agent_type."""
    service.template_manager.instantiate_template("build", "Build 1", "/tmp")

    pipelines = service.get_running_pipelines()
    jobs = pipelines[0]["stages"][0]["jobs"]

    names = {j["name"] for j in jobs}
    # template has name='compiler' and name='tester' — these should appear, not 'dev'/'tester' agent_type
    assert "compiler" in names


def test_get_running_pipelines_job_name_falls_back_to_agent_type(service, test_db):
    """When jobs.name is empty, fall back to agent_type."""
    ts = test_db._timestamp()
    test_db.conn.execute(
        "INSERT INTO pipelines (pipeline_id, original_prompt, status, created_at, updated_at) "
        "VALUES ('p1', 'test', 'running', ?, ?)",
        (ts, ts),
    )
    test_db.conn.execute(
        "INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at) "
        "VALUES ('s1', 'p1', 'dev', 1, 'running', ?)",
        (ts,),
    )
    test_db.conn.execute(
        "INSERT INTO jobs (job_id, pipeline_id, stage_id, agent_type, name, prompt, "
        "max_iterations, timeout_seconds, allowed_paths, status, created_at, updated_at) "
        "VALUES ('j1', 'p1', 's1', 'dev', '', 'do work', 10, 60, '[]', 'pending', ?, ?)",
        (ts, ts),
    )
    test_db.conn.commit()

    pipelines = service.get_running_pipelines()
    job = pipelines[0]["stages"][0]["jobs"][0]

    assert job["name"] == "dev"  # falls back to agent_type


def test_get_running_pipelines_job_has_chain_id_and_depends_on(service, test_db):
    """Jobs should include chain_id and depends_on fields."""
    ts = test_db._timestamp()
    test_db.conn.execute(
        "INSERT INTO pipelines (pipeline_id, original_prompt, status, created_at, updated_at) "
        "VALUES ('p1', 'test', 'running', ?, ?)",
        (ts, ts),
    )
    test_db.conn.execute(
        "INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at) "
        "VALUES ('s1', 'p1', 'dev', 1, 'running', ?)",
        (ts,),
    )
    test_db.conn.execute(
        "INSERT INTO jobs (job_id, pipeline_id, stage_id, agent_type, name, prompt, "
        "max_iterations, timeout_seconds, allowed_paths, status, chain_id, created_at, updated_at) "
        "VALUES ('j1', 'p1', 's1', 'tester', 'fib tester', 'test', 10, 60, '[]', 'pending', "
        "'fibonacci', ?, ?)",
        (ts, ts),
    )
    test_db.conn.execute(
        "INSERT INTO jobs (job_id, pipeline_id, stage_id, agent_type, name, prompt, "
        "max_iterations, timeout_seconds, allowed_paths, status, chain_id, created_at, updated_at) "
        "VALUES ('j2', 'p1', 's1', 'dev', 'fib dev', 'implement', 10, 60, '[]', 'pending', "
        "'fibonacci', ?, ?)",
        (ts, ts),
    )
    test_db.conn.execute(
        "INSERT INTO job_dependencies (job_id, depends_on_job_id, dependency_type) "
        "VALUES ('j2', 'j1', 'success')"
    )
    test_db.conn.commit()

    pipelines = service.get_running_pipelines()
    jobs = {j["id"]: j for j in pipelines[0]["stages"][0]["jobs"]}

    assert jobs["j1"]["chain_id"] == "fibonacci"
    assert jobs["j1"]["depends_on"] == []
    assert jobs["j2"]["chain_id"] == "fibonacci"
    assert jobs["j2"]["depends_on"] == ["j1"]


# --- get_pipeline ---


def test_get_pipeline_not_found(service):
    """Should return None for nonexistent pipeline."""
    result = service.get_pipeline("00000000-0000-0000-0000-000000000000")
    assert result is None


def test_get_pipeline(service, sample_template):
    """Should return pipeline with jobs."""
    pipeline_id = service.template_manager.instantiate_template(
        "build", "Build my project", "/tmp"
    )

    result = service.get_pipeline(pipeline_id)

    assert result is not None
    assert result["pipeline"]["pipeline_id"] == pipeline_id
    assert len(result["jobs"]) == 2


def test_get_pipeline_includes_stage_info(service, sample_template):
    """Should include stage information in jobs."""
    pipeline_id = service.template_manager.instantiate_template(
        "build", "Build my project", "/tmp"
    )

    result = service.get_pipeline(pipeline_id)

    # Check that jobs have stage info
    for job in result["jobs"]:
        assert "stage_name" in job
        assert "stage_order" in job


# --- four-tasks template ---


@pytest.fixture
def four_tasks_db(test_db):
    """Load the four-tasks seed template into the test database."""
    seed_path = Path(__file__).parent.parent / "pipeline" / "seed_templates.sql"
    seed_sql = seed_path.read_text()
    test_db.conn.executescript(seed_sql)
    test_db.conn.commit()
    return test_db


@pytest.fixture
def four_tasks_service(four_tasks_db):
    template_manager = TemplateManager(four_tasks_db)
    return PipelineService(four_tasks_db, template_manager)


def test_template_default_vendor_applied(test_db):
    """Jobs with NULL vendor inherit template's default_vendor."""
    ts = test_db._timestamp()
    test_db.conn.execute(
        "INSERT INTO pipeline_templates (template_id, name, description, default_vendor, default_model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "tmpl-vendor-test",
            "Vendor Test",
            "",
            "anthropic",
            "claude-haiku-4-5-20251001",
            ts,
            ts,
        ),
    )
    test_db.conn.execute(
        "INSERT INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES (?, ?, ?, ?)",
        ("ts-vt-1", "tmpl-vendor-test", "work", 1),
    )
    # vendor and model are NULL — should inherit from template
    test_db.conn.execute(
        "INSERT INTO template_jobs (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds, vendor, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("tj-vt-1", "ts-vt-1", "dev", "worker", "do stuff", 5, 60, None, None),
    )
    test_db.conn.commit()

    tm = TemplateManager(test_db)
    pipeline_id = tm.instantiate_template("tmpl-vendor-test", "test prompt", "/tmp/ws")

    job = test_db.conn.execute(
        "SELECT vendor, model FROM jobs WHERE pipeline_id = ?", (pipeline_id,)
    ).fetchone()
    assert job["vendor"] == "anthropic"
    assert job["model"] == "claude-haiku-4-5-20251001"


def test_job_level_vendor_overrides_template_default(test_db):
    """Explicit job-level vendor/model takes precedence over template default."""
    ts = test_db._timestamp()
    test_db.conn.execute(
        "INSERT INTO pipeline_templates (template_id, name, description, default_vendor, default_model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "tmpl-override-test",
            "Override Test",
            "",
            "anthropic",
            "claude-opus-4-6",
            ts,
            ts,
        ),
    )
    test_db.conn.execute(
        "INSERT INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES (?, ?, ?, ?)",
        ("ts-ot-1", "tmpl-override-test", "work", 1),
    )
    test_db.conn.execute(
        "INSERT INTO template_jobs (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds, vendor, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "tj-ot-1",
            "ts-ot-1",
            "dev",
            "worker",
            "do stuff",
            5,
            60,
            "local-ollama",
            "qwen3:8b",
        ),
    )
    test_db.conn.commit()

    tm = TemplateManager(test_db)
    pipeline_id = tm.instantiate_template(
        "tmpl-override-test", "test prompt", "/tmp/ws"
    )

    job = test_db.conn.execute(
        "SELECT vendor, model FROM jobs WHERE pipeline_id = ?", (pipeline_id,)
    ).fetchone()
    assert job["vendor"] == "local-ollama"
    assert job["model"] == "qwen3:8b"


def test_vendor_model_propagated(four_tasks_service, four_tasks_db):
    """All 12 four-tasks jobs should have vendor='local-ollama' and model='qwen3:8b' (from template default)."""
    pipeline_id = four_tasks_service.template_manager.instantiate_template(
        "template-four-tasks", "run four tasks", "/tmp/workspace"
    )

    jobs = four_tasks_db.conn.execute(
        "SELECT vendor, model FROM jobs WHERE pipeline_id = ?", (pipeline_id,)
    ).fetchall()

    assert len(jobs) == 12
    for job in jobs:
        assert job["vendor"] == "local-ollama", (
            f"Expected local-ollama, got {job['vendor']}"
        )
        assert job["model"] == "qwen3:8b", f"Expected qwen3:8b, got {job['model']}"


def test_join_tester_has_three_deps(four_tasks_service, four_tasks_db):
    """The Stage 2 tester job (all_join_in) should depend on exactly 3 jobs."""
    pipeline_id = four_tasks_service.template_manager.instantiate_template(
        "template-four-tasks", "run four tasks", "/tmp/workspace"
    )

    # Find the join tester job (template_job_id = tj-4t-join-test)
    join_tester = four_tasks_db.conn.execute(
        "SELECT job_id FROM jobs WHERE pipeline_id = ? AND template_job_id = 'tj-4t-join-test'",
        (pipeline_id,),
    ).fetchone()

    assert join_tester is not None, "Join tester job not found"

    deps = four_tasks_db.conn.execute(
        "SELECT depends_on_job_id FROM job_dependencies WHERE job_id = ?",
        (join_tester["job_id"],),
    ).fetchall()

    assert len(deps) == 3, f"Expected 3 deps, got {len(deps)}"


def test_stage1_jobs_dep_counts(four_tasks_service, four_tasks_db):
    """Stage 1 testers have 0 deps; devs have 1 (success); verifiers have 1 (completed)."""
    pipeline_id = four_tasks_service.template_manager.instantiate_template(
        "template-four-tasks", "run four tasks", "/tmp/workspace"
    )

    tester_ids = ["tj-4t-fib-test", "tj-4t-bsort-test", "tj-4t-case-test"]
    dev_ids = ["tj-4t-fib-dev", "tj-4t-bsort-dev", "tj-4t-case-dev"]
    ver_ids = ["tj-4t-fib-ver", "tj-4t-bsort-ver", "tj-4t-case-ver"]

    def dep_count(template_job_id):
        job = four_tasks_db.conn.execute(
            "SELECT job_id FROM jobs WHERE pipeline_id = ? AND template_job_id = ?",
            (pipeline_id, template_job_id),
        ).fetchone()
        assert job is not None, f"Job {template_job_id} not found"
        return four_tasks_db.conn.execute(
            "SELECT COUNT(*) as c FROM job_dependencies WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()["c"]

    for tid in tester_ids:
        assert dep_count(tid) == 0, f"{tid} should have 0 deps"
    for did in dev_ids:
        assert dep_count(did) == 1, f"{did} should have 1 dep"
    for vid in ver_ids:
        assert dep_count(vid) == 1, f"{vid} should have 1 dep"


# ── Template CRUD ──────────────────────────────────────────────────────────────

MINIMAL_SPEC = {
    "name": "My Template",
    "description": "A test template",
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


def test_create_template_returns_template_dict(service):
    result = service.create_template(MINIMAL_SPEC)
    assert result is not None
    assert result["name"] == "My Template"
    assert result["description"] == "A test template"
    assert result["category"] == "testing"
    assert len(result["stages"]) == 1


def test_create_template_missing_name_raises(service):
    with pytest.raises(ValueError, match="name is required"):
        service.create_template({"description": "no name"})


def test_create_template_appears_in_list(service):
    service.create_template(MINIMAL_SPEC)
    templates = service.list_templates()
    assert any(t["name"] == "My Template" for t in templates)


def test_update_template_metadata_name(service):
    service.create_template(MINIMAL_SPEC)
    templates = service.list_templates()
    tid = templates[0]["template_id"]

    result = service.update_template_metadata(tid, {"name": "Renamed"})
    assert result["name"] == "Renamed"


def test_update_template_metadata_category(service):
    service.create_template(MINIMAL_SPEC)
    templates = service.list_templates()
    tid = templates[0]["template_id"]

    result = service.update_template_metadata(tid, {"category": "development"})
    assert result["category"] == "development"


def test_update_template_metadata_not_found(service):
    result = service.update_template_metadata("nonexistent", {"name": "x"})
    assert result is None


def test_update_template_structure_replaces_stages(service):
    service.create_template(MINIMAL_SPEC)
    templates = service.list_templates()
    tid = templates[0]["template_id"]

    new_stages = [
        {
            "name": "new-stage",
            "stage_order": 1,
            "jobs": [
                {
                    "ref": "j2",
                    "agent_type": "tester",
                    "name": "tester",
                    "prompt_template": "test {{original_prompt}}",
                    "max_iterations": 3,
                    "timeout_seconds": 60,
                }
            ],
        }
    ]
    result = service.update_template_structure(tid, new_stages, [])
    assert len(result["stages"]) == 1
    assert result["stages"][0]["name"] == "new-stage"
    assert result["stages"][0]["jobs"][0]["agent_type"] == "tester"


def test_update_template_structure_not_found(service):
    result = service.update_template_structure("nonexistent", [], [])
    assert result is None


def test_delete_template_removes_it(service):
    service.create_template(MINIMAL_SPEC)
    templates = service.list_templates()
    tid = templates[0]["template_id"]

    found = service.delete_template(tid)
    assert found is True
    assert service.list_templates() == []


def test_delete_template_not_found(service):
    found = service.delete_template("nonexistent")
    assert found is False


def test_delete_template_with_schedule_raises(service, test_db):
    service.create_template(MINIMAL_SPEC)
    templates = service.list_templates()
    tid = templates[0]["template_id"]

    # Insert a schedule referencing this template
    ts = test_db._timestamp()
    test_db.conn.execute(
        """INSERT INTO pipeline_schedules
           (schedule_id, template_id, name, cron_expr, prompt, enabled, created_at, updated_at)
           VALUES ('sched-1', ?, 'test', '* * * * *', 'p', 1, ?, ?)""",
        (tid, ts, ts),
    )
    test_db.conn.commit()

    with pytest.raises(ValueError, match="schedule"):
        service.delete_template(tid)


# ── get_template_details dependency IDs ─────────────────────────────────────


def test_get_template_details_dependencies_use_template_job_ids(four_tasks_service):
    """Dependencies in get_template_details must use template_job_id, not agent_type.

    The build dialog uses these IDs as unique refs. If agent_type is returned
    instead, multiple jobs with the same agent_type (e.g. 4× 'tester') produce
    duplicate deps and a UNIQUE constraint violation on /pipelines/run.
    """
    tpl = four_tasks_service.get_template_details("template-four-tasks")
    assert tpl is not None

    for stage in tpl["stages"]:
        for job in stage["jobs"]:
            for dep in job["dependencies"]:
                dep_id = dep["depends_on"]
                # Must be a real template_job_id, not an agent_type string
                assert dep_id.startswith("tj-"), (
                    f"dependency 'depends_on' should be a template_job_id, got {dep_id!r}"
                )


# ── instantiate_from_spec ────────────────────────────────────────────────────


@pytest.fixture
def from_spec_db(test_db):
    """DB with a two-job template for testing instantiate_from_spec."""
    return test_db


def test_instantiate_from_spec_dependency_direction(from_spec_db):
    """Dependent job must be job_id; prerequisite must be depends_on_job_id."""
    from templates import TemplateManager

    tm = TemplateManager(from_spec_db)
    spec = {
        "stages": [
            {
                "name": "work",
                "stage_order": 0,
                "jobs": [
                    {
                        "ref": "prereq",
                        "agent_type": "tester",
                        "vendor": "local-ollama",
                        "model": "qwen3:8b",
                        "max_iterations": 5,
                        "timeout_seconds": 60,
                    },
                    {
                        "ref": "dependent",
                        "agent_type": "dev",
                        "vendor": "local-ollama",
                        "model": "qwen3:8b",
                        "max_iterations": 5,
                        "timeout_seconds": 60,
                    },
                ],
            }
        ],
        "dependencies": [
            {"from_ref": "prereq", "to_ref": "dependent", "type": "success"}
        ],
    }
    pipeline_id = tm.instantiate_from_spec(spec, "test prompt", "/tmp/ws")

    prereq_job = from_spec_db.conn.execute(
        "SELECT job_id FROM jobs WHERE pipeline_id = ? AND agent_type = 'tester'",
        (pipeline_id,),
    ).fetchone()
    dep_job = from_spec_db.conn.execute(
        "SELECT job_id FROM jobs WHERE pipeline_id = ? AND agent_type = 'dev'",
        (pipeline_id,),
    ).fetchone()

    assert prereq_job and dep_job

    row = from_spec_db.conn.execute(
        "SELECT job_id, depends_on_job_id FROM job_dependencies WHERE job_id = ?",
        (dep_job["job_id"],),
    ).fetchone()

    assert row is not None, "No dependency row found for the dependent job"
    assert row["job_id"] == dep_job["job_id"]
    assert row["depends_on_job_id"] == prereq_job["job_id"]


def test_instantiate_from_spec_rollback_on_duplicate_dep(from_spec_db):
    """A duplicate dependency attempt must rollback — no partial pipeline."""
    from templates import TemplateManager

    tm = TemplateManager(from_spec_db)
    # Two jobs with the same ref will cause a duplicate dep insert
    spec = {
        "stages": [
            {
                "name": "work",
                "stage_order": 0,
                "jobs": [
                    {
                        "ref": "shared",
                        "agent_type": "tester",
                        "vendor": "local-ollama",
                        "model": "qwen3:8b",
                        "max_iterations": 5,
                        "timeout_seconds": 60,
                    },
                    {
                        "ref": "shared",
                        "agent_type": "dev",
                        "vendor": "local-ollama",
                        "model": "qwen3:8b",
                        "max_iterations": 5,
                        "timeout_seconds": 60,
                    },
                ],
            }
        ],
        "dependencies": [
            {"from_ref": "shared", "to_ref": "shared", "type": "success"},
            {"from_ref": "shared", "to_ref": "shared", "type": "success"},  # duplicate
        ],
    }

    with pytest.raises(Exception):
        tm.instantiate_from_spec(spec, "test", "/tmp/ws")

    # No pipeline should have been persisted
    count = from_spec_db.conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
    assert count == 0, "Partial pipeline should have been rolled back"


# ── command_template variable substitution ────────────────────────────────────


def _make_command_template_db(test_db, command_template):
    """Helper: insert a template with a command-type job using the given command_template."""
    ts = test_db._timestamp()
    test_db.conn.execute(
        "INSERT INTO pipeline_templates (template_id, name, description, default_vendor, default_model, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("tmpl-cmd", "Cmd Template", "", "local-ollama", None, ts, ts),
    )
    test_db.conn.execute(
        "INSERT INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES (?, ?, ?, ?)",
        ("ts-cmd-1", "tmpl-cmd", "run", 1),
    )
    test_db.conn.execute(
        "INSERT INTO template_jobs (template_job_id, template_stage_id, agent_type, name, prompt_template,"
        " command_template, max_iterations, timeout_seconds, vendor, model)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "tj-cmd-1",
            "ts-cmd-1",
            "command",
            "cmd-job",
            "",
            command_template,
            1,
            30,
            "local-ollama",
            None,
        ),
    )
    test_db.conn.commit()


def test_command_template_substitutes_workspace_path(test_db):
    tm = TemplateManager(test_db)
    _make_command_template_db(test_db, "run --workspace={{workspace_path}}")
    pid = tm.instantiate_template("tmpl-cmd", "my prompt", "/my/workspace")
    job = test_db.conn.execute(
        "SELECT command FROM jobs WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert job["command"] == "run --workspace=/my/workspace"


def test_command_template_substitutes_original_prompt(test_db):
    tm = TemplateManager(test_db)
    _make_command_template_db(test_db, "echo '{{original_prompt}}'")
    pid = tm.instantiate_template("tmpl-cmd", "hello world", "/ws")
    job = test_db.conn.execute(
        "SELECT command FROM jobs WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert job["command"] == "echo 'hello world'"


def test_command_template_substitutes_all_variables(test_db):
    tm = TemplateManager(test_db)
    _make_command_template_db(
        test_db, "cmd {{job_id}} {{agent_type}} {{workspace_path}} {{original_prompt}}"
    )
    pid = tm.instantiate_template("tmpl-cmd", "do it", "/work")
    job = test_db.conn.execute(
        "SELECT job_id, command FROM jobs WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    job_id = job["job_id"]
    assert f"cmd {job_id} command /work do it" == job["command"]
