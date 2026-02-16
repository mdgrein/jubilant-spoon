"""
Pipeline Orchestrator
Monitors database and executes jobs when dependencies are satisfied.
"""

import time
import logging
from pathlib import Path
from typing import Optional
from db import ClowderDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Orchestrates pipeline execution.

    Responsibilities:
    - Poll database for pending pipelines
    - Find ready-to-run jobs (dependencies satisfied)
    - Spawn harness processes for jobs
    - Track running jobs
    - Update pipeline/job statuses
    """

    def __init__(self, db_path: str = "clowder.db", poll_interval: int = 5):
        """
        Initialize orchestrator.

        Args:
            db_path: Path to SQLite database
            poll_interval: Seconds between polls
        """
        self.db = ClowderDB(db_path)
        self.poll_interval = poll_interval
        self.running_jobs = {}  # job_id -> process
        self.running = False

    def start(self):
        """Start orchestrator main loop."""
        logger.info("Starting orchestrator...")
        self.running = True

        try:
            while self.running:
                self._orchestration_cycle()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("Orchestrator interrupted")
        finally:
            self._cleanup()

    def stop(self):
        """Stop orchestrator gracefully."""
        logger.info("Stopping orchestrator...")
        self.running = False

    def _orchestration_cycle(self):
        """
        Single orchestration cycle.

        1. Find pending pipelines
        2. Start pending pipelines
        3. Find ready jobs in running pipelines
        4. Spawn jobs
        5. Check running jobs for completion
        """
        # Find pending pipelines and start them
        pending_pipelines = self._find_pending_pipelines()
        for pipeline_id in pending_pipelines:
            self._start_pipeline(pipeline_id)

        # Find running pipelines
        running_pipelines = self._find_running_pipelines()

        for pipeline_id in running_pipelines:
            # Find ready jobs (dependencies satisfied)
            ready_jobs = self._find_ready_jobs(pipeline_id)

            for job_id in ready_jobs:
                if job_id not in self.running_jobs:
                    self._spawn_job(job_id)

            # Check if pipeline is complete
            self._check_pipeline_completion(pipeline_id)

        # Check running jobs for completion
        self._poll_running_jobs()

    def _find_pending_pipelines(self) -> list[str]:
        """Find pipelines with status='pending'."""
        rows = self.db.conn.execute("""
            SELECT pipeline_id FROM pipelines WHERE status = 'pending'
        """).fetchall()

        return [row['pipeline_id'] for row in rows]

    def _find_running_pipelines(self) -> list[str]:
        """Find pipelines with status='running'."""
        rows = self.db.conn.execute("""
            SELECT pipeline_id FROM pipelines WHERE status = 'running'
        """).fetchall()

        return [row['pipeline_id'] for row in rows]

    def _start_pipeline(self, pipeline_id: str):
        """Transition pipeline from pending to running."""
        logger.info(f"Starting pipeline {pipeline_id[:8]}...")

        self.db.conn.execute("""
            UPDATE pipelines SET status = 'running', updated_at = ?
            WHERE pipeline_id = ?
        """, (self.db._timestamp(), pipeline_id))

        # Also update all stages to running (they'll complete individually)
        self.db.conn.execute("""
            UPDATE stages SET status = 'running'
            WHERE pipeline_id = ? AND status = 'pending'
        """, (pipeline_id,))

        self.db.conn.commit()

    def _find_ready_jobs(self, pipeline_id: str) -> list[str]:
        """
        Find jobs ready to run (all dependencies satisfied).

        A job is ready if:
        - status = 'pending'
        - all dependency jobs have status = 'completed'
        """
        rows = self.db.conn.execute("""
            SELECT j.job_id
            FROM jobs j
            WHERE j.pipeline_id = ?
              AND j.status = 'pending'
              AND NOT EXISTS (
                  SELECT 1
                  FROM job_dependencies jd
                  JOIN jobs dep_job ON jd.depends_on_job_id = dep_job.job_id
                  WHERE jd.job_id = j.job_id
                    AND (
                        -- For 'success' dependencies, job must be completed
                        (jd.dependency_type = 'success' AND dep_job.status != 'completed')
                        -- For 'failure' dependencies, job must be failed
                        OR (jd.dependency_type = 'failure' AND dep_job.status != 'failed')
                        -- For 'always' dependencies, job must be done (completed or failed)
                        OR (jd.dependency_type = 'always' AND dep_job.status NOT IN ('completed', 'failed'))
                    )
              )
        """, (pipeline_id,)).fetchall()

        return [row['job_id'] for row in rows]

    def _spawn_job(self, job_id: str):
        """
        Spawn harness process for a job.

        For now, just update status to 'running' and mark as spawned.
        Actual subprocess spawning comes in Phase 1.2.
        """
        logger.info(f"Spawning job {job_id[:8]}...")

        # Update job status
        self.db.conn.execute("""
            UPDATE jobs SET status = 'running', started_at = ?, updated_at = ?
            WHERE job_id = ?
        """, (self.db._timestamp(), self.db._timestamp(), job_id))

        self.db.conn.commit()

        # Mark as running (no actual process yet)
        self.running_jobs[job_id] = "running"

        logger.info(f"Job {job_id[:8]} started")

    def _poll_running_jobs(self):
        """
        Poll running jobs to check if they're done.

        For now, jobs complete immediately (placeholder).
        In Phase 1.2, this will check actual process status.
        """
        for job_id in list(self.running_jobs.keys()):
            # Placeholder: mark job as completed after 1 cycle
            logger.info(f"Job {job_id[:8]} completed (placeholder)")

            self.db.conn.execute("""
                UPDATE jobs
                SET status = 'completed', completed_at = ?, updated_at = ?, termination_reason = 'success'
                WHERE job_id = ?
            """, (self.db._timestamp(), self.db._timestamp(), job_id))

            self.db.conn.commit()

            # Remove from running jobs
            del self.running_jobs[job_id]

    def _check_pipeline_completion(self, pipeline_id: str):
        """
        Check if pipeline is complete.

        Complete if all jobs are done (completed or failed).
        """
        # Count total jobs and completed jobs
        row = self.db.conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status IN ('completed', 'failed') THEN 1 ELSE 0 END) as done
            FROM jobs
            WHERE pipeline_id = ?
        """, (pipeline_id,)).fetchone()

        if row['total'] == row['done']:
            # Check if any failed
            failed = self.db.conn.execute("""
                SELECT COUNT(*) as count FROM jobs
                WHERE pipeline_id = ? AND status = 'failed'
            """, (pipeline_id,)).fetchone()['count']

            status = 'failed' if failed > 0 else 'completed'

            logger.info(f"Pipeline {pipeline_id[:8]} {status}")

            self.db.conn.execute("""
                UPDATE pipelines
                SET status = ?, completed_at = ?, updated_at = ?
                WHERE pipeline_id = ?
            """, (status, self.db._timestamp(), self.db._timestamp(), pipeline_id))

            # Update stages
            self.db.conn.execute("""
                UPDATE stages
                SET status = ?
                WHERE pipeline_id = ?
            """, (status, pipeline_id))

            self.db.conn.commit()

    def _cleanup(self):
        """Clean up resources on shutdown."""
        logger.info("Cleaning up...")
        # Kill running jobs (Phase 1.2)
        self.db.close()


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Clowder Pipeline Orchestrator")
    parser.add_argument(
        "--db",
        type=str,
        default="clowder.db",
        help="Database path (default: clowder.db)"
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="Poll interval in seconds (default: 5)"
    )

    args = parser.parse_args()

    orchestrator = Orchestrator(db_path=args.db, poll_interval=args.poll_interval)
    orchestrator.start()


if __name__ == "__main__":
    main()
