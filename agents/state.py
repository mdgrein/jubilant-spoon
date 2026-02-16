"""
External state management for agent orchestrator.
All state lives on disk and is authoritative.
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Any
from dataclasses import dataclass, asdict


@dataclass
class TaskConstraints:
    """Constraints for task execution."""
    max_iterations: int
    timeout_seconds: int
    allowed_paths: list[str]


@dataclass
class Task:
    """Immutable task definition."""
    task_id: str
    prompt: str
    constraints: TaskConstraints
    created_at: str


@dataclass
class AgentState:
    """Mutable agent state."""
    status: str  # "running", "stopped", "completed", "failed"
    iteration: int
    started_at: str
    updated_at: str
    termination_reason: Optional[str] = None


@dataclass
class Heartbeat:
    """Liveness indicator."""
    iteration: int
    timestamp: str
    status: str  # Current activity (e.g., "executing_action", "waiting_for_llm")


class StateManager:
    """Manages task state on disk."""

    def __init__(self, tasks_dir: Path):
        """
        Initialize state manager.

        Args:
            tasks_dir: Base directory for all task state
        """
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def _task_dir(self, task_id: str) -> Path:
        """Get directory for specific task."""
        return self.tasks_dir / task_id

    def _timestamp(self) -> str:
        """Get ISO8601 timestamp."""
        return datetime.now(timezone.utc).isoformat()

    def create_task(self, task: Task) -> None:
        """
        Create a new task.

        Args:
            task: Task definition
        """
        task_dir = self._task_dir(task.task_id)
        task_dir.mkdir(parents=True, exist_ok=True)

        # Write immutable task definition
        task_file = task_dir / "task.json"
        task_data = {
            "task_id": task.task_id,
            "prompt": task.prompt,
            "constraints": asdict(task.constraints),
            "created_at": task.created_at,
        }
        task_file.write_text(json.dumps(task_data, indent=2), encoding="utf-8")

        # Initialize state
        initial_state = AgentState(
            status="running",
            iteration=0,
            started_at=self._timestamp(),
            updated_at=self._timestamp(),
            termination_reason=None,
        )
        self.write_state(task.task_id, initial_state)

        # Create artifacts directory
        (task_dir / "artifacts").mkdir(exist_ok=True)

        # Create empty action log
        (task_dir / "actions.jsonl").touch()

    def load_task(self, task_id: str) -> Task:
        """Load task definition from disk."""
        task_file = self._task_dir(task_id) / "task.json"
        data = json.loads(task_file.read_text(encoding="utf-8"))

        return Task(
            task_id=data["task_id"],
            prompt=data["prompt"],
            constraints=TaskConstraints(**data["constraints"]),
            created_at=data["created_at"],
        )

    def read_state(self, task_id: str) -> AgentState:
        """Read current agent state from disk."""
        state_file = self._task_dir(task_id) / "state.json"
        data = json.loads(state_file.read_text(encoding="utf-8"))

        return AgentState(**data)

    def write_state(self, task_id: str, state: AgentState) -> None:
        """Write agent state to disk."""
        state.updated_at = self._timestamp()

        state_file = self._task_dir(task_id) / "state.json"
        state_file.write_text(
            json.dumps(asdict(state), indent=2),
            encoding="utf-8"
        )

    def write_heartbeat(self, task_id: str, iteration: int, status: str) -> None:
        """Write heartbeat to disk."""
        heartbeat = Heartbeat(
            iteration=iteration,
            timestamp=self._timestamp(),
            status=status,
        )

        heartbeat_file = self._task_dir(task_id) / "heartbeat.json"
        heartbeat_file.write_text(
            json.dumps(asdict(heartbeat), indent=2),
            encoding="utf-8"
        )

    def log_action(
        self,
        task_id: str,
        iteration: int,
        llm_response: dict,
        results: list[dict],
        raw_stdout: str = "",
        raw_stderr: str = "",
    ) -> None:
        """Append action to action log."""
        log_entry = {
            "iteration": iteration,
            "timestamp": self._timestamp(),
            "llm_response": llm_response,
            "results": results,
            "raw_llm_output": {
                "stdout": raw_stdout,
                "stderr": raw_stderr,
            },
        }

        log_file = self._task_dir(task_id) / "actions.jsonl"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")

    def check_termination(self, task: Task, state: AgentState) -> Optional[str]:
        """
        Check if task should terminate.

        Returns:
            Termination reason if should terminate, None otherwise
        """
        # Check iteration limit
        if state.iteration >= task.constraints.max_iterations:
            return f"max_iterations_reached ({task.constraints.max_iterations})"

        # Check timeout
        started = datetime.fromisoformat(state.started_at)
        now = datetime.now(timezone.utc)
        elapsed = (now - started).total_seconds()

        if elapsed >= task.constraints.timeout_seconds:
            return f"timeout_exceeded ({elapsed:.1f}s / {task.constraints.timeout_seconds}s)"

        # Check external stop signal
        if state.status == "stopped":
            return "external_stop_signal"

        return None
