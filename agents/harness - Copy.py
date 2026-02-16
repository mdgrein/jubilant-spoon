"""
Clowder-specific agent harness.

Wraps the standalone agent (agent.py) and integrates it with Clowder's
job/pipeline/database system.

The harness:
- Loads jobs from the database
- Creates and runs agent instances
- Syncs agent state back to the database
- Handles external stop signals
- Logs actions for observability
"""

import json
import sys
import logging
import argparse
import uuid
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

from agent import Agent, AgentError
from db import ClowderDB


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class HarnessError(Exception):
    """Fatal harness error."""
    pass


class AgentHarness:
    """
    Clowder-specific agent harness.

    Wraps the standalone Agent class and integrates it with Clowder's
    job/pipeline/database system.
    """

    def __init__(
        self,
        db_path: str = "clowder.db",
        model: str = "qwen3:8b",
    ):
        """
        Initialize harness.

        Args:
            db_path: Path to SQLite database
            model: Model name to use (must be available in WSL Ollama)
        """
        self.db = ClowderDB(db_path)
        self.model = model

    def _load_job(self, job_id: str) -> Optional[dict]:
        """Load job from database."""
        row = self.db.conn.execute("""
            SELECT * FROM jobs WHERE job_id = ?
        """, (job_id,)).fetchone()
        return dict(row) if row else None

    def _check_external_stop(self, job: dict) -> Optional[str]:
        """
        Check if job has external stop signal.

        Returns:
            Stop reason if stopped externally, None otherwise
        """
        if job['status'] in ('stopped', 'cancelled'):
            return "external_stop_signal"
        return None

    def _load_action_history(self, job_id: str) -> list[dict]:
        """Load action history from database."""
        return self.db.get_action_history(job_id, last_n=5)

    def run(self, job_id: str) -> str:
        """
        Run agent harness for a job.

        Creates a standalone Agent instance and runs it, syncing state
        with the database after each iteration.

        Args:
            job_id: ID of job to execute

        Returns:
            Termination reason
        """
        logger.info(f"Starting harness for job {job_id}")

        try:
            # Load job
            job = self._load_job(job_id)
            if not job:
                raise HarnessError(f"Job {job_id} not found")
            logger.info(f"Job: {job['prompt']}")

            # Parse job parameters
            allowed_paths = json.loads(job['allowed_paths'])

            # Create standalone agent
            agent = Agent(
                prompt=job['prompt'],
                allowed_paths=allowed_paths,
                model=self.model,
                max_iterations=job['max_iterations'],
                timeout_seconds=job['timeout_seconds'],
            )

            # Load and restore action history from DB
            db_history = self._load_action_history(job_id)
            agent.action_history = db_history
            agent.iteration = job['iteration']

            # Restore start time if job already started
            if job['started_at']:
                agent.started_at = datetime.fromisoformat(job['started_at'])

            # Main loop - run agent iterations
            while True:
                # Check for external stop signals
                job = self._load_job(job_id)
                if not job:
                    raise HarnessError(f"Job {job_id} not found")

                external_stop = self._check_external_stop(job)
                if external_stop:
                    logger.info(f"Terminating: {external_stop}")
                    self.db.conn.execute("""
                        UPDATE jobs
                        SET status = 'completed', termination_reason = ?, completed_at = ?, updated_at = ?
                        WHERE job_id = ?
                    """, (external_stop, self.db._timestamp(), self.db._timestamp(), job_id))
                    self.db.conn.commit()
                    return external_stop

                # Run one iteration
                try:
                    result = agent.run_iteration()
                except AgentError as e:
                    raise HarnessError(f"Agent error: {e}")

                # Sync agent state to database
                started_at = agent.started_at.isoformat() if agent.started_at else self.db._timestamp()

                self.db.conn.execute("""
                    UPDATE jobs
                    SET iteration = ?, started_at = ?, updated_at = ?
                    WHERE job_id = ?
                """, (agent.iteration, started_at, self.db._timestamp(), job_id))
                self.db.conn.commit()

                # Log action to database
                self.db.log_action(
                    job_id,
                    result['iteration'],
                    result['llm_response'],
                    result['results'],
                    raw_stdout=result['raw_stdout'],
                    raw_stderr=result['raw_stderr'],
                )

                # Check if agent signaled termination
                if result['should_terminate']:
                    termination_reason = result['termination_reason']
                    logger.info(f"Terminating: {termination_reason}")
                    self.db.conn.execute("""
                        UPDATE jobs
                        SET status = 'completed', termination_reason = ?, completed_at = ?, updated_at = ?
                        WHERE job_id = ?
                    """, (termination_reason, self.db._timestamp(), self.db._timestamp(), job_id))
                    self.db.conn.commit()
                    return termination_reason

        except HarnessError as e:
            logger.error(f"Fatal error: {e}")
            self.db.conn.execute("""
                UPDATE jobs
                SET status = 'failed', termination_reason = ?, completed_at = ?, updated_at = ?
                WHERE job_id = ?
            """, (str(e), self.db._timestamp(), self.db._timestamp(), job_id))
            self.db.conn.commit()
            return str(e)

        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
            self.db.conn.execute("""
                UPDATE jobs
                SET status = 'failed', termination_reason = ?, completed_at = ?, updated_at = ?
                WHERE job_id = ?
            """, (f"unexpected_error: {e}", self.db._timestamp(), self.db._timestamp(), job_id))
            self.db.conn.commit()
            return f"unexpected_error: {e}"


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Run LLM agent jobs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run existing job by ID
  python harness.py <job_id>

  # Run with direct prompt (uses current directory as workspace)
  python harness.py --prompt "Find all Python files and list them"

  # Run with custom workspace
  python harness.py --prompt "Create hello.txt" --workspace /path/to/dir

  # Customize constraints
  python harness.py --prompt "Complex task" --max-iterations 100 --timeout 600
        """
    )

    parser.add_argument(
        "job_id",
        nargs="?",
        help="Job ID to run (existing job mode)"
    )
    parser.add_argument(
        "--prompt",
        type=str,
        help="Job prompt (creates new job)"
    )
    parser.add_argument(
        "--workspace",
        type=str,
        help="Workspace directory (default: current directory)"
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=50,
        help="Maximum iterations (default: 50)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds (default: 300)"
    )
    parser.add_argument(
        "--model",
        type=str,
        default="qwen3:8b",
        help="Model name (default: qwen3:8b)"
    )

    args = parser.parse_args()

    # Determine mode
    if args.prompt:
        # New job mode
        job_id = _create_job_from_prompt(
            prompt=args.prompt,
            workspace=args.workspace,
            max_iterations=args.max_iterations,
            timeout=args.timeout,
        )
        logger.info(f"Created job {job_id}")
    elif args.job_id:
        # Existing job mode
        job_id = args.job_id
    else:
        parser.print_help()
        sys.exit(1)

    # Run harness
    harness = AgentHarness(db_path="clowder.db", model=args.model)
    reason = harness.run(job_id)

    print(f"\nJob terminated: {reason}")
    print(f"\nJob ID: {job_id}")

    # Exit with failure code if job didn't complete successfully
    # max_iterations_reached or timeout = failure, job should be retried
    if "max_iterations_reached" in reason or "timeout_exceeded" in reason:
        sys.exit(1)


def _create_job_from_prompt(
    prompt: str,
    workspace: Optional[str],
    max_iterations: int,
    timeout: int,
) -> str:
    """
    Create a new job from a prompt (for CLI testing).

    Creates a minimal pipeline with one stage and one job.

    Args:
        prompt: Job prompt
        workspace: Workspace directory (None = current directory)
        max_iterations: Maximum iterations
        timeout: Timeout in seconds

    Returns:
        Job ID
    """
    # Generate IDs
    pipeline_id = str(uuid.uuid4())
    stage_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())

    # Determine workspace
    if workspace:
        workspace_path = Path(workspace).resolve()
    else:
        workspace_path = Path.cwd()

    if not workspace_path.exists():
        logger.error(f"Workspace does not exist: {workspace_path}")
        sys.exit(1)

    logger.info(f"Using workspace: {workspace_path}")

    # Initialize database
    db = ClowderDB("clowder.db")
    timestamp = db._timestamp()

    # Create minimal pipeline
    db.conn.execute("""
        INSERT INTO pipelines (pipeline_id, template_id, original_prompt, workspace_path, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
    """, (pipeline_id, None, prompt, str(workspace_path), timestamp, timestamp))

    # Create stage
    db.conn.execute("""
        INSERT INTO stages (stage_id, pipeline_id, name, stage_order, status, created_at, updated_at)
        VALUES (?, ?, 'dev', 0, 'pending', ?, ?)
    """, (stage_id, pipeline_id, timestamp, timestamp))

    # Create job
    db.conn.execute("""
        INSERT INTO jobs (
            job_id, pipeline_id, stage_id, agent_type, prompt,
            max_iterations, timeout_seconds, allowed_paths,
            status, iteration, created_at, updated_at
        )
        VALUES (?, ?, ?, 'dev', ?, ?, ?, ?, 'pending', 0, ?, ?)
    """, (
        job_id, pipeline_id, stage_id, prompt,
        max_iterations, timeout, json.dumps([str(workspace_path)]),
        timestamp, timestamp
    ))

    db.conn.commit()
    db.close()

    print(f"\n{'='*60}")
    print(f"Created new job: {job_id}")
    print(f"{'='*60}")
    print(f"Prompt: {prompt}")
    print(f"Workspace: {workspace_path}")
    print(f"Max iterations: {max_iterations}")
    print(f"Timeout: {timeout}s")
    print(f"{'='*60}\n")

    return job_id


if __name__ == "__main__":
    main()
