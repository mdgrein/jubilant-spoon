# Minimal Agent Harness

A deterministic, file-based LLM agent harness designed for worker nodes in the Clowder system.

**🚀 New user? Start here: [QUICKSTART.md](QUICKSTART.md)**

## Design Principles

1. **The LLM is not trusted** - It's a function that suggests actions
2. **State is external and authoritative** - Everything lives on disk
3. **The harness is deterministic** - Fixed control loop, no hidden behavior
4. **Termination is external** - The LLM never decides when it's done

See [HARNESS_DESIGN.md](HARNESS_DESIGN.md) for full design documentation.

## Architecture

```
harness.py   # Main control loop
├── schema.py     # JSON validation for LLM outputs
├── tools.py      # Sandboxed file operations
└── state.py      # External state management
```

## Quick Start

### Prerequisites

1. **WSL (Windows Subsystem for Linux)** must be installed and configured
2. **Ollama in WSL**: Install Ollama inside WSL and pull the model:
   ```bash
   # Inside WSL
   curl https://ollama.ai/install.sh | sh
   ollama pull qwen2.5-coder:7b
   ```
3. **Python dependencies**: None! Uses only Python stdlib (subprocess)

**Test your setup:**
```bash
# Quick test
wsl bash -c 'ollama run qwen2.5-coder:7b "Hello world!"'

# Comprehensive test
python test_wsl_connection.py
```

### Run Example Task

```bash
python example_task.py
```

This will:
1. Create a workspace with a sample file
2. Create a task that converts text to uppercase
3. Run the harness
4. Save results to `tasks/<task_id>/`

### Task Output Structure

```
tasks/
  <task_id>/
    task.json          # Immutable task definition
    state.json         # Current agent state
    heartbeat.json     # Liveness indicator
    actions.jsonl      # Complete action log
    artifacts/         # Files created by agent
```

## Creating Tasks Programmatically

```python
from state import StateManager, Task, TaskConstraints

task = Task(
    task_id="unique-id",
    prompt="Your task description",
    constraints=TaskConstraints(
        max_iterations=50,
        timeout_seconds=300,
        allowed_paths=["/path/to/workspace"],
    ),
    created_at="2025-01-01T00:00:00Z",
)

state_manager = StateManager(Path("tasks"))
state_manager.create_task(task)
```

## Running the Harness

### Simple CLI Mode (Recommended)

```bash
# Run with a prompt (uses current directory as workspace)
python harness.py --prompt "Find all Python files and count their lines"

# Use custom workspace
python harness.py --prompt "Create a hello.txt file" --workspace /path/to/dir

# Customize constraints
python harness.py --prompt "Complex task" --max-iterations 100 --timeout 600

# Use different model
python harness.py --prompt "Task description" --model codellama:7b
```

### Programmatic API

```python
from harness import AgentHarness

harness = AgentHarness(tasks_dir=Path("tasks"))
termination_reason = harness.run(task_id)
```

### Existing Task Mode

```bash
# Run previously created task by ID
python harness.py <task_id>
```

## Available Tools

The agent can use these sandboxed file operations:

- `read_file(path: str)` - Read file contents
- `write_file(path: str, content: str)` - Write/overwrite file
- `create_file(path: str, content: str)` - Create new file (fails if exists)
- `list_directory(path: str)` - List directory contents
- `find_files(pattern: str, start_path: str, max_depth: int)` - Glob search

All paths are validated against `allowed_paths` in task constraints.

## Termination Conditions

The harness terminates when:

1. **Max iterations reached** - Iteration limit exceeded
2. **Timeout exceeded** - Wall-clock time limit exceeded
3. **External stop signal** - `state.json` status set to "stopped"
4. **Fatal error** - Ollama unavailable, state corruption, etc.

The LLM **never** decides when the task is complete. An external supervisor monitors output and updates state accordingly.

## Monitoring

### Check Liveness

```bash
cat tasks/<task_id>/heartbeat.json
```

### View Current State

```bash
cat tasks/<task_id>/state.json
```

### Inspect Action Log

```bash
cat tasks/<task_id>/actions.jsonl
```

### Stop Running Task

```bash
# Update status field to "stopped"
echo '{"status": "stopped", ...}' > tasks/<task_id>/state.json
```

## Failure Handling

| Failure Mode | Behavior |
|--------------|----------|
| Invalid JSON from LLM | Log error, increment iteration, continue |
| Unknown tool requested | Log error, skip action, continue |
| Tool execution fails | Log error, return error to LLM next iteration |
| Path security violation | Log error, reject action, continue |
| LLM gets stuck looping | Hits iteration limit, terminates |
| Ollama unavailable | Fatal error, terminate immediately |

Non-fatal errors are logged and the loop continues. Fatal errors stop execution.

## Integration with Clowder

This harness is designed as a worker node:

1. Clowder (the orchestrator) creates tasks in `tasks/`
2. Clowder spawns harness processes
3. Harness runs until termination
4. Clowder monitors heartbeat and actions
5. Clowder decides if task is complete
6. Clowder spawns new harness for next task

The harness is stateless between invocations - all context comes from disk.

## Testing

(TODO: Add test suite)

## Configuration

### Change Model

```python
harness = AgentHarness(
    tasks_dir=Path("tasks"),
    model="codellama:7b",  # Use different model (must be available in WSL)
)
```

### Architecture

The harness calls Ollama directly via WSL subprocess:
```bash
wsl bash -c 'ollama run qwen2.5-coder:7b "<prompt>"'
```

**No HTTP/networking required** - Pure subprocess communication.

## Limitations

- **Requires WSL** - Windows-only design (calls `wsl bash -c`)
- Single-threaded (one task at a time)
- No network access (file operations only)
- No streaming (waits for full LLM response)
- No multi-agent coordination (single worker node)
- No task queue (external supervisor handles scheduling)

## License

(TODO: Add license)
