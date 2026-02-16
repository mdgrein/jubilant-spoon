# Agent System Prompts

Stub prompts for different agent types in the Clowder system.

## Agent Types

### Planner (`planner.txt`)
**Purpose:** Break user requests into discrete, executable tasks

**Input:**
- User prompt
- Workspace path
- Existing file structure

**Output:**
```json
{
  "tasks": [
    {
      "id": "task_1",
      "description": "...",
      "type": "dev|test|verify",
      "dependencies": [],
      "files_involved": [],
      "acceptance_criteria": "..."
    }
  ],
  "execution_order": ["task_1", "task_2"]
}
```

**Use case:** First step in pipeline - decompose complex requests

---

### Dev (`dev.txt`)
**Purpose:** Implement code changes through file operations

**Input:**
- Task description
- Acceptance criteria

**Output:**
```json
{
  "reasoning": "...",
  "actions": [
    {"tool": "read_file", "args": {...}},
    {"tool": "write_file", "args": {...}}
  ]
}
```

**Use case:** Core implementation agent (current harness)

---

### Tester (`tester.txt`)
**Purpose:** Design test scenarios for feature coverage

**Input:**
- Original prompt
- Files to test

**Output:**
```json
{
  "test_scenarios": [
    {
      "scenario": "...",
      "type": "unit|integration|e2e",
      "setup": "...",
      "action": "...",
      "expected": "...",
      "edge_cases": []
    }
  ],
  "coverage_assessment": "..."
}
```

**Use case:** After dev completes, plan comprehensive tests

---

### Verifier (`verifier.txt`)
**Purpose:** Verify implementation matches original prompt

**Input:**
- Original prompt
- Files changed

**Output:**
```json
{
  "verification_result": "pass|fail|partial",
  "findings": [
    {
      "requirement": "...",
      "status": "satisfied|missing|incorrect",
      "evidence": "...",
      "location": "..."
    }
  ],
  "recommendation": "pass|return to dev|needs clarification"
}
```

**Use case:** Final step - ensure work meets requirements

---

## Pipeline Example

```
User Prompt
    ↓
[Planner] → task_list
    ↓
[Dev] → implements task_1
    ↓
[Tester] → plans tests for task_1
    ↓
[Dev] → implements tests
    ↓
[Verifier] → checks task_1 against original prompt
    ↓
(pass) → next task
(fail) → back to Dev with feedback
```

---

## Using These Prompts

### Option 1: Modify harness to load prompt templates
```python
def load_prompt_template(agent_type: str, **kwargs) -> str:
    template_path = Path("agents/prompts") / f"{agent_type}.txt"
    template = template_path.read_text()
    return template.format(**kwargs)
```

### Option 2: Create separate agent types
```python
class PlannerAgent(AgentHarness):
    def _build_context(self, task, state, history):
        return load_prompt_template("planner",
            user_prompt=task.prompt,
            workspace_path=self.workspace,
            ...)
```

### Option 3: Dynamic agent type selection
```bash
python harness.py --agent-type planner --prompt "Add user auth"
python harness.py --agent-type dev --prompt "Implement task_1"
python harness.py --agent-type tester --prompt "Test user auth"
python harness.py --agent-type verifier --prompt "Verify user auth"
```

---

## Customization

These are **stubs** - tune them based on:
- Model size (smaller models need simpler prompts)
- Domain specifics (web dev, CLI tools, data processing)
- Organizational standards
- Observed model behavior

---

## Design Principles

All prompts follow:
1. **Tight format** - Minimal tokens, maximum clarity
2. **JSON-first** - Structured output for parsing
3. **Tool-oriented** - Actions via tools, not free text
4. **Context-aware** - Include iteration count and history
5. **Explicit** - No ambiguity about expectations
