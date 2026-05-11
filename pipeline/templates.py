"""
Pipeline template management.
Create pipelines from templates with customization.
"""

import uuid
import json
from typing import Optional
from datetime import datetime, timezone
from db import ClowderDB


class TemplateManager:
    """Manage pipeline templates and instantiation."""

    def __init__(self, db: ClowderDB):
        self.db = db

    def list_templates(self) -> list[dict]:
        """List all available templates."""
        rows = self.db.conn.execute("""
            SELECT
                t.template_id,
                t.name,
                t.description,
                t.category,
                COUNT(DISTINCT ts.template_stage_id) as stage_count,
                COUNT(DISTINCT tj.template_job_id) as job_count
            FROM pipeline_templates t
            LEFT JOIN template_stages ts ON t.template_id = ts.template_id
            LEFT JOIN template_jobs tj ON ts.template_stage_id = tj.template_stage_id
            GROUP BY t.template_id
            ORDER BY t.name
        """).fetchall()

        return [dict(row) for row in rows]

    def get_template(self, template_id: str) -> Optional[dict]:
        """Get template details with stages and jobs."""
        # Get template
        template_row = self.db.conn.execute(
            """
            SELECT * FROM pipeline_templates WHERE template_id = ?
        """,
            (template_id,),
        ).fetchone()

        if not template_row:
            return None

        template = dict(template_row)

        # Get stages
        stage_rows = self.db.conn.execute(
            """
            SELECT * FROM template_stages
            WHERE template_id = ?
            ORDER BY stage_order
        """,
            (template_id,),
        ).fetchall()

        stages = []
        for stage_row in stage_rows:
            stage = dict(stage_row)

            # Get jobs for this stage
            job_rows = self.db.conn.execute(
                """
                SELECT * FROM template_jobs
                WHERE template_stage_id = ?
            """,
                (stage["template_stage_id"],),
            ).fetchall()

            stage["jobs"] = [dict(job_row) for job_row in job_rows]
            stages.append(stage)

        template["stages"] = stages

        # Get dependencies
        dep_rows = self.db.conn.execute(
            """
            SELECT * FROM template_job_dependencies
            WHERE template_job_id IN (
                SELECT template_job_id FROM template_jobs
                WHERE template_stage_id IN (
                    SELECT template_stage_id FROM template_stages
                    WHERE template_id = ?
                )
            )
        """,
            (template_id,),
        ).fetchall()

        template["dependencies"] = [dict(dep_row) for dep_row in dep_rows]

        return template

    def template_to_spec(self, template_id: str) -> Optional[dict]:
        """Convert a template to an instantiate_from_spec-compatible spec dict.

        Applies template-level default_vendor/model to jobs where job-level
        values are NULL. prompt_templates are left as-is ({{original_prompt}}
        substituted later by instantiate_from_spec).

        Returns None if template not found.
        """
        template = self.get_template(template_id)
        if not template:
            return None

        stages = []
        for stage in template["stages"]:
            jobs = []
            for job in stage["jobs"]:
                jobs.append(
                    {
                        "ref": job["template_job_id"],
                        "template_job_id": job["template_job_id"],
                        "agent_type": job["agent_type"],
                        "name": job.get("name", ""),
                        "chain_id": job.get("chain_id"),
                        "vendor": job.get("vendor") or template.get("default_vendor"),
                        "model": job.get("model") or template.get("default_model"),
                        "prompt_template": job.get("prompt_template", ""),
                        "command_template": job.get("command_template"),
                        "max_iterations": job.get("max_iterations", 50),
                        "timeout_seconds": job.get("timeout_seconds", 300),
                        "artifact_strategy": job.get("artifact_strategy"),
                        "retry_strategy": job.get("retry_strategy"),
                    }
                )
            stages.append(
                {
                    "name": stage["name"],
                    "stage_order": stage["stage_order"],
                    "jobs": jobs,
                }
            )

        dependencies = []
        for dep in template["dependencies"]:
            dependencies.append(
                {
                    "from_ref": dep["depends_on_template_job_id"],
                    "to_ref": dep["template_job_id"],
                    "type": dep.get("dependency_type", "success"),
                }
            )

        return {"stages": stages, "dependencies": dependencies}

    def instantiate_template(
        self,
        template_id: str,
        original_prompt: str,
        workspace_path: str,
        excluded_stage_ids: Optional[list[str]] = None,
        excluded_job_ids: Optional[list[str]] = None,
    ) -> str:
        """
        Create a pipeline from a template.

        Args:
            template_id: Template to use
            original_prompt: User's prompt (replaces {{original_prompt}})
            workspace_path: Allowed workspace path
            excluded_stage_ids: Template stage IDs to exclude (currently unused)
            excluded_job_ids: Template job IDs to exclude (currently unused)

        Returns:
            Pipeline ID
        """
        spec = self.template_to_spec(template_id)
        if spec is None:
            raise ValueError(f"Template {template_id} not found")
        return self.instantiate_from_spec(spec, original_prompt, workspace_path)

    def instantiate_from_spec(
        self,
        spec: dict,
        original_prompt: str,
        workspace_path: str,
    ) -> str:
        """
        Create a pipeline from an inline spec dict (no stored template required).

        Args:
            spec: Dict with 'stages' (list) and 'dependencies' (list).
                  Each stage has 'name', 'stage_order', and 'jobs'.
                  Each job has 'ref', 'agent_type', 'prompt_template', etc.
                  Each dependency has 'from_ref', 'to_ref', 'type'.
            original_prompt: User's prompt (replaces {{original_prompt}})
            workspace_path: Allowed workspace path

        Returns:
            Pipeline ID
        """
        pipeline_id = str(uuid.uuid4())
        try:
            self.db.conn.execute(
                """
                INSERT INTO pipelines (
                    pipeline_id, template_id, original_prompt, status, created_at, updated_at
                ) VALUES (?, NULL, ?, 'pending', ?, ?)
            """,
                (
                    pipeline_id,
                    original_prompt,
                    self._timestamp(),
                    self._timestamp(),
                ),
            )

            ref_to_job_id: dict[str, str] = {}

            for stage in spec.get("stages", []):
                stage_id = str(uuid.uuid4())
                self.db.conn.execute(
                    """
                    INSERT INTO stages (
                        stage_id, pipeline_id, name, stage_order, status, created_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                    (
                        stage_id,
                        pipeline_id,
                        stage["name"],
                        stage["stage_order"],
                        self._timestamp(),
                    ),
                )

                for job in stage.get("jobs", []):
                    job_id = str(uuid.uuid4())
                    ref = job.get("ref")
                    if ref:
                        ref_to_job_id[ref] = job_id

                    prompt_template = job.get("prompt_template") or ""
                    prompt = prompt_template.replace(
                        "{{original_prompt}}", original_prompt
                    )

                    command = None
                    if job.get("command_template"):
                        command = job["command_template"].replace("{{job_id}}", job_id)
                        command = command.replace("{{prompt}}", prompt)
                        command = command.replace(
                            "{{agent_type}}", job.get("agent_type", "")
                        )
                        command = command.replace("{{workspace_path}}", workspace_path)
                        command = command.replace(
                            "{{original_prompt}}", original_prompt
                        )

                    artifact_strategy = job.get("artifact_strategy")
                    retry_strategy = job.get("retry_strategy")
                    self.db.conn.execute(
                        """
                        INSERT INTO jobs (
                            job_id, pipeline_id, stage_id, agent_type, name, prompt, original_prompt, command,
                            max_iterations, timeout_seconds, vendor, model, allowed_paths,
                            artifact_strategy, retry_strategy, template_job_id, chain_id, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                        (
                            job_id,
                            pipeline_id,
                            stage_id,
                            job.get("agent_type", "dev"),
                            job.get("name", ""),
                            prompt,
                            prompt,
                            command,
                            job.get("max_iterations", 50),
                            job.get("timeout_seconds", 300),
                            job.get("vendor"),
                            job.get("model"),
                            f'["{workspace_path}"]',
                            json.dumps(artifact_strategy)
                            if isinstance(artifact_strategy, dict)
                            else artifact_strategy,
                            json.dumps(retry_strategy)
                            if isinstance(retry_strategy, dict)
                            else retry_strategy,
                            job.get("template_job_id"),
                            job.get("chain_id"),
                            self._timestamp(),
                            self._timestamp(),
                        ),
                    )

            # from_ref = prerequisite, to_ref = dependent (waiting) job
            for dep in spec.get("dependencies", []):
                prereq_job_id = ref_to_job_id.get(dep["from_ref"])
                waiting_job_id = ref_to_job_id.get(dep["to_ref"])
                if prereq_job_id and waiting_job_id:
                    self.db.conn.execute(
                        """
                        INSERT INTO job_dependencies (
                            job_id, depends_on_job_id, dependency_type
                        ) VALUES (?, ?, ?)
                    """,
                        (
                            waiting_job_id,
                            prereq_job_id,
                            dep.get("type", "success"),
                        ),
                    )

            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise
        return pipeline_id

    def create_template(self, spec: dict) -> str:
        """
        Create a new template from a spec dict.

        Args:
            spec: Dict with 'name', 'description', optional 'category',
                  'stages' (list), and 'dependencies' (list).

        Returns:
            New template_id
        """
        template_id = spec.get("template_id") or str(uuid.uuid4())
        now = self._timestamp()
        self.db.conn.execute(
            """
            INSERT INTO pipeline_templates (template_id, name, description, category, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                template_id,
                spec["name"],
                spec.get("description", ""),
                spec.get("category"),
                now,
                now,
            ),
        )
        self._insert_stages_jobs_deps(
            template_id, spec.get("stages", []), spec.get("dependencies", [])
        )
        self.db.conn.commit()
        return template_id

    def _insert_stages_jobs_deps(
        self,
        template_id: str,
        stages: list[dict],
        dependencies: list[dict],
    ) -> None:
        """Insert stages, jobs, and dependencies for a template."""
        ref_to_job_id: dict[str, str] = {}

        for stage in stages:
            stage_id = stage.get("template_stage_id") or str(uuid.uuid4())
            self.db.conn.execute(
                """
                INSERT INTO template_stages (template_stage_id, template_id, name, stage_order)
                VALUES (?, ?, ?, ?)
            """,
                (stage_id, template_id, stage["name"], stage["stage_order"]),
            )

            for job in stage.get("jobs", []):
                job_id = job.get("template_job_id") or str(uuid.uuid4())
                ref = job.get("ref") or job_id
                ref_to_job_id[ref] = job_id

                artifact_strategy = job.get("artifact_strategy")
                retry_strategy = job.get("retry_strategy")
                self.db.conn.execute(
                    """
                    INSERT INTO template_jobs (
                        template_job_id, template_stage_id, agent_type, name,
                        prompt_template, command_template, max_iterations, timeout_seconds,
                        vendor, model, artifact_strategy, retry_strategy
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        job_id,
                        stage_id,
                        job.get("agent_type", "dev"),
                        job.get("name", ""),
                        job.get("prompt_template", ""),
                        job.get("command_template"),
                        job.get("max_iterations", 50),
                        job.get("timeout_seconds", 300),
                        job.get("vendor"),
                        job.get("model"),
                        json.dumps(artifact_strategy)
                        if isinstance(artifact_strategy, dict)
                        else artifact_strategy,
                        json.dumps(retry_strategy)
                        if isinstance(retry_strategy, dict)
                        else retry_strategy,
                    ),
                )

        for dep in dependencies:
            from_ref = dep.get("from_ref") or dep.get("template_job_id")
            to_ref = dep.get("to_ref") or dep.get("depends_on_template_job_id")
            from_id = ref_to_job_id.get(from_ref)
            to_id = ref_to_job_id.get(to_ref)
            if from_id and to_id:
                self.db.conn.execute(
                    """
                    INSERT INTO template_job_dependencies (template_job_id, depends_on_template_job_id, dependency_type)
                    VALUES (?, ?, ?)
                """,
                    (
                        from_id,
                        to_id,
                        dep.get("type", dep.get("dependency_type", "success")),
                    ),
                )

    def replace_template_structure(
        self, template_id: str, stages: list[dict], dependencies: list[dict]
    ) -> bool:
        """
        Replace all stages, jobs, and dependencies for a template.

        Returns False if template not found, True on success.
        """
        row = self.db.conn.execute(
            "SELECT 1 FROM pipeline_templates WHERE template_id = ?", (template_id,)
        ).fetchone()
        if not row:
            return False

        self.db.conn.execute(
            """
            DELETE FROM template_job_dependencies WHERE template_job_id IN (
                SELECT template_job_id FROM template_jobs
                WHERE template_stage_id IN (
                    SELECT template_stage_id FROM template_stages WHERE template_id = ?
                )
            )
        """,
            (template_id,),
        )
        self.db.conn.execute(
            """
            DELETE FROM template_jobs WHERE template_stage_id IN (
                SELECT template_stage_id FROM template_stages WHERE template_id = ?
            )
        """,
            (template_id,),
        )
        self.db.conn.execute(
            "DELETE FROM template_stages WHERE template_id = ?", (template_id,)
        )

        self._insert_stages_jobs_deps(template_id, stages, dependencies)

        self.db.conn.execute(
            "UPDATE pipeline_templates SET updated_at = ? WHERE template_id = ?",
            (self._timestamp(), template_id),
        )
        self.db.conn.commit()
        return True

    def update_template_metadata(self, template_id: str, **fields) -> Optional[dict]:
        """
        Update metadata fields (name, description, category, default_vendor, default_model).

        Returns updated template dict, or None if not found.
        """
        allowed = {"name", "description", "category", "default_vendor", "default_model"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_template(template_id)

        updates["updated_at"] = self._timestamp()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [template_id]
        self.db.conn.execute(
            f"UPDATE pipeline_templates SET {set_clause} WHERE template_id = ?",
            values,
        )
        self.db.conn.commit()
        return self.get_template(template_id)

    def delete_template(self, template_id: str) -> bool:
        """
        Delete a template and all its stages/jobs/dependencies.

        Returns False if not found.
        Raises ValueError if schedules reference this template.
        """
        row = self.db.conn.execute(
            "SELECT 1 FROM pipeline_templates WHERE template_id = ?", (template_id,)
        ).fetchone()
        if not row:
            return False

        schedule_count = self.db.conn.execute(
            "SELECT COUNT(*) FROM pipeline_schedules WHERE template_id = ?",
            (template_id,),
        ).fetchone()[0]
        if schedule_count > 0:
            raise ValueError(
                f"Cannot delete template {template_id}: {schedule_count} schedule(s) reference it"
            )

        self.db.conn.execute(
            """
            DELETE FROM template_job_dependencies WHERE template_job_id IN (
                SELECT template_job_id FROM template_jobs
                WHERE template_stage_id IN (
                    SELECT template_stage_id FROM template_stages WHERE template_id = ?
                )
            )
        """,
            (template_id,),
        )
        self.db.conn.execute(
            """
            DELETE FROM template_jobs WHERE template_stage_id IN (
                SELECT template_stage_id FROM template_stages WHERE template_id = ?
            )
        """,
            (template_id,),
        )
        self.db.conn.execute(
            "DELETE FROM template_stages WHERE template_id = ?", (template_id,)
        )
        self.db.conn.execute(
            "DELETE FROM pipeline_templates WHERE template_id = ?", (template_id,)
        )
        self.db.conn.commit()
        return True

    def _timestamp(self) -> str:
        """Get ISO8601 timestamp."""
        return datetime.now(timezone.utc).isoformat()
