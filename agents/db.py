"""
SQLite database for Clowder state management.
Replaces file-based state with structured database.
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Any
from dataclasses import dataclass, asdict


@dataclass
class Task:
    """Task definition."""
    task_id: str
    prompt: str
    agent_type: str
    max_iterations: int
    timeout_seconds: int
    allowed_paths: list[str]
    created_at: str
    parent_task_id: Optional[str] = None
    metadata: Optional[dict] = None


@dataclass
class AgentState:
    """Agent runtime state."""
    task_id: str
    status: str
    iteration: int
    started_at: Optional[str]
    updated_at: str
    termination_reason: Optional[str] = None


class ClowderDB:
    """Database interface for Clowder state."""

    def __init__(self, db_path: str = "clowder.db"):
        """Initialize database connection."""
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,  # Allow multi-threaded access
            timeout=10.0,  # Wait up to 10s for locks
        )
        # Enable foreign keys
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Enable WAL mode for better concurrency
        self.conn.execute("PRAGMA journal_mode = WAL")
        # Return rows as dicts
        self.conn.row_factory = sqlite3.Row

    def init_schema(self):
        """Initialize database schema from schema.sql."""
        schema_path = Path(__file__).parent / "schema.sql"
        schema = schema_path.read_text()
        self.conn.executescript(schema)
        self.conn.commit()

    def _timestamp(self) -> str:
        """Get ISO8601 timestamp."""
        return datetime.now(timezone.utc).isoformat()

    # ==================== Task Operations ====================

    def create_task(self, task: Task) -> None:
        """Create a new task."""
        self.conn.execute("""
            INSERT INTO tasks (
                task_id, prompt, agent_type, max_iterations, timeout_seconds,
                allowed_paths, created_at, parent_task_id, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task.task_id,
            task.prompt,
            task.agent_type,
            task.max_iterations,
            task.timeout_seconds,
            json.dumps(task.allowed_paths),
            task.created_at,
            task.parent_task_id,
            json.dumps(task.metadata) if task.metadata else None,
        ))

        # Initialize state
        self.conn.execute("""
            INSERT INTO agent_state (task_id, status, iteration, updated_at)
            VALUES (?, 'pending', 0, ?)
        """, (task.task_id, self._timestamp()))

        self.conn.commit()

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID."""
        row = self.conn.execute("""
            SELECT * FROM tasks WHERE task_id = ?
        """, (task_id,)).fetchone()

        if not row:
            return None

        return Task(
            task_id=row["task_id"],
            prompt=row["prompt"],
            agent_type=row["agent_type"],
            max_iterations=row["max_iterations"],
            timeout_seconds=row["timeout_seconds"],
            allowed_paths=json.loads(row["allowed_paths"]),
            created_at=row["created_at"],
            parent_task_id=row["parent_task_id"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else None,
        )

    # ==================== State Operations ====================

    def get_state(self, task_id: str) -> Optional[AgentState]:
        """Get current agent state."""
        row = self.conn.execute("""
            SELECT * FROM agent_state WHERE task_id = ?
        """, (task_id,)).fetchone()

        if not row:
            return None

        return AgentState(
            task_id=row["task_id"],
            status=row["status"],
            iteration=row["iteration"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            termination_reason=row["termination_reason"],
        )

    def update_state(self, state: AgentState) -> None:
        """Update agent state."""
        self.conn.execute("""
            UPDATE agent_state
            SET status = ?, iteration = ?, started_at = ?,
                updated_at = ?, termination_reason = ?
            WHERE task_id = ?
        """, (
            state.status,
            state.iteration,
            state.started_at,
            self._timestamp(),
            state.termination_reason,
            state.task_id,
        ))
        self.conn.commit()

    # ==================== Action Operations ====================

    def log_action(
        self,
        job_id: str,
        iteration: int,
        llm_response: dict,
        results: list[dict],
        raw_stdout: str = "",
        raw_stderr: str = "",
    ) -> None:
        """Log an action (iteration)."""
        self.conn.execute("""
            INSERT INTO actions (
                job_id, iteration, timestamp, llm_response,
                results, raw_stdout, raw_stderr
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            job_id,
            iteration,
            self._timestamp(),
            json.dumps(llm_response),
            json.dumps(results),
            raw_stdout,
            raw_stderr,
        ))
        self.conn.commit()

    def get_actions(self, job_id: str, limit: int = 100) -> list[dict]:
        """Get recent actions for a job."""
        rows = self.conn.execute("""
            SELECT * FROM actions
            WHERE job_id = ?
            ORDER BY iteration DESC
            LIMIT ?
        """, (job_id, limit)).fetchall()

        return [dict(row) for row in rows]

    def get_action_history(self, job_id: str, last_n: int = 5) -> list[dict]:
        """Get last N actions formatted for context building."""
        rows = self.conn.execute("""
            SELECT iteration, llm_response, results
            FROM actions
            WHERE job_id = ?
            ORDER BY iteration DESC
            LIMIT ?
        """, (job_id, last_n)).fetchall()

        history = []
        for row in reversed(rows):  # Reverse to get chronological order
            history.append({
                "iteration": row["iteration"],
                "llm_response": json.loads(row["llm_response"]),
                "results": json.loads(row["results"]),
            })

        return history

    # ==================== Termination Checking ====================

    def check_termination(self, task: Task, state: AgentState) -> Optional[str]:
        """
        Check if task should terminate.

        Returns:
            Termination reason if should terminate, None otherwise
        """
        # Check iteration limit
        if state.iteration >= task.max_iterations:
            return f"max_iterations_reached ({task.max_iterations})"

        # Check timeout
        if state.started_at:
            from datetime import datetime, timezone
            started = datetime.fromisoformat(state.started_at)
            now = datetime.now(timezone.utc)
            elapsed = (now - started).total_seconds()

            if elapsed >= task.timeout_seconds:
                return f"timeout_exceeded ({elapsed:.1f}s / {task.timeout_seconds}s)"

        # Check external stop signal
        if state.status == "stopped":
            return "external_stop_signal"

        return None

    # ==================== Query Operations ====================

    def get_active_tasks(self) -> list[dict]:
        """Get all active tasks."""
        rows = self.conn.execute("""
            SELECT * FROM active_tasks
        """).fetchall()

        return [dict(row) for row in rows]

    def get_task_summary(self, task_id: str) -> Optional[dict]:
        """Get task summary with stats."""
        row = self.conn.execute("""
            SELECT * FROM task_summary WHERE task_id = ?
        """, (task_id,)).fetchone()

        return dict(row) if row else None

    def get_failed_actions(self, limit: int = 100) -> list[dict]:
        """Get recent failed actions for debugging."""
        rows = self.conn.execute("""
            SELECT * FROM failed_actions
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return [dict(row) for row in rows]

    # ==================== Cleanup ====================

    def close(self):
        """Close database connection."""
        self.conn.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
