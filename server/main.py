from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, PlainTextResponse
from pydantic import BaseModel
import asyncio
import sys
import json
import time
from pathlib import Path
from typing import List, Optional
import logging

# Add project root and pipeline directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

from db import ClowderDB
from templates import TemplateManager
from scheduler import SchedulerService
from server.services import PipelineService
from artifact_strategies import get_strategy
from job_multiplier import check_and_spawn_multiplied_jobs
from output_utils import clean_job_output
import log_levels  # noqa: F401  registers TRACE and MODEL levels
from log_levels import TRACE, MODEL


# Custom file handler that flushes after every write
class FlushingFileHandler(logging.FileHandler):
    """FileHandler that flushes after each emit to prevent log loss on crashes."""

    def emit(self, record):
        super().emit(record)
        self.flush()


# Configure logger (actual config happens in main() with uvicorn)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize database and load seed data on startup."""
    logger.info("Initializing database...")

    # Check if database is already initialized
    tables_exist = db.conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='pipeline_templates'
    """).fetchone()

    # Load schemas only if database is new
    if not tables_exist:
        logger.info("Database is new, creating schema...")
        schema_path = Path(__file__).parent.parent / "pipeline" / "schema_pipelines.sql"
        if schema_path.exists():
            try:
                schema = schema_path.read_text(encoding="utf-8")
                db.conn.executescript(schema)
                db.conn.commit()
                logger.info("Database schema created")
            except Exception as e:
                logger.error(f"Error creating schema: {e}")
                raise
    else:
        logger.info("Database schema already exists")

    # Additive migrations: add columns that may be missing from older DBs.
    # SQLite raises OperationalError if a column already exists; we swallow it.
    _migrations = [
        "ALTER TABLE template_jobs ADD COLUMN model TEXT",
        "ALTER TABLE template_jobs ADD COLUMN vendor TEXT DEFAULT 'local-ollama'",
        "ALTER TABLE jobs ADD COLUMN model TEXT",
        "ALTER TABLE jobs ADD COLUMN vendor TEXT NOT NULL DEFAULT 'local-ollama'",
        "ALTER TABLE pipeline_templates ADD COLUMN default_vendor TEXT NOT NULL DEFAULT 'local-ollama'",
        "ALTER TABLE pipeline_templates ADD COLUMN default_model TEXT",
        "ALTER TABLE pipeline_templates ADD COLUMN category TEXT",
        "ALTER TABLE pipelines ADD COLUMN schedule_id TEXT",
        "ALTER TABLE template_jobs ADD COLUMN chain_id TEXT",
    ]
    for sql in _migrations:
        try:
            db.conn.execute(sql)
            db.conn.commit()
        except Exception:
            pass  # column already exists

    # Create new tables/indexes idempotently (handles existing DBs).
    _new_tables_sql = """
        CREATE TABLE IF NOT EXISTS pipeline_schedules (
            schedule_id    TEXT PRIMARY KEY,
            template_id    TEXT NOT NULL REFERENCES pipeline_templates(template_id) ON DELETE RESTRICT,
            name           TEXT NOT NULL,
            cron_expr      TEXT NOT NULL,
            prompt         TEXT NOT NULL,
            workspace_path TEXT NOT NULL DEFAULT '/workspace',
            enabled        INTEGER NOT NULL DEFAULT 1,
            last_fired_at  TEXT,
            next_fire_at   TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_schedules_next_fire ON pipeline_schedules(next_fire_at) WHERE enabled=1;
        CREATE INDEX IF NOT EXISTS idx_pipelines_schedule ON pipelines(schedule_id);
    """
    try:
        db.conn.executescript(_new_tables_sql)
        db.conn.commit()
    except Exception as e:
        logger.error(f"Error creating schedule tables: {e}")

    # Load/refresh seed templates on every startup.
    # The seed file uses INSERT OR IGNORE, so existing rows are skipped safely.
    # New templates added to the file appear automatically after a restart.
    seed_path = Path(__file__).parent.parent / "pipeline" / "seed_templates.sql"
    if seed_path.exists():
        try:
            db.conn.executescript(seed_path.read_text(encoding="utf-8"))
            db.conn.commit()
            logger.info("Seed templates refreshed")
        except Exception as e:
            logger.error(f"Error loading seeds: {e}")

    # Start orchestration and scheduler background tasks
    asyncio.create_task(orchestration_loop())
    asyncio.create_task(scheduler_loop())
    logger.info("Orchestration and scheduler loops started")

    yield


app = FastAPI(lifespan=lifespan)

# Initialize database
db = ClowderDB("clowder.db")
template_manager = TemplateManager(db)
pipeline_service = PipelineService(db, template_manager)
scheduler_service = SchedulerService(db, pipeline_service)

_static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# Middleware to log requests with timing at TRACE level
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    logger.log(TRACE, f"Request: {request.method} {request.url}")
    response = await call_next(request)
    duration = (time.time() - start) * 1000  # ms
    logger.log(TRACE, f"Response: {response.status_code} ({duration:.2f}ms)")
    return response


# Old in-memory storage removed - now using database


running_jobs = {}  # job_id -> asyncio.Task

LOG_FLUSH_EVERY = 10  # Flush accumulated log to DB every N lines during execution


def find_ready_job():
    """
    Return the next pending job whose dependencies are all satisfied, or None.

    Dependency satisfaction rules by type:
      success   — dep must be 'completed'
      failure   — dep must be 'failed'
      completed / always — dep must be in any terminal state ('completed', 'failed', 'skipped')
    """
    return db.conn.execute("""
        SELECT j.job_id, j.pipeline_id
        FROM jobs j
        WHERE j.status = 'pending'
          AND NOT EXISTS (
              SELECT 1
              FROM job_dependencies jd
              JOIN jobs dep ON jd.depends_on_job_id = dep.job_id
              WHERE jd.job_id = j.job_id
                AND NOT (
                    (jd.dependency_type = 'success'  AND dep.status = 'completed')
                    OR (jd.dependency_type = 'failure' AND dep.status = 'failed')
                    OR (jd.dependency_type IN ('completed', 'always')
                        AND dep.status IN ('completed', 'failed', 'skipped'))
                )
          )
        LIMIT 1
    """).fetchone()


async def orchestration_loop():
    """Background task that orchestrates pipeline execution."""
    logger.info("Orchestration loop started")

    while True:
        try:
            # Start pending pipelines
            pending = db.conn.execute("""
                SELECT pipeline_id FROM pipelines WHERE status = 'pending'
            """).fetchall()

            for row in pending:
                pipeline_id = row["pipeline_id"]
                db.conn.execute(
                    """
                    UPDATE pipelines SET status = 'running', updated_at = ?
                    WHERE pipeline_id = ?
                """,
                    (db._timestamp(), pipeline_id),
                )
                db.conn.commit()
                logger.info(f"Started pipeline {pipeline_id[:8]}")

            # Find ready jobs - only run ONE job at a time (crawl before we walk)
            # Only spawn a new job if nothing is currently running
            if not running_jobs:
                ready_job = find_ready_job()

                if ready_job:
                    job_id = ready_job["job_id"]
                    # Spawn job
                    task = asyncio.create_task(run_job(job_id))
                    running_jobs[job_id] = task
                    logger.info(f"Spawned job {job_id[:8]} (sequential mode)")

            # Check completed pipelines
            running_pipelines = db.conn.execute("""
                SELECT pipeline_id FROM pipelines WHERE status = 'running'
            """).fetchall()

            for row in running_pipelines:
                pipeline_id = row["pipeline_id"]
                check_pipeline_completion(pipeline_id)

        except Exception as e:
            logger.error(f"Orchestration error: {e}")

        await asyncio.sleep(5)  # Poll every 5 seconds


async def scheduler_loop():
    """Background task that fires scheduled pipelines every 60 seconds."""
    logger.info("Scheduler loop started")
    while True:
        try:
            await tick_scheduler()
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
        await asyncio.sleep(60)


async def tick_scheduler():
    """Check for due schedules and fire them. Extracted for testability."""
    now_iso = db._timestamp()
    due = scheduler_service.get_due_schedules(now_iso)
    for schedule in due:
        try:
            pipeline_id = template_manager.instantiate_template(
                template_id=schedule["template_id"],
                original_prompt=schedule["prompt"],
                workspace_path=schedule["workspace_path"],
            )
            scheduler_service.record_fired(
                schedule_id=schedule["schedule_id"],
                pipeline_id=pipeline_id,
                fired_at_iso=now_iso,
            )
            logger.info(
                f"Scheduler fired schedule {schedule['schedule_id'][:8]} "
                f"-> pipeline {pipeline_id[:8]}"
            )
        except Exception as e:
            logger.error(
                f"Scheduler failed for schedule {schedule['schedule_id'][:8]}: {e}"
            )


async def run_job(job_id: str):
    """Execute a single job via harness subprocess or custom command."""
    try:
        # Get job details including retry info, artifact strategy, and retry strategy
        job = db.conn.execute(
            """
            SELECT command, retry_count, max_retries, artifact_strategy, retry_strategy,
                   pipeline_id, prompt, original_prompt, job_output, vendor, model
            FROM jobs WHERE job_id = ?
        """,
            (job_id,),
        ).fetchone()

        retry_count = job["retry_count"] if job and job["retry_count"] else 0
        max_retries = (
            job["max_retries"] if job and job["max_retries"] is not None else 100
        )
        artifact_strategy_config = (
            json.loads(job["artifact_strategy"])
            if job and job["artifact_strategy"]
            else None
        )
        retry_strategy_config = (
            json.loads(job["retry_strategy"]) if job and job["retry_strategy"] else None
        )

        # Handle retry with context: if this is a retry and retry_strategy says include_context
        if (
            retry_count > 0
            and retry_strategy_config
            and retry_strategy_config.get("include_context")
        ):
            previous_output = job["job_output"] if job and job["job_output"] else ""
            if previous_output:
                # Get the continuation instruction from retry strategy or use default
                context_instruction = retry_strategy_config.get(
                    "context_instruction",
                    "IMPORTANT: This is a retry. Previous attempt output is below. Continue from where you left off.\n\n",
                )

                # Build augmented prompt from ORIGINAL prompt (not the already-augmented one)
                original_prompt = (
                    job["original_prompt"]
                    if job and job["original_prompt"]
                    else job["prompt"]
                )
                augmented_prompt = f"{context_instruction}=== PREVIOUS ATTEMPT OUTPUT ===\n{previous_output}\n\n=== ORIGINAL TASK ===\n{original_prompt}"

                # Update the prompt in the database for this run
                db.conn.execute(
                    """
                    UPDATE jobs SET prompt = ? WHERE job_id = ?
                """,
                    (augmented_prompt, job_id),
                )
                db.conn.commit()
                logger.info(
                    f"Job {job_id[:8]} retry with previous context ({len(previous_output)} chars)"
                )

        # Update status to running
        db.conn.execute(
            """
            UPDATE jobs SET status = 'running', started_at = ?, updated_at = ?
            WHERE job_id = ?
        """,
            (db._timestamp(), db._timestamp(), job_id),
        )
        db.conn.commit()

        # Use custom command if specified, otherwise route by vendor
        if job and job["command"]:
            cmd = job["command"]
        elif job and job["vendor"] == "mock":
            cmd = "python harnesses/mock_model.py"
        else:
            cmd = f"python harnesses/harness.py {job_id}"

        logger.info(
            f"Running job {job_id[:8]}: {cmd} (attempt {retry_count + 1}/{max_retries + 1})"
        )

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=10 * 1024 * 1024,  # 10 MB — handles thinking-model long lines
        )

        # Build log prefix from any prior output (preserves previous attempt logs)
        existing_log = job["job_output"] or ""
        log_prefix = (
            (existing_log + f"\n--- Attempt {retry_count + 1} ---\n")
            if existing_log
            else ""
        )
        log_output_lines: list[str] = []
        last_flush_time = time.monotonic()

        def _full_log() -> str:
            return log_prefix + "\n".join(log_output_lines)

        def _flush():
            nonlocal last_flush_time
            db.conn.execute(
                "UPDATE jobs SET job_output = ?, updated_at = ? WHERE job_id = ?",
                (_full_log(), db._timestamp(), job_id),
            )
            db.conn.commit()
            last_flush_time = time.monotonic()

        # Stream output line by line to DEBUG log
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except asyncio.LimitOverrunError as exc:
                # A single line exceeded the buffer limit (e.g. reasoning model
                # output without newlines).  Drain the buffered chunk, log a
                # marker, and continue reading so the job doesn't crash.
                logger.warning(
                    f"[{job_id[:8]}] Line too long ({exc.consumed} bytes); "
                    "draining and skipping"
                )
                await proc.stdout.read(exc.consumed)
                log_output_lines.append("[...line truncated: exceeded buffer limit...]")
                continue
            if not line:
                break
            line_str = clean_job_output(line.decode(errors="replace")).rstrip()
            if not line_str:
                continue  # drop lines that are pure terminal control sequences
            log_output_lines.append(line_str)
            logger.debug(f"[{job_id[:8]}] {line_str}")
            if (
                len(log_output_lines) % LOG_FLUSH_EVERY == 0
                or (time.monotonic() - last_flush_time) >= 1.0
            ):
                _flush()

        await proc.wait()

        if proc.returncode == 0:
            status = "completed"
            reason = "success"
        else:
            # Job failed - check if we should retry
            if retry_count < max_retries:
                # Retry the job — preserve accumulated log so live streaming works
                db.conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'pending', retry_count = ?, updated_at = ?, job_output = ?
                    WHERE job_id = ?
                """,
                    (retry_count + 1, db._timestamp(), _full_log(), job_id),
                )
                db.conn.commit()
                logger.warning(
                    f"Job {job_id[:8]} failed with exit code {proc.returncode}, retrying ({retry_count + 1}/{max_retries})"
                )
                return  # Exit early, job will be picked up again by orchestration loop
            else:
                # Max retries exhausted
                status = "failed"
                reason = f"exit_code_{proc.returncode}_after_{retry_count + 1}_attempts"
                logger.error(
                    f"Job {job_id[:8]} failed permanently after {retry_count + 1} attempts"
                )

        # Update job status and store output
        db.conn.execute(
            """
            UPDATE jobs
            SET status = ?, completed_at = ?, updated_at = ?, termination_reason = ?, job_output = ?
            WHERE job_id = ?
        """,
            (status, db._timestamp(), db._timestamp(), reason, _full_log(), job_id),
        )
        db.conn.commit()

        logger.info(f"Job {job_id[:8]} {status}")

        # Collect artifacts if job succeeded
        if status == "completed" and artifact_strategy_config:
            try:
                strategy = get_strategy(artifact_strategy_config)
                # For now, use current directory as job_dir (TODO: use actual workspace)
                job_dir = Path.cwd()
                artifacts = strategy.collect_artifacts(
                    job_id=job_id,
                    job_dir=job_dir,
                    final_output=_full_log(),
                    db_conn=db.conn,
                )
                if artifacts:
                    logger.info(
                        f"Job {job_id[:8]} collected {len(artifacts)} artifact(s)"
                    )
            except Exception as e:
                logger.error(f"Job {job_id[:8]} artifact collection failed: {e}")

        # Check if this job should spawn multiplied child jobs
        if status == "completed":
            try:
                spawned_count = check_and_spawn_multiplied_jobs(
                    db_conn=db.conn, completed_job_id=job_id, timestamp_fn=db._timestamp
                )
                if spawned_count > 0:
                    logger.info(
                        f"Job {job_id[:8]} spawned {spawned_count} child job(s) via multiplier"
                    )
            except Exception as e:
                logger.error(f"Job {job_id[:8]} multiplier spawn failed: {e}")

        # If job failed permanently, propagate failure to dependent jobs
        if status == "failed":
            propagate_job_failure(job_id)

    except Exception as e:
        logger.error(f"Job {job_id[:8]} error: {e}")
        db.conn.execute(
            """
            UPDATE jobs SET status = 'failed', updated_at = ?, termination_reason = ?
            WHERE job_id = ?
        """,
            (db._timestamp(), str(e), job_id),
        )
        db.conn.commit()
        propagate_job_failure(job_id)

    finally:
        if job_id in running_jobs:
            del running_jobs[job_id]


def propagate_job_failure(failed_job_id: str, upstream_was_skipped: bool = False):
    """
    Mark dependent jobs as skipped when their dependency fails or is skipped.
    - If upstream failed (actually ran): only skip 'success' deps; leave 'failure'/'completed'/'always'
      deps pending so they can still fire.
    - If upstream was skipped (never ran): skip ALL pending deps regardless of type, because there
      is no output to work with.
    """
    # Find all jobs that depend on this failed/skipped job
    dependent_jobs = db.conn.execute(
        """
        SELECT jd.job_id, j.agent_type, jd.dependency_type
        FROM job_dependencies jd
        JOIN jobs j ON jd.job_id = j.job_id
        WHERE jd.depends_on_job_id = ?
          AND j.status = 'pending'
    """,
        (failed_job_id,),
    ).fetchall()

    for dep in dependent_jobs:
        dep_job_id = dep["job_id"]
        dep_type = dep["dependency_type"]

        # Skip if: dep type is 'success' (upstream failed), OR upstream was itself skipped
        # (meaning nothing ran, so 'completed'/'always' semantics don't apply)
        if dep_type == "success" or upstream_was_skipped:
            db.conn.execute(
                """
                UPDATE jobs
                SET status = 'skipped',
                    completed_at = ?,
                    updated_at = ?,
                    termination_reason = 'dependency_failed'
                WHERE job_id = ?
            """,
                (db._timestamp(), db._timestamp(), dep_job_id),
            )
            logger.info(
                f"Skipped job {dep_job_id[:8]} ({dep['agent_type']}) due to failed dependency"
            )

            # Recursively propagate — this job is now skipped, so cascade further
            propagate_job_failure(dep_job_id, upstream_was_skipped=True)

    if dependent_jobs:
        db.conn.commit()


def check_pipeline_completion(pipeline_id: str):
    """Check if pipeline is complete and update status."""
    row = db.conn.execute(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status IN ('completed', 'failed', 'skipped') THEN 1 ELSE 0 END) as done,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) as skipped,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending
        FROM jobs WHERE pipeline_id = ?
    """,
        (pipeline_id,),
    ).fetchone()

    # Check if pipeline is complete (all jobs done, failed, or skipped)
    if row["total"] == row["done"]:
        status = "failed" if row["failed"] > 0 else "completed"
        db.conn.execute(
            """
            UPDATE pipelines
            SET status = ?, completed_at = ?, updated_at = ?
            WHERE pipeline_id = ?
        """,
            (status, db._timestamp(), db._timestamp(), pipeline_id),
        )
        db.conn.commit()
        logger.info(
            f"Pipeline {pipeline_id[:8]} {status} ({row['failed']} failed, {row['skipped']} skipped)"
        )

    # Detect deadlock: pending jobs that will never run
    elif row["pending"] > 0:
        # Check if any pending jobs have no path to completion
        # A job is deadlocked only if ALL its dependencies are in terminal states that block it
        deadlocked = db.conn.execute(
            """
            SELECT COUNT(*) as count
            FROM jobs j
            WHERE j.pipeline_id = ?
              AND j.status = 'pending'
              AND NOT EXISTS (
                  -- Check if this job has ANY dependency that could allow it to run
                  -- Job can run if it has at least one dependency that is:
                  -- 1. Completed (for success type)
                  -- 2. Running or pending (still in progress, job should wait)
                  -- 3. Failed (for failure type)
                  -- 4. Always type (runs regardless)
                  SELECT 1
                  FROM job_dependencies jd
                  JOIN jobs dep ON jd.depends_on_job_id = dep.job_id
                  WHERE jd.job_id = j.job_id
                    AND (dep.status IN ('running', 'pending')  -- In progress, not deadlocked
                         OR (jd.dependency_type = 'success' AND dep.status = 'completed')
                         OR (jd.dependency_type = 'failure' AND dep.status = 'failed')
                         OR (jd.dependency_type IN ('completed', 'always')
                             AND dep.status IN ('completed', 'failed', 'skipped')))
              )
              AND EXISTS (
                  -- Has at least one dependency
                  SELECT 1 FROM job_dependencies WHERE job_id = j.job_id
              )
        """,
            (pipeline_id,),
        ).fetchone()

        if deadlocked and deadlocked["count"] > 0:
            logger.warning(
                f"Pipeline {pipeline_id[:8]} has {deadlocked['count']} deadlocked jobs, marking as failed"
            )
            db.conn.execute(
                """
                UPDATE pipelines
                SET status = 'failed', completed_at = ?, updated_at = ?
                WHERE pipeline_id = ?
            """,
                (db._timestamp(), db._timestamp(), pipeline_id),
            )

            # Mark all pending jobs as skipped
            db.conn.execute(
                """
                UPDATE jobs
                SET status = 'skipped',
                    completed_at = ?,
                    updated_at = ?,
                    termination_reason = 'pipeline_deadlocked'
                WHERE pipeline_id = ? AND status = 'pending'
            """,
                (db._timestamp(), db._timestamp(), pipeline_id),
            )

            db.conn.commit()


# Pydantic models removed - using database records instead


@app.get("/pipelines/templates")
async def list_pipeline_templates():
    """List available pipeline templates."""
    return pipeline_service.list_templates()


@app.get("/pipelines/templates/{template_id}")
async def get_template_details(template_id: str):
    """Get full template details including stages, jobs, and dependencies."""
    template = pipeline_service.get_template_details(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@app.get("/pipelines/templates/{template_id}/as-spec")
async def get_template_as_spec(template_id: str):
    """Return template expanded as a RunPipelineRequest-compatible spec.
    The caller fills in prompt and workspace_path, then posts to /pipelines/run."""
    spec = template_manager.template_to_spec(template_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return spec


# ── Template CRUD ──────────────────────────────────────────────────────────────


class TemplateJobSpec(BaseModel):
    ref: Optional[str] = None
    template_job_id: Optional[str] = None
    agent_type: str = "dev"
    name: str = ""
    prompt_template: str = ""
    command_template: Optional[str] = None
    max_iterations: int = 50
    timeout_seconds: int = 300
    vendor: Optional[str] = None
    model: Optional[str] = None
    artifact_strategy: Optional[dict] = None
    retry_strategy: Optional[dict] = None


class TemplateStageSpec(BaseModel):
    template_stage_id: Optional[str] = None
    name: str
    stage_order: int
    jobs: List[TemplateJobSpec] = []


class TemplateDependencySpec(BaseModel):
    from_ref: str
    to_ref: str
    type: str = "success"


class CreateTemplateRequest(BaseModel):
    template_id: Optional[str] = None
    name: str
    description: str = ""
    category: Optional[str] = None
    stages: List[TemplateStageSpec] = []
    dependencies: List[TemplateDependencySpec] = []


class UpdateTemplateMetadataRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    default_vendor: Optional[str] = None
    default_model: Optional[str] = None


class UpdateTemplateStructureRequest(BaseModel):
    stages: List[TemplateStageSpec]
    dependencies: List[TemplateDependencySpec] = []


@app.post("/pipelines/templates", status_code=201)
async def create_template(request: CreateTemplateRequest):
    """Create a new pipeline template."""
    spec = request.model_dump()
    try:
        return pipeline_service.create_template(spec)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.patch("/pipelines/templates/{template_id}")
async def update_template_metadata(
    template_id: str, request: UpdateTemplateMetadataRequest
):
    """Update template metadata (name, description, category, vendor, model)."""
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    result = pipeline_service.update_template_metadata(template_id, updates)
    if result is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return result


@app.patch("/pipelines/templates/{template_id}/structure")
async def update_template_structure(
    template_id: str, request: UpdateTemplateStructureRequest
):
    """Replace template stages, jobs, and dependencies."""
    stages = [s.model_dump() for s in request.stages]
    deps = [d.model_dump() for d in request.dependencies]
    result = pipeline_service.update_template_structure(template_id, stages, deps)
    if result is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return result


@app.delete("/pipelines/templates/{template_id}", status_code=204)
async def delete_template(template_id: str):
    """Delete a template. Returns 409 if schedules reference it."""
    try:
        found = pipeline_service.delete_template(template_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not found:
        raise HTTPException(status_code=404, detail="Template not found")


class JobSpec(BaseModel):
    ref: str
    agent_type: str
    name: str = ""
    chain_id: Optional[str] = None
    vendor: str = "local-ollama"
    model: Optional[str] = None
    prompt_template: Optional[str] = "{{original_prompt}}"
    command_template: Optional[str] = None
    max_iterations: int = 15
    timeout_seconds: int = 300
    artifact_strategy: Optional[dict] = None
    retry_strategy: Optional[dict] = None


class StageSpec(BaseModel):
    name: str
    stage_order: int
    jobs: List[JobSpec]


class DependencySpec(BaseModel):
    from_ref: str
    to_ref: str
    type: str = "success"


class RunPipelineRequest(BaseModel):
    prompt: str
    workspace_path: str = "/workspace"
    stages: List[StageSpec]
    dependencies: List[DependencySpec] = []


@app.post("/pipelines/run")
async def run_pipeline(request: RunPipelineRequest):
    """Start a pipeline from an inline spec (no stored template required)."""
    spec = {
        "stages": [
            {
                "name": stage.name,
                "stage_order": stage.stage_order,
                "jobs": [
                    {
                        "ref": job.ref,
                        "agent_type": job.agent_type,
                        "name": job.name,
                        "chain_id": job.chain_id,
                        "vendor": job.vendor,
                        "model": job.model,
                        "prompt_template": job.prompt_template,
                        "command_template": job.command_template,
                        "max_iterations": job.max_iterations,
                        "timeout_seconds": job.timeout_seconds,
                        "artifact_strategy": job.artifact_strategy,
                        "retry_strategy": job.retry_strategy,
                    }
                    for job in stage.jobs
                ],
            }
            for stage in request.stages
        ],
        "dependencies": [
            {"from_ref": dep.from_ref, "to_ref": dep.to_ref, "type": dep.type}
            for dep in request.dependencies
        ],
    }
    pipeline_id = template_manager.instantiate_from_spec(
        spec=spec,
        original_prompt=request.prompt,
        workspace_path=request.workspace_path,
    )
    logger.info(f"Started pipeline {pipeline_id} from inline spec")
    return {"pipeline_id": pipeline_id}


@app.post("/pipelines/{pipeline_id}/stop")
async def stop_pipeline(pipeline_id: str):
    """Stop a running pipeline."""
    result = pipeline_service.stop_pipeline(pipeline_id)
    logger.info(f"Stopped pipeline {pipeline_id}")
    return result


@app.delete("/pipelines/{pipeline_id}", status_code=204)
async def delete_pipeline(pipeline_id: str):
    """Permanently delete a pipeline and all associated data from the database."""
    found = pipeline_service.delete_pipeline(pipeline_id)
    if not found:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    logger.info(f"Deleted pipeline {pipeline_id}")


@app.get("/ui")
async def ui_redirect():
    return RedirectResponse(url="/static/index.html")


@app.get("/pipelines/jobs/{job_id}/log/since")
async def job_log_since(job_id: str, line: int = 0):
    result = pipeline_service.get_job_log(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Job not found")
    lines = result["output"].split("\n") if result["output"] else []
    return {"lines": lines[line:], "total": len(lines), "live": result["is_live"]}


@app.get("/pipelines/jobs/{job_id}/log/full")
async def job_log_full(job_id: str):
    result = pipeline_service.get_job_log(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return PlainTextResponse(result["output"] or "")


@app.get("/pipelines/running")
async def list_running_pipelines():
    """List currently running pipelines with nested stages and jobs."""
    return pipeline_service.get_running_pipelines()


@app.get("/pipelines/recent")
async def list_recent_pipelines(limit: int = 10):
    """List recently completed/failed pipelines with nested stages and jobs."""
    return pipeline_service.get_recent_pipelines(limit=limit)


@app.get("/pipelines/{pipeline_id}")
async def get_pipeline(pipeline_id: str):
    """Get a single pipeline by its ID."""
    result = pipeline_service.get_pipeline(pipeline_id)
    if not result:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    return result


# ── Schedule endpoints ─────────────────────────────────────────────────────────


class CreateScheduleRequest(BaseModel):
    template_id: str
    name: str
    cron_expr: str
    prompt: str
    workspace_path: str = "/workspace"
    enabled: bool = True


class UpdateScheduleRequest(BaseModel):
    name: Optional[str] = None
    cron_expr: Optional[str] = None
    enabled: Optional[bool] = None
    prompt: Optional[str] = None
    workspace_path: Optional[str] = None


@app.get("/schedules")
async def list_schedules():
    """List all pipeline schedules."""
    return scheduler_service.list_schedules()


@app.post("/schedules")
async def create_schedule(request: CreateScheduleRequest):
    """Create a new pipeline schedule."""
    try:
        return scheduler_service.create_schedule(
            template_id=request.template_id,
            name=request.name,
            cron_expr=request.cron_expr,
            prompt=request.prompt,
            workspace_path=request.workspace_path,
            enabled=request.enabled,
        )
    except ValueError as e:
        msg = str(e)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)


@app.get("/schedules/{schedule_id}")
async def get_schedule(schedule_id: str):
    """Get a single schedule."""
    schedule = scheduler_service.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule


@app.patch("/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, request: UpdateScheduleRequest):
    """Partially update a schedule."""
    if not scheduler_service.get_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    fields = {k: v for k, v in request.model_dump().items() if v is not None}
    try:
        return scheduler_service.update_schedule(schedule_id, **fields)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: str):
    """Delete a schedule."""
    if not scheduler_service.get_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    scheduler_service.delete_schedule(schedule_id)


@app.post("/schedules/{schedule_id}/enable")
async def enable_schedule(schedule_id: str):
    """Enable a schedule."""
    if not scheduler_service.get_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return scheduler_service.enable_schedule(schedule_id)


@app.post("/schedules/{schedule_id}/disable")
async def disable_schedule(schedule_id: str):
    """Disable a schedule."""
    if not scheduler_service.get_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return scheduler_service.disable_schedule(schedule_id)


@app.get("/schedules/{schedule_id}/pipelines")
async def get_schedule_pipelines(schedule_id: str):
    """List all pipeline instances spawned by a schedule."""
    if not scheduler_service.get_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return scheduler_service.get_schedule_pipelines(schedule_id)


@app.get("/")
async def read_root():
    return {"message": "Clowder Server is running!"}


@app.get("/ping")
async def ping():
    """Minimal endpoint for latency testing."""
    return {"pong": True}


if __name__ == "__main__":
    import uvicorn
    import argparse

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Clowder Server")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["TRACE", "MODEL", "DEBUG", "INFO", "WARNING", "ERROR"],
        help="Minimum log level to display (default: INFO)",
    )
    args = parser.parse_args()

    # Convert log level string to number
    log_level_map = {
        "TRACE": TRACE,
        "MODEL": MODEL,
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    min_log_level = log_level_map[args.log_level]

    # Configure logging: colored console (no ms) + plain file (with ms)
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "console": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(asctime)s - %(levelprefix)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",  # No milliseconds
                "use_colors": True,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(asctime)s - %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                "datefmt": "%Y-%m-%d %H:%M:%S",  # No milliseconds
                "use_colors": True,
            },
            "file": {
                "format": "%(asctime)s - %(levelname)s - %(message)s",
                # No datefmt = default format with milliseconds (YYYY-MM-DD HH:MM:SS,mmm)
            },
        },
        "handlers": {
            "console": {
                "formatter": "console",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "formatter": "file",
                "class": "server.main.FlushingFileHandler",
                "filename": "server.log",
                "mode": "w",
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False,
            },
            # Disable uvicorn's built-in access logs (we use our own middleware at TRACE level)
            "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False},
            # Our custom logger - uses the specified log level
            "__main__": {
                "handlers": ["console", "file"],
                "level": min_log_level,
                "propagate": False,
            },
        },
    }

    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=log_config)
