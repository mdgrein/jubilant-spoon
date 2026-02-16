"""
Tests for artifact collection strategies.
"""

import pytest
import tempfile
import os
import json
from pathlib import Path
from unittest.mock import MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))

from artifact_strategies import (
    StdoutFinalStrategy,
    GitDiffStrategy,
    CompositeStrategy,
    get_strategy
)
from db import ClowderDB


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


def test_stdout_final_strategy_collects_output(test_db):
    """Test that stdout_final captures final model output."""
    strategy = StdoutFinalStrategy()

    job_id = "test-job-1"
    job_dir = Path.cwd()
    final_output = "This is the planner's output:\n1. Design API\n2. Write tests"

    # Create required pipeline, stage, and job for foreign key constraints
    test_db.conn.execute("""
        INSERT INTO pipelines (pipeline_id, original_prompt, status, created_at, updated_at)
        VALUES ('test-pipeline-1', 'test', 'running', datetime('now'), datetime('now'))
    """)
    test_db.conn.execute("""
        INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at)
        VALUES ('test-stage-1', 'test-pipeline-1', 'test', 1, 'running', datetime('now'))
    """)
    test_db.conn.execute("""
        INSERT INTO jobs (
            job_id, pipeline_id, stage_id, agent_type, prompt,
            max_iterations, timeout_seconds, allowed_paths,
            status, created_at, updated_at
        ) VALUES (?, 'test-pipeline-1', 'test-stage-1', 'planner', 'test', 50, 300, '["./"]',
                  'running', datetime('now'), datetime('now'))
    """, (job_id,))
    test_db.conn.commit()

    artifacts = strategy.collect_artifacts(
        job_id=job_id,
        job_dir=job_dir,
        final_output=final_output,
        db_conn=test_db.conn
    )

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact['type'] == 'model_output'
    assert artifact['name'] == 'final_output.txt'
    assert artifact['content'] == final_output
    assert artifact['job_id'] == job_id

    # Verify it was inserted into database
    db_artifact = test_db.conn.execute("""
        SELECT * FROM artifacts WHERE job_id = ?
    """, (job_id,)).fetchone()

    assert db_artifact is not None
    assert db_artifact['content'] == final_output


def test_stdout_final_strategy_returns_empty_if_no_output(test_db):
    """Test that stdout_final returns empty list if no output."""
    strategy = StdoutFinalStrategy()

    artifacts = strategy.collect_artifacts(
        job_id="test-job-2",
        job_dir=Path.cwd(),
        final_output=None,
        db_conn=test_db.conn
    )

    assert artifacts == []


def test_get_strategy_returns_stdout_final_by_default():
    """Test that get_strategy returns StdoutFinalStrategy by default."""
    strategy = get_strategy({})
    assert isinstance(strategy, StdoutFinalStrategy)

    strategy = get_strategy({"type": "stdout_final"})
    assert isinstance(strategy, StdoutFinalStrategy)


def test_get_strategy_returns_git_diff():
    """Test that get_strategy returns GitDiffStrategy."""
    strategy = get_strategy({"type": "git_diff"})
    assert isinstance(strategy, GitDiffStrategy)


def test_get_strategy_returns_composite():
    """Test that get_strategy returns CompositeStrategy."""
    config = {
        "type": "composite",
        "strategies": [
            {"type": "stdout_final"},
            {"type": "git_diff"}
        ]
    }
    strategy = get_strategy(config)
    assert isinstance(strategy, CompositeStrategy)
    assert len(strategy.strategies) == 2


def test_composite_strategy_combines_artifacts(test_db):
    """Test that composite strategy collects from multiple strategies."""
    stdout_strategy = StdoutFinalStrategy()
    # Can't easily test git_diff without a git repo, so just test composite with stdout
    composite = CompositeStrategy([stdout_strategy, stdout_strategy])

    job_id = "test-job-3"

    # Create required pipeline, stage, and job
    test_db.conn.execute("""
        INSERT INTO pipelines (pipeline_id, original_prompt, status, created_at, updated_at)
        VALUES ('test-pipeline-3', 'test', 'running', datetime('now'), datetime('now'))
    """)
    test_db.conn.execute("""
        INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at)
        VALUES ('test-stage-3', 'test-pipeline-3', 'test', 1, 'running', datetime('now'))
    """)
    test_db.conn.execute("""
        INSERT INTO jobs (
            job_id, pipeline_id, stage_id, agent_type, prompt,
            max_iterations, timeout_seconds, allowed_paths,
            status, created_at, updated_at
        ) VALUES (?, 'test-pipeline-3', 'test-stage-3', 'planner', 'test', 50, 300, '["./"]',
                  'running', datetime('now'), datetime('now'))
    """, (job_id,))
    test_db.conn.commit()

    artifacts = composite.collect_artifacts(
        job_id="test-job-3",
        job_dir=Path.cwd(),
        final_output="Test output",
        db_conn=test_db.conn
    )

    # Should get 2 artifacts (one from each strategy)
    assert len(artifacts) == 2
    assert all(a['type'] == 'model_output' for a in artifacts)


def test_artifact_has_required_fields(test_db):
    """Test that artifacts have all required fields."""
    strategy = StdoutFinalStrategy()

    job_id = "test-job-4"

    # Create required pipeline, stage, and job
    test_db.conn.execute("""
        INSERT INTO pipelines (pipeline_id, original_prompt, status, created_at, updated_at)
        VALUES ('test-pipeline-4', 'test', 'running', datetime('now'), datetime('now'))
    """)
    test_db.conn.execute("""
        INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at)
        VALUES ('test-stage-4', 'test-pipeline-4', 'test', 1, 'running', datetime('now'))
    """)
    test_db.conn.execute("""
        INSERT INTO jobs (
            job_id, pipeline_id, stage_id, agent_type, prompt,
            max_iterations, timeout_seconds, allowed_paths,
            status, created_at, updated_at
        ) VALUES (?, 'test-pipeline-4', 'test-stage-4', 'planner', 'test', 50, 300, '["./"]',
                  'running', datetime('now'), datetime('now'))
    """, (job_id,))
    test_db.conn.commit()

    artifacts = strategy.collect_artifacts(
        job_id="test-job-4",
        job_dir=Path.cwd(),
        final_output="Test",
        db_conn=test_db.conn
    )

    artifact = artifacts[0]
    required_fields = [
        'artifact_id', 'job_id', 'type', 'name', 'description',
        'file_path', 'content', 'content_hash', 'size_bytes',
        'metadata', 'created_at'
    ]

    for field in required_fields:
        assert field in artifact

    assert artifact['size_bytes'] == len("Test".encode('utf-8'))
