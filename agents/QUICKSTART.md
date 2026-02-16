# Quick Start Guide

## Setup (One Time)

### 1. Install WSL

If you don't have WSL installed:

```powershell
wsl --install
```

Restart your computer if needed.

### 2. Install Ollama in WSL

```bash
# Open WSL terminal
wsl

# Install Ollama
curl https://ollama.ai/install.sh | sh

# Pull the model (this will take a few minutes)
ollama pull qwen2.5-coder:7b
```

### 3. Verify Setup

```bash
# From Windows (PowerShell or CMD)
cd agents
python test_wsl_connection.py
```

This will test:
- ✓ WSL is available
- ✓ Ollama is installed
- ✓ Model is downloaded
- ✓ Inference works

### 4. Test JSON Output (Optional)

```bash
python test_json_output.py
```

This verifies the model can produce valid JSON matching the harness schema.

---

## Running Your First Task

### Simple CLI (Recommended)

```bash
# Create a test file
echo "Hello, world!" > test.txt

# Run agent with a prompt (works on current directory)
python harness.py --prompt "Read test.txt, convert to uppercase, write to test_upper.txt"
```

The agent will:
1. Work in your current directory
2. Execute the task using available tools
3. Save all state to `tasks/<task-id>/`

### Inspect Results

```bash
# View the output file
cat test_upper.txt

# View final state
cat tasks/<task-id>/state.json

# View action log (shows all LLM calls and tool executions)
cat tasks/<task-id>/actions.jsonl
```

### Example Using Programmatic API

```bash
python example_task.py
```

This will:
1. Create a workspace with a sample file
2. Run an agent that converts text to uppercase
3. Save all state to `tasks/<task-id>/`

---

## Creating Custom Tasks

### Python API

```python
from pathlib import Path
from state import StateManager, Task, TaskConstraints
from harness import AgentHarness

# Create task
task = Task(
    task_id="my-task-123",
    prompt="Find all Python files and list their sizes",
    constraints=TaskConstraints(
        max_iterations=20,
        timeout_seconds=120,
        allowed_paths=["D:/my-project"],
    ),
    created_at="2025-01-01T00:00:00Z",
)

# Initialize and run
state_manager = StateManager(Path("tasks"))
state_manager.create_task(task)

harness = AgentHarness(Path("tasks"))
result = harness.run(task.task_id)

print(f"Completed: {result}")
```

### CLI

```bash
# First create the task (using Python or another tool)
python -c "from state import *; ..."

# Then run it
python harness.py <task-id>
```

---

## Monitoring Running Tasks

### Check Liveness

```bash
# Heartbeat updates every iteration
cat tasks/<task-id>/heartbeat.json
```

### View Progress

```bash
# Current state
cat tasks/<task-id>/state.json

# Action log (one line per iteration)
tail -f tasks/<task-id>/actions.jsonl
```

### Stop a Task

```bash
# Edit state.json and change status to "stopped"
# The harness will terminate at the next iteration check
```

---

## Understanding Task Output

### Task Directory Structure

```
tasks/<task-id>/
  task.json          # Immutable: original task definition
  state.json         # Mutable: current status, iteration count
  heartbeat.json     # Updated every iteration (liveness check)
  actions.jsonl      # Append-only log of all actions
  artifacts/         # Files created by the agent
```

### state.json

```json
{
  "status": "running",           // or "completed", "stopped", "failed"
  "iteration": 15,
  "started_at": "2025-01-01T10:00:00Z",
  "updated_at": "2025-01-01T10:05:23Z",
  "termination_reason": null     // Set when terminated
}
```

### actions.jsonl

Each line is a complete iteration:

```json
{
  "iteration": 1,
  "timestamp": "2025-01-01T10:00:05Z",
  "llm_response": {
    "reasoning": "I need to read the file first",
    "actions": [
      {"tool": "read_file", "args": {"path": "data.txt"}}
    ]
  },
  "results": [
    {
      "tool": "read_file",
      "args": {"path": "data.txt"},
      "status": "success",
      "result": "Hello, world!\n"
    }
  ]
}
```

---

## Troubleshooting

### "WSL not found"

- Install WSL: `wsl --install`
- Restart your computer
- Verify: `wsl echo test`

### "Ollama not found in WSL"

```bash
wsl
curl https://ollama.ai/install.sh | sh
```

### "Model not found"

```bash
wsl bash -c 'ollama pull qwen2.5-coder:7b'
```

### Model outputs invalid JSON

- Run `python test_json_output.py` to diagnose
- Try a larger model (e.g., `qwen2.5-coder:14b`)
- Check the raw output in `actions.jsonl` to see what the model produced

### Task gets stuck

- Check `heartbeat.json` - is it updating?
- Check `state.json` - what's the current iteration?
- Check `actions.jsonl` - is the agent repeating the same failed action?
- The agent will auto-terminate at `max_iterations`

### Tool execution fails

Common issues:
- **Path outside allowed_paths**: Add the path to `task.constraints.allowed_paths`
- **File not found**: Agent may have wrong path - check the workspace
- **Permission denied**: Ensure the harness has file system permissions

---

## Next Steps

- Read [ORCHESTRATOR_DESIGN.md](ORCHESTRATOR_DESIGN.md) for architecture details
- Read [README.md](README.md) for complete documentation
- Explore the source code (only ~500 lines total)
- Build external supervisor to spawn/monitor multiple tasks
- Integrate with Clowder's pipeline system

---

## Key Concepts

### The LLM is not trusted

- All outputs are validated against strict schema
- All file paths are sandboxed
- AgentHarness decides termination, not the LLM

### State is external

- Everything lives on disk
- AgentHarness is stateless between runs
- Tasks survive crashes and restarts

### Deterministic control loop

- Fixed iteration structure
- No hidden behavior
- Every action is logged

---

## Performance Notes

**Cold start:** First inference after WSL boot may take 10-30 seconds as the model loads into memory.

**Warm inference:** Subsequent calls are typically 1-5 seconds depending on model size and prompt length.

**Memory:** qwen2.5-coder:7b uses ~4-5GB RAM. Ensure WSL has sufficient memory allocation.

To increase WSL memory limit:

```powershell
# Create/edit C:\Users\<username>\.wslconfig
[wsl2]
memory=8GB
```

Then restart WSL: `wsl --shutdown`
