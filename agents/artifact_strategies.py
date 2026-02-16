"""
Artifact collection strategies.

Each strategy defines how Clowder (not the model) determines what artifacts
a job produced. The model has no control over artifact registration.
"""

import json
import subprocess
import uuid
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone


def timestamp():
    """Get ISO8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


class ArtifactStrategy:
    """Base class for artifact collection strategies."""

    def collect_artifacts(
        self,
        job_id: str,
        job_dir: Path,
        final_output: Optional[str] = None,
        db_conn=None
    ) -> list[dict]:
        """
        Collect artifacts produced by a job.

        Args:
            job_id: Job ID
            job_dir: Job working directory
            final_output: Final model output (if applicable)
            db_conn: Database connection for inserting artifacts

        Returns:
            List of artifact dicts that were created
        """
        raise NotImplementedError


class StdoutFinalStrategy(ArtifactStrategy):
    """
    Captures the final model output as an artifact.

    Perfect for planner, reviewer, verifier jobs that produce text outputs
    rather than files.
    """

    def collect_artifacts(self, job_id: str, job_dir: Path, final_output: Optional[str] = None, db_conn=None) -> list[dict]:
        if not final_output:
            return []

        artifact_id = str(uuid.uuid4())
        artifact = {
            "artifact_id": artifact_id,
            "job_id": job_id,
            "type": "model_output",
            "name": "final_output.txt",
            "description": "Final model output before job completion",
            "file_path": None,
            "content": final_output,
            "content_hash": None,  # Could add SHA256 if needed
            "size_bytes": len(final_output.encode('utf-8')),
            "metadata": json.dumps({"strategy": "stdout_final"}),
            "created_at": timestamp()
        }

        if db_conn:
            db_conn.execute("""
                INSERT INTO artifacts (
                    artifact_id, job_id, type, name, description,
                    file_path, content, content_hash, size_bytes, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                artifact["artifact_id"],
                artifact["job_id"],
                artifact["type"],
                artifact["name"],
                artifact["description"],
                artifact["file_path"],
                artifact["content"],
                artifact["content_hash"],
                artifact["size_bytes"],
                artifact["metadata"],
                artifact["created_at"]
            ))
            db_conn.commit()

        return [artifact]


class GitDiffStrategy(ArtifactStrategy):
    """
    Uses git to detect all changed/new files as artifacts.

    Bulletproof - captures everything the job actually modified.
    Requires the workspace to be a git repo with a clean tree at job start.
    """

    def __init__(self, pre_commit: bool = True):
        """
        Args:
            pre_commit: If True, create a commit before job starts (recommended)
        """
        self.pre_commit = pre_commit

    def collect_artifacts(self, job_id: str, job_dir: Path, final_output: Optional[str] = None, db_conn=None) -> list[dict]:
        artifacts = []

        try:
            # Get changed files
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=job_dir,
                capture_output=True,
                text=True,
                check=False
            )
            changed_files = result.stdout.strip().split('\n') if result.stdout.strip() else []

            # Get new untracked files
            result = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=job_dir,
                capture_output=True,
                text=True,
                check=False
            )
            new_files = result.stdout.strip().split('\n') if result.stdout.strip() else []

            all_files = set(changed_files + new_files)
            all_files.discard('')  # Remove empty strings

            for file_path in all_files:
                full_path = job_dir / file_path
                if not full_path.exists():
                    continue

                artifact_id = str(uuid.uuid4())
                artifact = {
                    "artifact_id": artifact_id,
                    "job_id": job_id,
                    "type": "file",
                    "name": file_path,
                    "description": f"File modified/created by job",
                    "file_path": str(full_path.absolute()),
                    "content": None,
                    "content_hash": None,
                    "size_bytes": full_path.stat().st_size if full_path.is_file() else 0,
                    "metadata": json.dumps({"strategy": "git_diff", "relative_path": file_path}),
                    "created_at": timestamp()
                }

                if db_conn:
                    db_conn.execute("""
                        INSERT INTO artifacts (
                            artifact_id, job_id, type, name, description,
                            file_path, content, content_hash, size_bytes, metadata, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        artifact["artifact_id"],
                        artifact["job_id"],
                        artifact["type"],
                        artifact["name"],
                        artifact["description"],
                        artifact["file_path"],
                        artifact["content"],
                        artifact["content_hash"],
                        artifact["size_bytes"],
                        artifact["metadata"],
                        artifact["created_at"]
                    ))

                artifacts.append(artifact)

            if db_conn and artifacts:
                db_conn.commit()

        except Exception as e:
            print(f"Error collecting git_diff artifacts: {e}")

        return artifacts


class CompositeStrategy(ArtifactStrategy):
    """
    Combines multiple strategies.

    Example: Use both git_diff (for files) and stdout_final (for model output)
    """

    def __init__(self, strategies: list[ArtifactStrategy]):
        self.strategies = strategies

    def collect_artifacts(self, job_id: str, job_dir: Path, final_output: Optional[str] = None, db_conn=None) -> list[dict]:
        all_artifacts = []
        for strategy in self.strategies:
            artifacts = strategy.collect_artifacts(job_id, job_dir, final_output, db_conn)
            all_artifacts.extend(artifacts)
        return all_artifacts


def get_strategy(strategy_config: dict) -> ArtifactStrategy:
    """
    Factory function to create strategy from config.

    Args:
        strategy_config: Dict like {"type": "stdout_final"} or {"type": "git_diff"}

    Returns:
        ArtifactStrategy instance
    """
    if not strategy_config:
        return StdoutFinalStrategy()  # Default

    strategy_type = strategy_config.get("type", "stdout_final")

    if strategy_type == "stdout_final":
        return StdoutFinalStrategy()
    elif strategy_type == "git_diff":
        return GitDiffStrategy()
    elif strategy_type == "composite":
        sub_strategies = [
            get_strategy(s) for s in strategy_config.get("strategies", [])
        ]
        return CompositeStrategy(sub_strategies)
    else:
        # Unknown strategy, default to stdout_final
        return StdoutFinalStrategy()
