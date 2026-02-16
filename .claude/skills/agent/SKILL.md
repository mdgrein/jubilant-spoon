---
name: agent
description: Run the local LLM agent with a task description
argument-hint: "[task description]"
---

Run the standalone agent to handle this task:

$ARGUMENTS

Execute the agent with this command:

```bash
python agents/run_agent.py "$ARGUMENTS" --workspace .
```

The agent will:
- Use the local Ollama model (qwen3:8b)
- Work within the current project workspace
- Execute actions via tools (read/write files, etc.)
- Report results when complete

**Note**: The agent runs independently using WSL + Ollama, not through Claude's API.
