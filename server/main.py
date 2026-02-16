from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import asyncio
import sys
import json
from pathlib import Path
from typing import List, Optional
import uuid
from uuid import UUID
import logging

# Add agents directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))

from db import ClowderDB
from templates import TemplateManager
from server.services import PipelineService
from artifact_strategies import get_strategy
from job_multiplier import check_and_spawn_multiplied_jobs

# Custom TRACE level (below DEBUG)
TRACE = 5
logging.addLevelName(TRACE, "TRACE")


# Custom file handler that flushes after every write
class FlushingFileHandler(logging.FileHandler):
    """FileHandler that flushes after each emit to prevent log loss on crashes."""
    def emit(self, record):
        super().emit(record)
        self.flush()


# Configure logger (actual config happens in main() with uvicorn)
logger = logging.getLogger(__name__)

app = FastAPI()

# Initialize database
db = ClowderDB("clowder.db")
template_manager = TemplateManager(db)
pipeline_service = PipelineService(db, template_manager)

# Middleware to log requests with timing at TRACE level
@app.middleware("http")
async def log_requests(request: Request, call_next):
    import time
    start = time.time()
    logger.log(TRACE, f"Request: {request.method} {request.url}")
    response = await call_next(request)
    duration = (time.time() - start) * 1000  # ms
    logger.log(TRACE, f"Response: {response.status_code} ({duration:.2f}ms)")
    return response

# Old in-memory storage removed - now using database


running_jobs = {}  # job_id -> asyncio.Task


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
                pipeline_id = row['pipeline_id']
                db.conn.execute("""
                    UPDATE pipelines SET status = 'running', updated_at = ?
                    WHERE pipeline_id = ?
                """, (db._timestamp(), pipeline_id))
                db.conn.commit()
                logger.info(f"Started pipeline {pipeline_id[:8]}")

            # Find ready jobs - only run ONE job at a time (crawl before we walk)
            # Only spawn a new job if nothing is currently running
            if not running_jobs:
                ready_job = db.conn.execute("""
                    SELECT j.job_id, j.pipeline_id
                    FROM jobs j
                    WHERE j.status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM job_dependencies jd
                          JOIN jobs dep ON jd.depends_on_job_id = dep.job_id
                          WHERE jd.job_id = j.job_id
                            AND dep.status NOT IN ('completed')
                      )
                    LIMIT 1
                """).fetchone()

                if ready_job:
                    job_id = ready_job['job_id']
                    # Spawn job
                    task = asyncio.create_task(run_job(job_id))
                    running_jobs[job_id] = task
                    logger.info(f"Spawned job {job_id[:8]} (sequential mode)")

            # Check completed pipelines
            running_pipelines = db.conn.execute("""
                SELECT pipeline_id FROM pipelines WHERE status = 'running'
            """).fetchall()

            for row in running_pipelines:
                pipeline_id = row['pipeline_id']
                check_pipeline_completion(pipeline_id)

        except Exception as e:
            logger.error(f"Orchestration error: {e}")

        await asyncio.sleep(5)  # Poll every 5 seconds


async def run_job(job_id: str):
    """Execute a single job via harness subprocess or custom command."""
    try:
        # Get job details including retry info, artifact strategy, and retry strategy
        job = db.conn.execute("""
            SELECT command, retry_count, max_retries, artifact_strategy, retry_strategy,
                   pipeline_id, prompt, original_prompt, job_output
            FROM jobs WHERE job_id = ?
        """, (job_id,)).fetchone()

        retry_count = job['retry_count'] if job and job['retry_count'] else 0
        max_retries = job['max_retries'] if job and job['max_retries'] is not None else 100
        artifact_strategy_config = json.loads(job['artifact_strategy']) if job and job['artifact_strategy'] else None
        retry_strategy_config = json.loads(job['retry_strategy']) if job and job['retry_strategy'] else None

        # Handle retry with context: if this is a retry and retry_strategy says include_context
        if retry_count > 0 and retry_strategy_config and retry_strategy_config.get('include_context'):
            previous_output = job['job_output'] if job and job['job_output'] else ""
            if previous_output:
                # Get the continuation instruction from retry strategy or use default
                context_instruction = retry_strategy_config.get(
                    'context_instruction',
                    "IMPORTANT: This is a retry. Previous attempt output is below. Continue from where you left off.\n\n"
                )

                # Build augmented prompt from ORIGINAL prompt (not the already-augmented one)
                original_prompt = job['original_prompt'] if job and job['original_prompt'] else job['prompt']
                augmented_prompt = f"{context_instruction}=== PREVIOUS ATTEMPT OUTPUT ===\n{previous_output}\n\n=== ORIGINAL TASK ===\n{original_prompt}"

                # Update the prompt in the database for this run
                db.conn.execute("""
                    UPDATE jobs SET prompt = ? WHERE job_id = ?
                """, (augmented_prompt, job_id))
                db.conn.commit()
                logger.info(f"Job {job_id[:8]} retry with previous context ({len(previous_output)} chars)")

        # Update status to running
        db.conn.execute("""
            UPDATE jobs SET status = 'running', started_at = ?, updated_at = ?
            WHERE job_id = ?
        """, (db._timestamp(), db._timestamp(), job_id))
        db.conn.commit()

        # Use custom command if specified, otherwise default to harness
        if job and job['command']:
            cmd = job['command']
        else:
            cmd = f"python agents/harness.py {job_id}"

        logger.info(f"Running job {job_id[:8]}: {cmd} (attempt {retry_count + 1}/{max_retries + 1})")

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Stream output line by line to DEBUG log
        log_output_lines = []
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line_str = line.decode(errors="replace").rstrip()
            log_output_lines.append(line_str)
            logger.debug(f"[{job_id[:8]}] {line_str}")

        await proc.wait()
        log_output = "\n".join(log_output_lines)

        if proc.returncode == 0:
            status = "completed"
            reason = "success"
        else:
            # Job failed - check if we should retry
            if retry_count < max_retries:
                # Retry the job
                db.conn.execute("""
                    UPDATE jobs
                    SET status = 'pending', retry_count = ?, updated_at = ?
                    WHERE job_id = ?
                """, (retry_count + 1, db._timestamp(), job_id))
                db.conn.commit()
                logger.warning(f"Job {job_id[:8]} failed with exit code {proc.returncode}, retrying ({retry_count + 1}/{max_retries})")
                return  # Exit early, job will be picked up again by orchestration loop
            else:
                # Max retries exhausted
                status = "failed"
                reason = f"exit_code_{proc.returncode}_after_{retry_count + 1}_attempts"
                logger.error(f"Job {job_id[:8]} failed permanently after {retry_count + 1} attempts")

        # Update job status and store output
        db.conn.execute("""
            UPDATE jobs
            SET status = ?, completed_at = ?, updated_at = ?, termination_reason = ?, job_output = ?
            WHERE job_id = ?
        """, (status, db._timestamp(), db._timestamp(), reason, log_output, job_id))
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
                    final_output=log_output,
                    db_conn=db.conn
                )
                if artifacts:
                    logger.info(f"Job {job_id[:8]} collected {len(artifacts)} artifact(s)")
            except Exception as e:
                logger.error(f"Job {job_id[:8]} artifact collection failed: {e}")

        # Check if this job should spawn multiplied child jobs
        if status == "completed":
            try:
                spawned_count = check_and_spawn_multiplied_jobs(
                    db_conn=db.conn,
                    completed_job_id=job_id,
                    timestamp_fn=db._timestamp
                )
                if spawned_count > 0:
                    logger.info(f"Job {job_id[:8]} spawned {spawned_count} child job(s) via multiplier")
            except Exception as e:
                logger.error(f"Job {job_id[:8]} multiplier spawn failed: {e}")

        # If job failed permanently, propagate failure to dependent jobs
        if status == "failed":
            propagate_job_failure(job_id)

    except Exception as e:
        logger.error(f"Job {job_id[:8]} error: {e}")
        db.conn.execute("""
            UPDATE jobs SET status = 'failed', updated_at = ?, termination_reason = ?
            WHERE job_id = ?
        """, (db._timestamp(), str(e), job_id))
        db.conn.commit()
        propagate_job_failure(job_id)

    finally:
        if job_id in running_jobs:
            del running_jobs[job_id]


def propagate_job_failure(failed_job_id: str):
    """
    Mark dependent jobs as skipped when their dependency fails.
    This prevents pipelines from getting stuck with pending jobs that can never run.
    """
    # Find all jobs that depend on this failed job
    dependent_jobs = db.conn.execute("""
        SELECT jd.job_id, j.agent_type, jd.dependency_type
        FROM job_dependencies jd
        JOIN jobs j ON jd.job_id = j.job_id
        WHERE jd.depends_on_job_id = ?
          AND j.status = 'pending'
    """, (failed_job_id,)).fetchall()

    for dep in dependent_jobs:
        dep_job_id = dep['job_id']
        dep_type = dep['dependency_type']

        # Only skip if dependency type is 'success' (default)
        # Jobs with 'failure' or 'always' dependency types can still run
        if dep_type == 'success':
            db.conn.execute("""
                UPDATE jobs
                SET status = 'skipped',
                    completed_at = ?,
                    updated_at = ?,
                    termination_reason = 'dependency_failed'
                WHERE job_id = ?
            """, (db._timestamp(), db._timestamp(), dep_job_id))
            logger.info(f"Skipped job {dep_job_id[:8]} ({dep['agent_type']}) due to failed dependency")

            # Recursively propagate to jobs that depend on this one
            propagate_job_failure(dep_job_id)

    if dependent_jobs:
        db.conn.commit()


def check_pipeline_completion(pipeline_id: str):
    """Check if pipeline is complete and update status."""
    row = db.conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status IN ('completed', 'failed', 'skipped') THEN 1 ELSE 0 END) as done,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) as skipped,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending
        FROM jobs WHERE pipeline_id = ?
    """, (pipeline_id,)).fetchone()

    # Check if pipeline is complete (all jobs done, failed, or skipped)
    if row['total'] == row['done']:
        status = 'failed' if row['failed'] > 0 else 'completed'
        db.conn.execute("""
            UPDATE pipelines
            SET status = ?, completed_at = ?, updated_at = ?
            WHERE pipeline_id = ?
        """, (status, db._timestamp(), db._timestamp(), pipeline_id))
        db.conn.commit()
        logger.info(f"Pipeline {pipeline_id[:8]} {status} ({row['failed']} failed, {row['skipped']} skipped)")

    # Detect deadlock: pending jobs that will never run
    elif row['pending'] > 0:
        # Check if any pending jobs have no path to completion
        # A job is deadlocked only if ALL its dependencies are in terminal states that block it
        deadlocked = db.conn.execute("""
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
                         OR jd.dependency_type = 'always')
              )
              AND EXISTS (
                  -- Has at least one dependency
                  SELECT 1 FROM job_dependencies WHERE job_id = j.job_id
              )
        """, (pipeline_id,)).fetchone()

        if deadlocked and deadlocked['count'] > 0:
            logger.warning(f"Pipeline {pipeline_id[:8]} has {deadlocked['count']} deadlocked jobs, marking as failed")
            db.conn.execute("""
                UPDATE pipelines
                SET status = 'failed', completed_at = ?, updated_at = ?
                WHERE pipeline_id = ?
            """, (db._timestamp(), db._timestamp(), pipeline_id))

            # Mark all pending jobs as skipped
            db.conn.execute("""
                UPDATE jobs
                SET status = 'skipped',
                    completed_at = ?,
                    updated_at = ?,
                    termination_reason = 'pipeline_deadlocked'
                WHERE pipeline_id = ? AND status = 'pending'
            """, (db._timestamp(), db._timestamp(), pipeline_id))

            db.conn.commit()

# Pydantic models removed - using database records instead


@app.on_event("startup")
async def startup_event():
    """Initialize database and load seed data."""
    logger.info("Initializing database...")

    # Check if database is already initialized
    tables_exist = db.conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='pipeline_templates'
    """).fetchone()

    # Load schemas only if database is new
    if not tables_exist:
        logger.info("Database is new, creating schema...")
        schema_path = Path(__file__).parent.parent / "agents" / "schema_pipelines.sql"
        if schema_path.exists():
            try:
                schema = schema_path.read_text()
                db.conn.executescript(schema)
                db.conn.commit()
                logger.info("Database schema created")
            except Exception as e:
                logger.error(f"Error creating schema: {e}")
                raise
    else:
        logger.info("Database schema already exists")

    # Load seed templates if database is empty
    templates = template_manager.list_templates()
    if not templates:
        logger.info("No templates found, loading seeds...")
        seed_path = Path(__file__).parent.parent / "agents" / "seed_templates.sql"
        if seed_path.exists():
            try:
                seeds = seed_path.read_text()
                db.conn.executescript(seeds)
                db.conn.commit()
                logger.info("Seed templates loaded")
            except Exception as e:
                logger.error(f"Error loading seeds: {e}")

    # Start orchestration background task
    asyncio.create_task(orchestration_loop())
    logger.info("Orchestration loop started")

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

class StartPipelineRequest(BaseModel):
    prompt: str
    workspace_path: str = "/workspace"

@app.post("/pipelines/{template_id}/start")
async def start_pipeline(template_id: str, request: StartPipelineRequest):
    """Start a new pipeline from a template."""
    try:
        result = pipeline_service.create_pipeline(
            template_id, request.prompt, request.workspace_path
        )
        logger.info(f"Started pipeline {result['pipeline_id']} from template {template_id}")
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/pipelines/{pipeline_id}/stop")
async def stop_pipeline(pipeline_id: str):
    """Stop a running pipeline."""
    result = pipeline_service.stop_pipeline(pipeline_id)
    logger.info(f"Stopped pipeline {pipeline_id}")
    return result


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
        choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
        help="Minimum log level to display (default: INFO)",
    )
    args = parser.parse_args()

    # Convert log level string to number
    log_level_map = {"TRACE": TRACE, "DEBUG": logging.DEBUG, "INFO": logging.INFO,
                     "WARNING": logging.WARNING, "ERROR": logging.ERROR}
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
            "uvicorn": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            # Disable uvicorn's built-in access logs (we use our own middleware at TRACE level)
            "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False},
            # Our custom logger - uses the specified log level
            "__main__": {"handlers": ["console", "file"], "level": min_log_level, "propagate": False},
        },
    }

    print(f"Starting server with log level: {args.log_level}")
    print(f"  - Orchestrator messages: INFO")
    print(f"  - Model streaming: DEBUG")
    print(f"  - HTTP requests: TRACE")
    print(f"  - Showing logs at level: {args.log_level} and above")
    print()

    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=log_config)