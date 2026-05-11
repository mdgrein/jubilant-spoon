"""
Business logic layer for Clowder server.
Extracted from route handlers to enable unit testing.
"""

from typing import Optional
import sys
from pathlib import Path

# Add pipeline directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

from db import ClowderDB
from templates import TemplateManager
from output_utils import clean_job_output


class PipelineService:
    """Handles pipeline business logic."""

    def __init__(self, db: ClowderDB, template_manager: TemplateManager):
        self.db = db
        self.template_manager = template_manager

    def list_templates(self) -> list[dict]:
        """Get list of templates with id, name, description, and category."""
        return self.template_manager.list_templates()

    def get_template_details(self, template_id: str) -> Optional[dict]:
        """
        Get full template details including stages, jobs, and dependencies.

        Returns None if template not found.
        """
        # Get template
        template = self.db.conn.execute(
            """
            SELECT * FROM pipeline_templates WHERE template_id = ?
        """,
            (template_id,),
        ).fetchone()

        if not template:
            return None

        # Get stages
        stages_rows = self.db.conn.execute(
            """
            SELECT * FROM template_stages WHERE template_id = ? ORDER BY stage_order
        """,
            (template_id,),
        ).fetchall()

        stages = []
        for stage_row in stages_rows:
            stage_id = stage_row["template_stage_id"]

            # Get jobs for this stage
            jobs_rows = self.db.conn.execute(
                """
                SELECT * FROM template_jobs WHERE template_stage_id = ?
            """,
                (stage_id,),
            ).fetchall()

            jobs = []
            for job_row in jobs_rows:
                job_id = job_row["template_job_id"]

                # Get dependencies for this job with resolved IDs
                deps_rows = self.db.conn.execute(
                    """
                    SELECT dep_job.template_job_id as dep_job_id, tjd.dependency_type
                    FROM template_job_dependencies tjd
                    JOIN template_jobs dep_job ON tjd.depends_on_template_job_id = dep_job.template_job_id
                    WHERE tjd.template_job_id = ?
                """,
                    (job_id,),
                ).fetchall()

                dependencies = [
                    {"depends_on": dep["dep_job_id"], "type": dep["dependency_type"]}
                    for dep in deps_rows
                ]

                jobs.append(
                    {
                        "id": job_id,
                        "agent_type": job_row["agent_type"],
                        "name": job_row["name"],
                        "vendor": job_row["vendor"],
                        "model": job_row["model"],
                        "prompt_template": job_row["prompt_template"],
                        "command_template": job_row["command_template"],
                        "max_iterations": job_row["max_iterations"],
                        "timeout_seconds": job_row["timeout_seconds"],
                        "artifact_strategy": job_row["artifact_strategy"],
                        "retry_strategy": job_row["retry_strategy"],
                        "chain_id": job_row["chain_id"],
                        "dependencies": dependencies,
                    }
                )

            stages.append({"id": stage_id, "name": stage_row["name"], "jobs": jobs})

        return {
            "id": template["template_id"],
            "name": template["name"],
            "description": template["description"],
            "stages": stages,
        }

    def stop_pipeline(self, pipeline_id: str) -> dict:
        """
        Cancel a running pipeline.

        Args:
            pipeline_id: Pipeline to stop

        Returns:
            Pipeline info with updated status
        """
        # Get pipeline name before updating
        pipeline = self.db.conn.execute(
            """
            SELECT original_prompt FROM pipelines WHERE pipeline_id = ?
        """,
            (pipeline_id,),
        ).fetchone()

        self.db.conn.execute(
            """
            UPDATE pipelines SET status = 'cancelled', updated_at = ?
            WHERE pipeline_id = ?
        """,
            (self.db._timestamp(), pipeline_id),
        )
        self.db.conn.commit()

        name = pipeline["original_prompt"][:50] if pipeline else "Unknown"
        return {"pipeline_id": pipeline_id, "name": name, "status": "cancelled"}

    def get_running_pipelines(self) -> list[dict]:
        """
        List currently running pipelines with nested stages and jobs.

        Returns:
            List of pipeline dicts with full hierarchy
        """
        # Get active pipelines (pending or running)
        pipelines_rows = self.db.conn.execute("""
            SELECT * FROM pipelines WHERE status IN ('pending', 'running')
        """).fetchall()

        result = []
        for pipeline_row in pipelines_rows:
            pipeline_id = pipeline_row["pipeline_id"]

            # Get stages for this pipeline
            stages_rows = self.db.conn.execute(
                """
                SELECT * FROM stages WHERE pipeline_id = ? ORDER BY stage_order
            """,
                (pipeline_id,),
            ).fetchall()

            stages = []
            for stage_row in stages_rows:
                stage_id = stage_row["stage_id"]

                # Get jobs for this stage
                jobs_rows = self.db.conn.execute(
                    """
                    SELECT job_id, name, agent_type, prompt, status, iteration, max_iterations,
                           job_output, retry_count, chain_id
                    FROM jobs WHERE stage_id = ?
                """,
                    (stage_id,),
                ).fetchall()

                jobs = []
                for job_row in jobs_rows:
                    dep_rows = self.db.conn.execute(
                        "SELECT depends_on_job_id FROM job_dependencies WHERE job_id = ?",
                        (job_row["job_id"],),
                    ).fetchall()
                    jobs.append(
                        {
                            "id": job_row["job_id"],
                            "name": job_row["name"] or job_row["agent_type"],
                            "status": job_row["status"],
                            "log": job_row["job_output"] or "",
                            "retries": job_row["retry_count"] or 0,
                            "chain_id": job_row["chain_id"],
                            "depends_on": [r["depends_on_job_id"] for r in dep_rows],
                        }
                    )

                stages.append({"name": stage_row["name"], "jobs": jobs})

            result.append(
                {
                    "id": pipeline_id,
                    "name": pipeline_row["original_prompt"][
                        :50
                    ],  # Truncate for display
                    "description": pipeline_row["original_prompt"],
                    "status": pipeline_row["status"],
                    "stages": stages,
                }
            )

        return result

    def get_recent_pipelines(self, limit: int = 10) -> list[dict]:
        """
        List recently completed/failed pipelines with nested stages and jobs.

        Args:
            limit: Maximum number of recent pipelines to return (default: 10)

        Returns:
            List of pipeline dicts with full hierarchy
        """
        # Get recent completed/failed/cancelled pipelines
        pipelines_rows = self.db.conn.execute(
            """
            SELECT * FROM pipelines
            WHERE status IN ('completed', 'failed', 'cancelled')
            ORDER BY completed_at DESC
            LIMIT ?
        """,
            (limit,),
        ).fetchall()

        result = []
        for pipeline_row in pipelines_rows:
            pipeline_id = pipeline_row["pipeline_id"]

            # Get stages for this pipeline
            stages_rows = self.db.conn.execute(
                """
                SELECT * FROM stages WHERE pipeline_id = ? ORDER BY stage_order
            """,
                (pipeline_id,),
            ).fetchall()

            stages = []
            for stage_row in stages_rows:
                stage_id = stage_row["stage_id"]

                # Get jobs for this stage
                jobs_rows = self.db.conn.execute(
                    """
                    SELECT job_id, name, agent_type, prompt, status, iteration, max_iterations,
                           job_output, retry_count, chain_id
                    FROM jobs WHERE stage_id = ?
                """,
                    (stage_id,),
                ).fetchall()

                jobs = []
                for job_row in jobs_rows:
                    dep_rows = self.db.conn.execute(
                        "SELECT depends_on_job_id FROM job_dependencies WHERE job_id = ?",
                        (job_row["job_id"],),
                    ).fetchall()
                    jobs.append(
                        {
                            "id": job_row["job_id"],
                            "name": job_row["name"] or job_row["agent_type"],
                            "status": job_row["status"],
                            "log": job_row["job_output"] or "",
                            "retries": job_row["retry_count"] or 0,
                            "chain_id": job_row["chain_id"],
                            "depends_on": [r["depends_on_job_id"] for r in dep_rows],
                        }
                    )

                stages.append({"name": stage_row["name"], "jobs": jobs})

            result.append(
                {
                    "id": pipeline_id,
                    "name": pipeline_row["original_prompt"][
                        :50
                    ],  # Truncate for display
                    "description": pipeline_row["original_prompt"],
                    "status": pipeline_row["status"],
                    "completed_at": pipeline_row["completed_at"],
                    "stages": stages,
                }
            )

        return result

    def get_job_log(self, job_id: str) -> Optional[dict]:
        """
        Returns { 'output': str, 'is_live': bool } or None if job not found.
        is_live = True when status is 'running' or 'pending' (output may grow).
        """
        row = self.db.conn.execute(
            "SELECT job_output, status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not row:
            return None
        live_statuses = ("pending", "running")
        return {
            "output": clean_job_output(row["job_output"] or ""),
            "is_live": row["status"] in live_statuses,
        }

    def create_template(self, spec: dict) -> dict:
        """
        Create a new template from a spec dict.

        Returns full template dict.
        Raises ValueError if name is missing.
        """
        if not spec.get("name"):
            raise ValueError("Template name is required")
        template_id = self.template_manager.create_template(spec)
        return self.template_manager.get_template(template_id)

    def update_template_metadata(
        self, template_id: str, updates: dict
    ) -> Optional[dict]:
        """
        Update template metadata fields.

        Returns updated template dict, or None if not found.
        """
        return self.template_manager.update_template_metadata(template_id, **updates)

    def update_template_structure(
        self, template_id: str, stages: list[dict], dependencies: list[dict]
    ) -> Optional[dict]:
        """
        Replace template stages/jobs/dependencies.

        Returns updated template dict, or None if not found.
        """
        found = self.template_manager.replace_template_structure(
            template_id, stages, dependencies
        )
        if not found:
            return None
        return self.template_manager.get_template(template_id)

    def delete_template(self, template_id: str) -> bool:
        """
        Delete a template.

        Returns False if not found.
        Raises ValueError if schedules reference it.
        """
        return self.template_manager.delete_template(template_id)

    def delete_pipeline(self, pipeline_id: str) -> bool:
        """
        Permanently delete a pipeline and all its stages, jobs, actions, artifacts,
        artifact_consumption, and job_dependencies from the database.

        Returns True if deleted, False if not found.
        """
        row = self.db.conn.execute(
            "SELECT pipeline_id FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)
        ).fetchone()
        if row is None:
            return False
        self.db.conn.execute(
            "DELETE FROM pipelines WHERE pipeline_id = ?", (pipeline_id,)
        )
        self.db.conn.commit()
        return True

    def get_pipeline(self, pipeline_id: str) -> Optional[dict]:
        """
        Get a single pipeline by its ID.

        Args:
            pipeline_id: Pipeline ID to fetch

        Returns:
            Dict with 'pipeline' and 'jobs' keys, or None if not found
        """
        pipeline = self.db.conn.execute(
            """
            SELECT * FROM pipelines WHERE pipeline_id = ?
        """,
            (pipeline_id,),
        ).fetchone()

        if not pipeline:
            return None

        # Get jobs
        jobs = self.db.conn.execute(
            """
            SELECT j.*, s.name as stage_name, s.stage_order
            FROM jobs j
            JOIN stages s ON j.stage_id = s.stage_id
            WHERE j.pipeline_id = ?
            ORDER BY s.stage_order
        """,
            (pipeline_id,),
        ).fetchall()

        return {
            "pipeline": dict(pipeline),
            "jobs": [dict(job) for job in jobs],
        }
