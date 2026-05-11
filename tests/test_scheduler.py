"""Unit tests for SchedulerService."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

from db import ClowderDB
from templates import TemplateManager
from server.services import PipelineService
from scheduler import SchedulerService, compute_next_fire


@pytest.fixture
def test_db():
    db = ClowderDB(":memory:")
    db.init_pipeline_schema()
    yield db
    db.conn.close()


@pytest.fixture
def service(test_db):
    tm = TemplateManager(test_db)
    ps = PipelineService(test_db, tm)
    return SchedulerService(test_db, ps)


@pytest.fixture
def sample_template(test_db):
    ts = test_db._timestamp()
    test_db.conn.execute(
        """INSERT INTO pipeline_templates (template_id, name, description, created_at, updated_at)
           VALUES ('tpl-1', 'Test Template', 'desc', ?, ?)""",
        (ts, ts),
    )
    test_db.conn.execute(
        "INSERT INTO template_stages (template_stage_id, template_id, name, stage_order) VALUES ('ts-1', 'tpl-1', 'work', 1)"
    )
    test_db.conn.execute(
        """INSERT INTO template_jobs (template_job_id, template_stage_id, agent_type, name, prompt_template, max_iterations, timeout_seconds)
           VALUES ('tj-1', 'ts-1', 'dev', 'worker', '{{original_prompt}}', 5, 60)"""
    )
    test_db.conn.commit()


# ── compute_next_fire ──────────────────────────────────────────────────────────


def test_compute_next_fire_returns_future_time():
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    result = compute_next_fire("* * * * *", now)
    result_dt = datetime.fromisoformat(result)
    assert result_dt > now


def test_compute_next_fire_hourly():
    now = datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)
    result = compute_next_fire("0 * * * *", now)
    result_dt = datetime.fromisoformat(result)
    assert result_dt.minute == 0
    assert result_dt > now


# ── create_schedule ────────────────────────────────────────────────────────────


def test_create_schedule_success(service, sample_template):
    s = service.create_schedule("tpl-1", "Daily run", "0 2 * * *", "do stuff")
    assert s["schedule_id"] is not None
    assert s["name"] == "Daily run"
    assert s["cron_expr"] == "0 2 * * *"
    assert s["enabled"] == 1
    assert s["last_fired_at"] is None
    assert s["next_fire_at"] is not None
    assert s["template_name"] == "Test Template"


def test_create_schedule_computes_next_fire(service, sample_template):
    before = datetime.now(timezone.utc).isoformat()
    s = service.create_schedule("tpl-1", "Every minute", "* * * * *", "ping")
    next_fire = s["next_fire_at"]
    assert next_fire > before


def test_create_schedule_invalid_cron_raises(service, sample_template):
    with pytest.raises(ValueError, match="Invalid cron"):
        service.create_schedule("tpl-1", "Bad cron", "not a cron", "task")


def test_create_schedule_unknown_template_raises(service):
    with pytest.raises(ValueError, match="not found"):
        service.create_schedule("no-such-template", "x", "* * * * *", "task")


# ── list_schedules ─────────────────────────────────────────────────────────────


def test_list_schedules_empty(service):
    assert service.list_schedules() == []


def test_list_schedules_returns_all(service, sample_template):
    service.create_schedule("tpl-1", "A", "0 0 * * *", "a")
    service.create_schedule("tpl-1", "B", "0 1 * * *", "b")
    result = service.list_schedules()
    assert len(result) == 2
    names = {r["name"] for r in result}
    assert names == {"A", "B"}


# ── get_schedule ───────────────────────────────────────────────────────────────


def test_get_schedule_not_found(service):
    assert service.get_schedule("nonexistent") is None


def test_get_schedule_found(service, sample_template):
    created = service.create_schedule("tpl-1", "My schedule", "0 0 * * *", "work")
    fetched = service.get_schedule(created["schedule_id"])
    assert fetched is not None
    assert fetched["name"] == "My schedule"


# ── update_schedule ────────────────────────────────────────────────────────────


def test_update_schedule_name(service, sample_template):
    s = service.create_schedule("tpl-1", "Old name", "0 0 * * *", "task")
    updated = service.update_schedule(s["schedule_id"], name="New name")
    assert updated["name"] == "New name"


def test_update_schedule_recomputes_next_fire_on_cron_change(service, sample_template):
    s = service.create_schedule("tpl-1", "Test", "0 2 * * *", "task")
    old_next = s["next_fire_at"]
    updated = service.update_schedule(s["schedule_id"], cron_expr="0 3 * * *")
    assert updated["next_fire_at"] != old_next
    assert updated["cron_expr"] == "0 3 * * *"


def test_update_schedule_invalid_cron_raises(service, sample_template):
    s = service.create_schedule("tpl-1", "Test", "0 0 * * *", "task")
    with pytest.raises(ValueError, match="Invalid cron"):
        service.update_schedule(s["schedule_id"], cron_expr="bad expr")


# ── enable / disable ──────────────────────────────────────────────────────────


def test_disable_schedule(service, sample_template):
    s = service.create_schedule("tpl-1", "Test", "0 0 * * *", "task", enabled=True)
    result = service.disable_schedule(s["schedule_id"])
    assert result["enabled"] == 0


def test_enable_schedule(service, sample_template):
    s = service.create_schedule("tpl-1", "Test", "0 0 * * *", "task", enabled=False)
    result = service.enable_schedule(s["schedule_id"])
    assert result["enabled"] == 1


# ── get_due_schedules ─────────────────────────────────────────────────────────


def test_get_due_schedules_returns_overdue(service, sample_template):
    s = service.create_schedule("tpl-1", "Test", "* * * * *", "task")
    # Use a far-future now_iso so the schedule is overdue
    far_future = "2099-01-01T00:00:00+00:00"
    due = service.get_due_schedules(far_future)
    assert any(d["schedule_id"] == s["schedule_id"] for d in due)


def test_get_due_schedules_excludes_disabled(service, sample_template):
    s = service.create_schedule("tpl-1", "Disabled", "* * * * *", "task", enabled=False)
    far_future = "2099-01-01T00:00:00+00:00"
    due = service.get_due_schedules(far_future)
    assert all(d["schedule_id"] != s["schedule_id"] for d in due)


def test_get_due_schedules_excludes_future(service, sample_template):
    s = service.create_schedule("tpl-1", "Future", "0 2 * * *", "task")
    # Use past now_iso so next_fire_at is in the future relative to it
    past = "2000-01-01T00:00:00+00:00"
    due = service.get_due_schedules(past)
    assert all(d["schedule_id"] != s["schedule_id"] for d in due)


# ── record_fired ──────────────────────────────────────────────────────────────


def test_record_fired_updates_last_fired_at(service, sample_template, test_db):
    s = service.create_schedule("tpl-1", "Test", "* * * * *", "task")
    # Create a pipeline to reference
    tm = TemplateManager(test_db)
    pipeline_id = tm.instantiate_template("tpl-1", "task", "/workspace")

    fired_at = datetime.now(timezone.utc).isoformat()
    service.record_fired(s["schedule_id"], pipeline_id, fired_at)

    updated = service.get_schedule(s["schedule_id"])
    assert updated["last_fired_at"] == fired_at


def test_record_fired_advances_next_fire_at(service, sample_template, test_db):
    # Use hourly cron so fired_at (now) and creation time produce different next_fire values
    s = service.create_schedule("tpl-1", "Test", "0 * * * *", "task")

    tm = TemplateManager(test_db)
    pipeline_id = tm.instantiate_template("tpl-1", "task", "/workspace")

    # Simulate firing at a fixed past time so next_fire is deterministic
    fired_at = "2020-01-01T10:00:00+00:00"
    service.record_fired(s["schedule_id"], pipeline_id, fired_at)

    updated = service.get_schedule(s["schedule_id"])
    # next fire after 10:00 with "0 * * * *" should be 11:00
    assert updated["next_fire_at"] > fired_at
    assert "11:00" in updated["next_fire_at"]


def test_record_fired_sets_schedule_id_on_pipeline(service, sample_template, test_db):
    s = service.create_schedule("tpl-1", "Test", "* * * * *", "task")

    tm = TemplateManager(test_db)
    pipeline_id = tm.instantiate_template("tpl-1", "task", "/workspace")

    fired_at = datetime.now(timezone.utc).isoformat()
    service.record_fired(s["schedule_id"], pipeline_id, fired_at)

    row = test_db.conn.execute(
        "SELECT schedule_id FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)
    ).fetchone()
    assert row["schedule_id"] == s["schedule_id"]


# ── get_schedule_pipelines ────────────────────────────────────────────────────


def test_get_schedule_pipelines_empty(service, sample_template):
    s = service.create_schedule("tpl-1", "Test", "* * * * *", "task")
    assert service.get_schedule_pipelines(s["schedule_id"]) == []


def test_get_schedule_pipelines_returns_only_own(service, sample_template, test_db):
    s1 = service.create_schedule("tpl-1", "S1", "* * * * *", "task")
    s2 = service.create_schedule("tpl-1", "S2", "0 0 * * *", "task")

    tm = TemplateManager(test_db)

    p1 = tm.instantiate_template("tpl-1", "task", "/workspace")
    p2 = tm.instantiate_template("tpl-1", "task", "/workspace")
    p3 = tm.instantiate_template("tpl-1", "task", "/workspace")

    fired_at = datetime.now(timezone.utc).isoformat()
    service.record_fired(s1["schedule_id"], p1, fired_at)
    service.record_fired(s1["schedule_id"], p2, fired_at)
    service.record_fired(s2["schedule_id"], p3, fired_at)

    results = service.get_schedule_pipelines(s1["schedule_id"])
    assert len(results) == 2
    ids = {r["pipeline_id"] for r in results}
    assert ids == {p1, p2}


# ── delete_schedule ───────────────────────────────────────────────────────────


def test_delete_schedule(service, sample_template):
    s = service.create_schedule("tpl-1", "Test", "* * * * *", "task")
    service.delete_schedule(s["schedule_id"])
    assert service.get_schedule(s["schedule_id"]) is None
