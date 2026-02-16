"""
Test/demo script for SQLite database.
Run: python agents/test_db.py
"""

import uuid
from pathlib import Path
from db import ClowderDB, Task, AgentState


def main():
    # Initialize database
    db_path = Path("test_clowder.db")
    if db_path.exists():
        db_path.unlink()  # Clean slate

    print("Initializing database...")
    db = ClowderDB(str(db_path))
    db.init_schema()
    print(f"[OK] Created {db_path}")

    # Create a task
    task_id = str(uuid.uuid4())
    task = Task(
        task_id=task_id,
        prompt="Read file.txt and uppercase it",
        agent_type="dev",
        max_iterations=50,
        timeout_seconds=300,
        allowed_paths=["/workspace"],
        created_at="2026-02-11T00:00:00Z",
        metadata={"source": "cli"},
    )

    print(f"\nCreating task {task_id[:8]}...")
    db.create_task(task)
    print("[OK] Task created")

    # Update state to running
    state = db.get_state(task_id)
    state.status = "running"
    state.started_at = "2026-02-11T00:00:01Z"
    state.iteration = 1
    db.update_state(state)
    print("[OK] State updated to 'running'")

    # Log some actions
    print("\nLogging actions...")
    db.log_action(
        task_id=task_id,
        iteration=1,
        llm_response={
            "reasoning": "Need to read the file first",
            "actions": [{"tool": "read_file", "args": {"path": "file.txt"}}]
        },
        results=[
            {"tool": "read_file", "status": "success", "result": "hello world"}
        ],
        raw_stdout='{"actions":[...]}',
        raw_stderr="",
    )
    print("  Iteration 1: read_file -> success")

    db.log_action(
        task_id=task_id,
        iteration=2,
        llm_response={
            "actions": [
                {"tool": "transform_text", "args": {"text": "hello world", "operation": "uppercase"}},
                {"tool": "write_file", "args": {"path": "output.txt", "content": "{{result}}"}}
            ]
        },
        results=[
            {"tool": "transform_text", "status": "success", "result": "HELLO WORLD"},
            {"tool": "write_file", "status": "success", "result": True}
        ],
        raw_stdout='{"actions":[...]}',
        raw_stderr="",
    )
    print("  Iteration 2: transform_text, write_file -> success")

    # Query examples
    print("\n" + "="*60)
    print("QUERY EXAMPLES")
    print("="*60)

    # Get task summary
    summary = db.get_task_summary(task_id)
    print(f"\nTask Summary:")
    print(f"  Status: {summary['status']}")
    print(f"  Iteration: {summary['iteration']}/{summary['max_iterations']}")
    print(f"  Total actions: {summary['total_actions']}")

    # Get action history
    history = db.get_action_history(task_id)
    print(f"\nAction History ({len(history)} iterations):")
    for entry in history:
        actions = entry['llm_response'].get('actions', [])
        tools = [a['tool'] for a in actions if isinstance(a, dict)]
        print(f"  Iteration {entry['iteration']}: {', '.join(tools)}")

    # Get active tasks
    active = db.get_active_tasks()
    print(f"\nActive Tasks: {len(active)}")
    for t in active:
        print(f"  {t['task_id'][:8]}: {t['prompt'][:50]}...")

    # Complete the task
    state = db.get_state(task_id)
    state.status = "completed"
    state.termination_reason = "task_complete"
    db.update_state(state)
    print(f"\n[OK] Task marked as completed")

    db.close()
    print(f"\n[OK] Database closed")
    print(f"\nInspect with: sqlite3 {db_path}")
    print(f"  Example: sqlite3 {db_path} 'SELECT * FROM task_summary'")


if __name__ == "__main__":
    main()
