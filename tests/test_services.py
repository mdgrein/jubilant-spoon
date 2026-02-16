"""
Unit tests for server business logic (service layer).
Tests business logic directly without HTTP layer.
"""

import pytest
import tempfile
import os
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))

from db import ClowderDB
from templates import TemplateManager
from server.services import PipelineService


@pytest.fixture
def test_db():
    """Create a temporary test database."""
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
def service(test_db):
    """Create a PipelineService with test database."""
    template_manager = TemplateManager(test_db)
    return PipelineService(test_db, template_manager)


@pytest.fixture
def sample_template(test_db):
    """Create a sample template in the database."""
    ts = test_db._timestamp()

    # Insert template
    test_db.conn.execute("""
        INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at)
        VALUES ('build', 'Build Pipeline', 'Compiles the project', ?, ?)
    """, (ts, ts))

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
    """Should return list of template IDs."""
    templates = service.list_templates()
    assert "build" in templates
    assert isinstance(templates, list)


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


# --- create_pipeline ---

def test_create_pipeline_with_valid_template(service, sample_template):
    """Should create pipeline from template."""
    result = service.create_pipeline("build", "Build my project", "/tmp/workspace")

    assert result["template_id"] == "build"
    assert result["prompt"] == "Build my project"
    assert result["status"] == "pending"
    assert "pipeline_id" in result
    assert result["name"] == "Build my project"  # Not truncated


def test_create_pipeline_truncates_long_names(service, sample_template):
    """Should truncate long prompts in name field."""
    long_prompt = "a" * 100
    result = service.create_pipeline("build", long_prompt, "/tmp/workspace")

    assert len(result["name"]) == 50
    assert result["prompt"] == long_prompt  # Full prompt preserved


def test_create_pipeline_with_invalid_template(service):
    """Should raise ValueError for nonexistent template."""
    with pytest.raises(ValueError, match="not found"):
        service.create_pipeline("nonexistent", "Test", "/tmp")


def test_create_pipeline_creates_jobs(service, sample_template, test_db):
    """Should create jobs in database."""
    result = service.create_pipeline("build", "Build my project", "/tmp/workspace")
    pipeline_id = result["pipeline_id"]

    # Check that jobs were created
    jobs = test_db.conn.execute("""
        SELECT job_id, agent_type, status FROM jobs WHERE pipeline_id = ?
    """, (pipeline_id,)).fetchall()

    assert len(jobs) == 2
    assert jobs[0]["status"] == "pending"
    assert jobs[1]["status"] == "pending"


# --- stop_pipeline ---

def test_stop_pipeline(service, sample_template, test_db):
    """Should update pipeline status to cancelled."""
    # Create a pipeline first
    result = service.create_pipeline("build", "Build my project", "/tmp")
    pipeline_id = result["pipeline_id"]

    # Stop it
    stop_result = service.stop_pipeline(pipeline_id)

    assert stop_result["status"] == "cancelled"
    assert stop_result["pipeline_id"] == pipeline_id
    assert "name" in stop_result

    # Verify in database
    row = test_db.conn.execute("""
        SELECT status FROM pipelines WHERE pipeline_id = ?
    """, (pipeline_id,)).fetchone()

    assert row["status"] == "cancelled"


def test_stop_pipeline_truncates_name(service, sample_template):
    """Should truncate long pipeline names in response."""
    long_prompt = "a" * 100
    result = service.create_pipeline("build", long_prompt, "/tmp")
    pipeline_id = result["pipeline_id"]

    stop_result = service.stop_pipeline(pipeline_id)

    assert len(stop_result["name"]) == 50


# --- get_running_pipelines ---

def test_get_running_pipelines_empty(service):
    """Should return empty list when no running pipelines."""
    pipelines = service.get_running_pipelines()
    assert pipelines == []


def test_get_running_pipelines(service, sample_template):
    """Should return list of active pipelines."""
    # Create some pipelines
    service.create_pipeline("build", "Build 1", "/tmp")
    service.create_pipeline("build", "Build 2", "/tmp")

    pipelines = service.get_running_pipelines()

    assert len(pipelines) == 2
    assert pipelines[0]["status"] in ("pending", "running")
    assert pipelines[1]["status"] in ("pending", "running")


def test_get_running_pipelines_includes_stages(service, sample_template):
    """Should include nested stages and jobs."""
    service.create_pipeline("build", "Build 1", "/tmp")

    pipelines = service.get_running_pipelines()

    assert len(pipelines) == 1
    assert "stages" in pipelines[0]
    assert len(pipelines[0]["stages"]) == 1
    assert "jobs" in pipelines[0]["stages"][0]


def test_get_running_pipelines_excludes_cancelled(service, sample_template):
    """Should not include cancelled pipelines."""
    result = service.create_pipeline("build", "Build 1", "/tmp")
    pipeline_id = result["pipeline_id"]

    service.stop_pipeline(pipeline_id)

    pipelines = service.get_running_pipelines()

    assert len(pipelines) == 0


# --- get_pipeline ---

def test_get_pipeline_not_found(service):
    """Should return None for nonexistent pipeline."""
    result = service.get_pipeline("00000000-0000-0000-0000-000000000000")
    assert result is None


def test_get_pipeline(service, sample_template):
    """Should return pipeline with jobs."""
    create_result = service.create_pipeline("build", "Build my project", "/tmp")
    pipeline_id = create_result["pipeline_id"]

    result = service.get_pipeline(pipeline_id)

    assert result is not None
    assert result["pipeline"]["pipeline_id"] == pipeline_id
    assert len(result["jobs"]) == 2


def test_get_pipeline_includes_stage_info(service, sample_template):
    """Should include stage information in jobs."""
    create_result = service.create_pipeline("build", "Build my project", "/tmp")
    pipeline_id = create_result["pipeline_id"]

    result = service.get_pipeline(pipeline_id)

    # Check that jobs have stage info
    for job in result["jobs"]:
        assert "stage_name" in job
        assert "stage_order" in job
