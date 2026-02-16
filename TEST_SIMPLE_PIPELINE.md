# Testing Simple 3-Stage Pipeline

## What This Pipeline Does

**Stage 1: Planner**
- Takes your prompt
- Breaks it into simple coding tasks
- Outputs JSON list of tasks
- Artifact: `stdout_final` (plan as JSON)

**Stage 2: Script-Kiddie**
- Reads planner's output
- Writes code for each task
- Outputs code to stdout (no file operations)
- Artifact: `stdout_final` (code)

**Stage 3: Verifier**
- Reads original prompt + script-kiddie output
- Checks if code meets requirements
- Outputs PASS/FAIL
- Artifact: `stdout_final` (verification result)

## Prerequisites

1. **Ollama installed and running:**
   ```bash
   # Check if ollama is running
   ollama list

   # If not installed: https://ollama.ai
   # Pull a model:
   ollama pull llama3.2:latest
   ```

2. **Server running:**
   ```bash
   python server/main.py
   ```

3. **Client running (optional, for UI):**
   ```bash
   python client/main.py
   ```

## Start the Pipeline

### Via API:
```bash
curl -X POST http://localhost:8000/pipelines/simple-plan-code-verify/start \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write a function to calculate fibonacci numbers", "workspace_path": "./"}'
```

### Via Client UI:
1. Launch client: `python client/main.py`
2. Navigate to "Templates"
3. Select "Simple: Plan → Code → Verify"
4. Press Enter or click "Start Pipeline"
5. Enter prompt: "Write a function to calculate fibonacci numbers"

## Watch Progress

### In Client UI:
- Navigate to "Running" section
- Expand the pipeline
- See jobs change status: pending → running → completed
- Click jobs to see their output

### In Logs:
```bash
# Server logs show:
[INFO] Started pipeline abc123
[INFO] Spawned job def456 (planner)
[INFO] Job def456 completed
[INFO] Job def456 collected 1 artifact(s)
[INFO] Spawned job ghi789 (script-kiddie)
...
```

### Check Artifacts:
```bash
python -c "
import sqlite3
conn = sqlite3.connect('clowder.db')
conn.row_factory = sqlite3.Row

# Get latest pipeline
pipeline = conn.execute('''
    SELECT pipeline_id, original_prompt, status
    FROM pipelines
    ORDER BY created_at DESC
    LIMIT 1
''').fetchone()

print(f'Pipeline: {pipeline[\"pipeline_id\"][:8]}')
print(f'Prompt: {pipeline[\"original_prompt\"]}')
print(f'Status: {pipeline[\"status\"]}')
print()

# Get all artifacts
artifacts = conn.execute('''
    SELECT a.name, a.type, a.content, j.agent_type
    FROM artifacts a
    JOIN jobs j ON a.job_id = j.job_id
    WHERE j.pipeline_id = ?
    ORDER BY a.created_at
''', (pipeline['pipeline_id'],)).fetchall()

for artifact in artifacts:
    print(f'--- {artifact[\"agent_type\"]}: {artifact[\"name\"]} ---')
    print(artifact['content'][:200] if artifact['content'] else '(no content)')
    print()
"
```

## Expected Flow

1. **Planner runs:**
   - Receives: "Write a function to calculate fibonacci numbers"
   - Outputs: `["Create a fibonacci function"]`
   - Artifact stored

2. **Script-Kiddie runs:**
   - Receives: Planner's task + original prompt
   - Outputs: Python code for fibonacci
   - Artifact stored

3. **Verifier runs:**
   - Receives: Original prompt + Script-Kiddie's code
   - Outputs: "PASS" or "FAIL - <reason>"
   - Artifact stored

## Troubleshooting

**"Ollama not found":**
- Install Ollama: https://ollama.ai
- Make sure `ollama` command is in PATH

**"Model timeout":**
- Increase timeout in template (default 300s)
- Use faster model: `--model llama3.2:1b`

**Pipeline stuck:**
- Check server logs for errors
- Check if ollama is running: `ollama list`
- Check job status: `SELECT * FROM jobs WHERE status='running'`

**Verifier always fails:**
- This is expected! Script-Kiddie output may not be perfect
- Future: Implement feedback loop (verifier spawns new script-kiddie jobs)

## Next Steps

Once this works:
1. Add feedback loop (verifier spawns new jobs on failure)
2. Add file-based artifacts (use `git_diff` strategy)
3. Create more complex templates
