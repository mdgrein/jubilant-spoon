"""Scheduler service for cron-triggered pipeline instances."""

import uuid
from datetime import datetime, timezone

from croniter import croniter


def compute_next_fire(cron_expr: str, after_dt: datetime) -> str:
    """Compute the next fire time after after_dt, returned as ISO8601 string."""
    cron = croniter(cron_expr, after_dt)
    next_dt = cron.get_next(datetime)
    return next_dt.isoformat()


class SchedulerService:
    """Manages pipeline schedules."""

    def __init__(self, db, pipeline_service):
        self.db = db
        self.pipeline_service = pipeline_service

    def create_schedule(
        self,
        template_id: str,
        name: str,
        cron_expr: str,
        prompt: str,
        workspace_path: str = "/workspace",
        enabled: bool = True,
    ) -> dict:
        """Create a new schedule. Raises ValueError on bad cron or missing template."""
        if not croniter.is_valid(cron_expr):
            raise ValueError(f"Invalid cron expression: {cron_expr!r}")

        template = self.db.conn.execute(
            "SELECT template_id FROM pipeline_templates WHERE template_id = ?",
            (template_id,),
        ).fetchone()
        if not template:
            raise ValueError(f"Template {template_id!r} not found")

        schedule_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        next_fire = compute_next_fire(cron_expr, now)

        self.db.conn.execute(
            """
            INSERT INTO pipeline_schedules
                (schedule_id, template_id, name, cron_expr, prompt, workspace_path,
                 enabled, last_fired_at, next_fire_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                schedule_id,
                template_id,
                name,
                cron_expr,
                prompt,
                workspace_path,
                1 if enabled else 0,
                next_fire,
                now_iso,
                now_iso,
            ),
        )
        self.db.conn.commit()
        return self.get_schedule(schedule_id)

    def list_schedules(self) -> list[dict]:
        """List all schedules with template_name included."""
        rows = self.db.conn.execute("""
            SELECT s.*, t.name as template_name
            FROM pipeline_schedules s
            JOIN pipeline_templates t ON s.template_id = t.template_id
            ORDER BY s.created_at DESC
        """).fetchall()
        return [dict(row) for row in rows]

    def get_schedule(self, schedule_id: str) -> dict | None:
        """Get a single schedule by ID."""
        row = self.db.conn.execute(
            """
            SELECT s.*, t.name as template_name
            FROM pipeline_templates t
            JOIN pipeline_schedules s ON s.template_id = t.template_id
            WHERE s.schedule_id = ?
            """,
            (schedule_id,),
        ).fetchone()
        return dict(row) if row else None

    def update_schedule(self, schedule_id: str, **fields) -> dict:
        """Partial update. Recomputes next_fire_at if cron_expr changes."""
        allowed = {"name", "cron_expr", "enabled", "prompt", "workspace_path"}
        updates = {k: v for k, v in fields.items() if k in allowed}

        if not updates:
            return self.get_schedule(schedule_id)

        if "cron_expr" in updates:
            if not croniter.is_valid(updates["cron_expr"]):
                raise ValueError(f"Invalid cron expression: {updates['cron_expr']!r}")
            now = datetime.now(timezone.utc)
            updates["next_fire_at"] = compute_next_fire(updates["cron_expr"], now)

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [schedule_id]

        self.db.conn.execute(
            f"UPDATE pipeline_schedules SET {set_clause} WHERE schedule_id = ?",
            values,
        )
        self.db.conn.commit()
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str) -> None:
        """Delete a schedule row."""
        self.db.conn.execute(
            "DELETE FROM pipeline_schedules WHERE schedule_id = ?",
            (schedule_id,),
        )
        self.db.conn.commit()

    def enable_schedule(self, schedule_id: str) -> dict:
        """Enable a schedule."""
        return self.update_schedule(schedule_id, enabled=1)

    def disable_schedule(self, schedule_id: str) -> dict:
        """Disable a schedule."""
        return self.update_schedule(schedule_id, enabled=0)

    def get_due_schedules(self, now_iso: str) -> list[dict]:
        """Return enabled schedules whose next_fire_at <= now_iso."""
        rows = self.db.conn.execute(
            """
            SELECT s.*, t.name as template_name
            FROM pipeline_schedules s
            JOIN pipeline_templates t ON s.template_id = t.template_id
            WHERE s.enabled = 1 AND s.next_fire_at <= ?
            """,
            (now_iso,),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_fired(
        self, schedule_id: str, pipeline_id: str, fired_at_iso: str
    ) -> None:
        """Record a schedule firing: update last/next fire, set schedule_id on pipeline."""
        row = self.db.conn.execute(
            "SELECT cron_expr FROM pipeline_schedules WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
        if not row:
            return

        fired_at = datetime.fromisoformat(fired_at_iso)
        next_fire = compute_next_fire(row["cron_expr"], fired_at)
        now_iso = datetime.now(timezone.utc).isoformat()

        self.db.conn.execute(
            """
            UPDATE pipeline_schedules
            SET last_fired_at = ?, next_fire_at = ?, updated_at = ?
            WHERE schedule_id = ?
            """,
            (fired_at_iso, next_fire, now_iso, schedule_id),
        )
        self.db.conn.execute(
            "UPDATE pipelines SET schedule_id = ? WHERE pipeline_id = ?",
            (schedule_id, pipeline_id),
        )
        self.db.conn.commit()

    def get_schedule_pipelines(self, schedule_id: str) -> list[dict]:
        """Return all pipeline instances spawned by this schedule."""
        rows = self.db.conn.execute(
            "SELECT * FROM pipelines WHERE schedule_id = ? ORDER BY created_at DESC",
            (schedule_id,),
        ).fetchall()
        return [dict(row) for row in rows]
