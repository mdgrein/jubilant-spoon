"""
Example: Create and run a simple task.

Usage:
    python example_task.py
"""

import uuid
from pathlib import Path
from state import StateManager, Task, TaskConstraints
from harness import AgentHarness


def main():
    # Setup
    tasks_dir = Path("tasks")
    tasks_dir.mkdir(exist_ok=True)

    # Create workspace for agent
    workspace_dir = tasks_dir / "workspace"
    workspace_dir.mkdir(exist_ok=True)

    # Create a sample file for the agent to work with
    sample_file = workspace_dir / "data.txt"
    sample_file.write_text("Hello, world!\n")

    # Create task
    task_id = str(uuid.uuid4())
    task = Task(
        task_id=task_id,
        prompt=(
            "Read the file data.txt, convert all text to uppercase, "
            "and write the result to data_uppercase.txt"
        ),
        constraints=TaskConstraints(
            max_iterations=10,
            timeout_seconds=60,
            allowed_paths=[str(workspace_dir.resolve())],
        ),
        created_at="2025-01-01T00:00:00Z",
    )

    # Initialize state manager and create task
    state_manager = StateManager(tasks_dir)
    state_manager.create_task(task)

    print(f"Created task: {task_id}")
    print(f"Prompt: {task.prompt}")
    print(f"Workspace: {workspace_dir}")
    print()

    # Run harness
    harness = AgentHarness(tasks_dir)
    termination_reason = harness.run(task_id)

    print()
    print(f"Task completed: {termination_reason}")
    print()
    print("Check the following files for results:")
    print(f"  - {tasks_dir / task_id / 'state.json'}")
    print(f"  - {tasks_dir / task_id / 'actions.jsonl'}")
    print(f"  - {workspace_dir / 'data_uppercase.txt'}")


if __name__ == "__main__":
    main()
