# Minimal Agent Harness Design

## Core Principle

**The LLM is a function: (state, prompt) → structured_actions**

The harness is the deterministic wrapper that:
1. Calls the LLM
2. Validates output
3. Executes actions
4. Updates external state
5. Decides whether to continue

**Clowder is the orchestrator** that coordinates multiple harnesses.

---

## Agent Loop

```
while not terminated:
    1. Read current state from disk
    2. Build context (task, history, constraints)
    3. Call LLM with context
    4. Parse response (strict JSON)
    5. Validate against schema
    6. Execute tool calls
    7. Write state back to disk
    8. Write heartbeat
    9. Check termination conditions
    10. Sleep or exit
```

**Critical:** The loop is controlled by the harness, not the LLM.

---

## LLM Output Schema

The LLM must output valid JSON matching this schema:

```json
{
  "reasoning": "optional string for debugging",
  "actions": [
    {
      "tool": "read_file",
      "args": {"path": "/path/to/file"}
    },
    {
      "tool": "write_file",
      "args": {"path": "/path/to/file", "content": "..."}
    }
  ]
}
```

**Rules:**
- `actions` is required (empty array is valid)
- Each action must have `tool` (string) and `args` (object)
- `reasoning` is optional and ignored by harness (for debugging only)
- Any other fields are rejected
- Invalid JSON → iteration fails, logged, loop continues

---

## Available Tools

### File Operations
- `read_file(path: str) -> str`
  - Returns file content or error

- `write_file(path: str, content: str) -> bool`
  - Overwrites file, creates if missing

- `create_file(path: str, content: str) -> bool`
  - Fails if file exists

- `list_directory(path: str) -> list[str]`
  - Returns file/dir names in path

- `find_files(pattern: str, start_path: str, max_depth: int = 3) -> list[str]`
  - Glob-style search, limited depth

### No Other Tools
- No network access
- No arbitrary shell commands
- No "task_complete()" tool (LLM doesn't decide completion)

---

## External State Structure

All state lives on disk in a task directory:

```
tasks/
  <task_id>/
    task.json          # Immutable task definition
    state.json         # Mutable agent state
    heartbeat.json     # Liveness indicator
    actions.jsonl      # Append-only action log
    artifacts/         # Files created by agent
```

### task.json
```json
{
  "task_id": "uuid",
  "prompt": "Fix the bug in parser.py",
  "constraints": {
    "max_iterations": 50,
    "timeout_seconds": 300,
    "allowed_paths": ["/workspace"]
  },
  "created_at": "ISO8601"
}
```

### state.json
```json
{
  "status": "running",
  "iteration": 23,
  "started_at": "ISO8601",
  "updated_at": "ISO8601",
  "termination_reason": null
}
```

### heartbeat.json
```json
{
  "iteration": 23,
  "timestamp": "ISO8601",
  "status": "executing_action"
}
```

### actions.jsonl
Each line is a JSON object:
```json
{"iteration": 1, "timestamp": "...", "llm_response": {...}, "results": [...]}
{"iteration": 2, "timestamp": "...", "llm_response": {...}, "results": [...]}
```

---

## Termination Conditions

The harness terminates when **any** of these occur:

1. **Max iterations reached** (`state.iteration >= task.constraints.max_iterations`)
2. **Timeout exceeded** (`now() - state.started_at > task.constraints.timeout_seconds`)
3. **External stop signal** (`state.json` contains `"status": "stopped"`)
4. **Fatal error** (harness crashes)

**The LLM never decides termination.**

An external supervisor (Clowder) may:
- Monitor `heartbeat.json` for liveness
- Write `"status": "stopped"` to `state.json`
- Examine `actions.jsonl` to decide if task is complete

---

## Failure Modes & Handling

| Failure | Detection | Handling |
|---------|-----------|----------|
| Invalid JSON from LLM | Parse exception | Log error, increment iteration, retry |
| Schema validation fails | Validation error | Log error, increment iteration, retry |
| Unknown tool requested | Tool lookup fails | Log error, skip action, continue |
| Tool execution fails | Exception | Log error, return error to LLM next iteration |
| LLM outputs nothing | Empty/null response | Log error, increment iteration, retry |
| LLM gets stuck looping | Iteration limit | Terminate (external checks output) |
| LLM tries forbidden path | Path validation | Reject action, log security violation |
| Ollama unavailable | Connection error | Terminate with fatal error |
| Disk full / permission denied | OS error | Terminate with fatal error |
| State corruption | JSON decode error | Terminate with fatal error |

**Key principle:** Non-fatal errors increment the iteration counter and continue. Fatal errors stop the loop and require external intervention.

---

## Explicit Assumptions

1. **WSL is available:** Runs on Windows, calls Ollama via `wsl bash -c`
2. **Single-threaded:** One agent per task, no concurrency
3. **Synchronous:** Each iteration completes before the next starts
4. **Stateless LLM:** Model has no memory between calls
5. **Trust external state:** If `state.json` says iteration 23, that's the truth
6. **No retry logic:** If Ollama is down, fail fast
7. **No task queue:** Harness runs one task from start to finish
8. **No streaming:** LLM completes full response before parsing
9. **Sandboxed paths:** All file operations validate against `allowed_paths`
10. **No networking:** Direct subprocess call to WSL, no HTTP

---

## Why This Design

### Debuggability
- Every iteration is logged to `actions.jsonl`
- State is always on disk, inspectable
- Heartbeat shows liveness without parsing logs

### Determinism
- Fixed loop structure
- No hidden recursion
- External state is single source of truth
- Iteration counter prevents infinite loops

### Simplicity
- ~200 lines of Python
- No dependencies except subprocess (for WSL/Ollama)
- No async/await complexity
- No frameworks

---

## What This Design Excludes

- **Planning/reflection:** LLM sees context and acts, no meta-reasoning
- **Multi-agent coordination:** This is a single worker node (Clowder handles coordination)
- **Memory/embeddings:** No vector stores, no RAG
- **Human interaction:** No input(), no approval gates
- **Self-modification:** Agent cannot edit its own code
- **Tool synthesis:** Fixed tool set, no dynamic tools

---

## Next Steps

1. ✓ Implement `harness.py` (core loop)
2. ✓ Implement `tools.py` (file operations)
3. ✓ Implement `schema.py` (validation)
4. ✓ Implement `state.py` (state management)
5. Write tests for each component
6. Create example task definitions
7. Build Clowder supervisor (separate component)
