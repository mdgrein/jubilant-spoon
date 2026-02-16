# Agent Refactoring

## Overview

The agent has been split into two parts:

1. **`agent.py`** - Standalone agent that can run independently
2. **`harness.py`** - Clowder-specific wrapper that integrates with the job/pipeline system

This separation allows you to use the agent to work on Clowder itself without needing the full Clowder system operational.

## Architecture

### Before

```
harness.py
├── LLM calling logic
├── Context building
├── Action execution
├── Tool registry
└── Clowder database integration
```

### After

```
agent.py (Standalone)
├── LLM calling logic
├── Context building
├── Action execution
├── Tool registry
└── In-memory state management

harness.py (Clowder Integration)
├── Load jobs from database
├── Create Agent instances
├── Sync agent state to database
├── Handle external stop signals
└── Log actions for observability
```

## Usage

### Standalone Agent (No Clowder Required)

Use `run_agent.py` for quick tasks:

```bash
# Simple task in current directory
python agents/run_agent.py "Create hello.txt with 'Hello World'"

# Specify workspace
python agents/run_agent.py "List all Python files" --workspace /path/to/dir

# Use different model
python agents/run_agent.py "Read and summarize main.py" --model qwen3:14b

# Adjust limits
python agents/run_agent.py "Complex task" --max-iterations 100 --timeout 600
```

### Clowder-Integrated Harness

Use `harness.py` when running as part of Clowder pipelines:

```bash
# Run existing job by ID
python agents/harness.py <job_id>

# Create and run new job (for testing)
python agents/harness.py --prompt "Find all Python files"
```

## Key Benefits

### 1. Bootstrapping
You can now use the agent to work on Clowder itself without needing the full Clowder system running. This solves the chicken-and-egg problem.

### 2. Testing
The standalone agent is easier to test in isolation without database setup and job management complexity.

### 3. Flexibility
The agent can be used in other contexts:
- CLI tools
- Jupyter notebooks
- Other orchestration systems
- Quick one-off tasks

### 4. Clarity
Clean separation between:
- **Agent logic**: LLM interaction, tool execution, termination
- **Orchestration logic**: Job management, database sync, external signals

## Implementation Details

### Agent State

The `Agent` class maintains its own state:
- `iteration`: Current iteration number
- `started_at`: Start timestamp
- `action_history`: List of previous actions and results
- `termination_reason`: Why the agent stopped

### Harness Integration

The `AgentHarness` class:
1. Loads job from database
2. Creates `Agent` instance with job parameters
3. Restores action history from database
4. Runs agent iterations in a loop
5. Syncs state back to database after each iteration
6. Handles external stop signals (job status changes)

### Action History

Action history is stored in both places:
- **Agent**: In-memory list for building context
- **Database**: Persisted log via harness

When the harness creates an agent for an existing job, it loads the action history from the database and seeds the agent with it.

## Migration Notes

### What Changed

- **Removed from harness.py**:
  - `_call_llm()`
  - `_build_context()`
  - `_resolve_references()`
  - `_execute_actions()`

- **Moved to agent.py**:
  - All LLM calling logic
  - Context building
  - Reference resolution
  - Action execution

- **Simplified in harness.py**:
  - `run()` now creates an `Agent` instance and delegates to it
  - Harness only handles DB operations and state sync

### Backward Compatibility

The harness maintains the same interface:
- CLI arguments unchanged
- Database schema unchanged
- Behavior is identical from Clowder's perspective

## Example: Working on Clowder

Before this refactoring, you couldn't use the agent to help develop Clowder because the agent required Clowder to be fully operational (jobs, pipelines, database, etc.).

Now you can:

```bash
# Use the standalone agent to help debug Clowder
python agents/run_agent.py "Read server/main.py and explain the pipeline creation logic"

# Use it to refactor Clowder code
python agents/run_agent.py "Refactor client/main.py to use better error handling"

# Generate tests for Clowder
python agents/run_agent.py "Create unit tests for agents/tools.py"
```

The agent has full access to the codebase via its tools and can help you work on Clowder itself!
