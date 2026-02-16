"""
Standalone LLM agent.

This is a self-contained agent that can run independently of Clowder.
It takes a prompt and workspace, executes actions via tools, and manages
its own iteration state.

The agent can be used directly for simple tasks, or wrapped by harness.py
for integration with Clowder's job/pipeline system.
"""

import json
import logging
import subprocess
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from schema import validate_llm_response, ValidationError
from tools import ToolRegistry, ToolError, SecurityError


logger = logging.getLogger(__name__)


class AgentError(Exception):
    """Fatal agent error."""
    pass


class Agent:
    """
    Standalone LLM agent.

    Runs a prompt using an LLM, executing actions via tools until
    completion or termination conditions are met.
    """

    def __init__(
        self,
        prompt: str,
        allowed_paths: list[str],
        model: str = "qwen3:8b",
        max_iterations: int = 50,
        timeout_seconds: int = 300,
    ):
        """
        Initialize agent.

        Args:
            prompt: Task description for the agent
            allowed_paths: List of allowed filesystem paths (security constraint)
            model: Model name to use (must be available in WSL Ollama)
            max_iterations: Maximum number of iterations before stopping
            timeout_seconds: Maximum runtime in seconds
        """
        self.prompt = prompt
        self.model = model
        self.max_iterations = max_iterations
        self.timeout_seconds = timeout_seconds

        # Initialize tool registry
        self.tool_registry = ToolRegistry(allowed_paths)

        # Agent state
        self.iteration = 0
        self.started_at: Optional[datetime] = None
        self.action_history: list[dict] = []
        self.termination_reason: Optional[str] = None

    def run_iteration(self) -> dict:
        """
        Run a single iteration of the agent loop.

        Returns:
            dict with keys:
                - iteration: current iteration number
                - llm_response: parsed LLM output
                - results: list of action execution results
                - raw_stdout: raw LLM stdout
                - raw_stderr: raw LLM stderr
                - should_terminate: bool indicating if agent should stop
                - termination_reason: reason for termination (if applicable)

        Raises:
            AgentError: If a fatal error occurs
        """
        # Initialize start time on first iteration
        if self.started_at is None:
            self.started_at = datetime.now(timezone.utc)

        # Increment iteration
        self.iteration += 1
        logger.info(f"=== Iteration {self.iteration} ===")

        # Check termination before running iteration
        termination_reason = self._check_termination()
        if termination_reason:
            logger.info(f"Terminating: {termination_reason}")
            self.termination_reason = termination_reason
            return {
                "iteration": self.iteration,
                "llm_response": {},
                "results": [],
                "raw_stdout": "",
                "raw_stderr": "",
                "should_terminate": True,
                "termination_reason": termination_reason,
            }

        # Call LLM
        logger.debug("Calling LLM")
        llm_response, raw_stdout, raw_stderr = self._call_llm()
        logger.debug(f"Model output: {json.dumps(llm_response, indent=2)}")

        # Execute actions
        logger.debug("Executing actions")
        results = self._execute_actions(llm_response)

        # Store in history
        history_entry = {
            "iteration": self.iteration,
            "llm_response": llm_response,
            "results": results,
        }
        self.action_history.append(history_entry)

        logger.info(f"Completed {len(results)} actions")

        # Check if any action was 'finish' - signals completion
        for result in results:
            if result.get("status") == "success" and isinstance(result.get("result"), dict):
                if result["result"].get("__FINISH__"):
                    finish_reason = result["result"].get("reason", "finish_tool_called")
                    logger.info(f"Agent finished via finish tool: {finish_reason}")
                    self.termination_reason = f"finish: {finish_reason}"
                    return {
                        "iteration": self.iteration,
                        "llm_response": llm_response,
                        "results": results,
                        "raw_stdout": raw_stdout,
                        "raw_stderr": raw_stderr,
                        "should_terminate": True,
                        "termination_reason": self.termination_reason,
                    }

        # Continue running
        return {
            "iteration": self.iteration,
            "llm_response": llm_response,
            "results": results,
            "raw_stdout": raw_stdout,
            "raw_stderr": raw_stderr,
            "should_terminate": False,
            "termination_reason": None,
        }

    def run(self) -> dict:
        """
        Run agent to completion.

        Runs iterations until termination condition is met.

        Returns:
            dict with keys:
                - termination_reason: why the agent stopped
                - iteration: final iteration count
                - action_history: complete action history
        """
        logger.info(f"Starting agent: {self.prompt}")

        while True:
            result = self.run_iteration()

            if result["should_terminate"]:
                return {
                    "termination_reason": result["termination_reason"],
                    "iteration": self.iteration,
                    "action_history": self.action_history,
                }

    def _check_termination(self) -> Optional[str]:
        """
        Check if agent should terminate.

        Returns:
            Termination reason if should terminate, None otherwise
        """
        # Check iteration limit
        if self.iteration >= self.max_iterations:
            return f"max_iterations_reached ({self.max_iterations})"

        # Check timeout
        if self.started_at:
            now = datetime.now(timezone.utc)
            elapsed = (now - self.started_at).total_seconds()

            if elapsed >= self.timeout_seconds:
                return f"timeout_exceeded ({elapsed:.1f}s / {self.timeout_seconds}s)"

        return None

    def _call_llm(self) -> tuple[dict, str, str]:
        """
        Call LLM with current context via WSL subprocess.

        Returns:
            Tuple of (parsed_json, raw_stdout, raw_stderr)

        Raises:
            AgentError: If LLM call fails
        """
        # Build context
        context = self._build_context()

        # Escape single quotes for bash -c
        escaped_context = context.replace("'", "'\\''")

        # Build WSL command
        cmd = [
            "wsl",
            "bash",
            "-c",
            f"ollama run {self.model} --format json '{escaped_context}'"
        ]

        # Call Ollama via WSL
        try:
            logger.info(f"Calling LLM via WSL (iteration {self.iteration})")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=120,
            )

            if result.returncode != 0:
                raise AgentError(f"LLM call failed: {result.stderr}")

        except subprocess.TimeoutExpired:
            raise AgentError("LLM call timed out after 120 seconds")
        except FileNotFoundError:
            raise AgentError("WSL not found. Ensure WSL is installed and in PATH.")
        except Exception as e:
            raise AgentError(f"LLM call failed: {e}")

        # Get raw outputs
        raw_stdout = result.stdout
        raw_stderr = result.stderr
        llm_output = result.stdout.strip()

        if not llm_output:
            logger.error("LLM returned empty response")
            return {"actions": []}, raw_stdout, raw_stderr

        # Clean LLM output: remove ANSI escape codes and markdown fences
        # Remove ANSI escape codes (terminal control sequences)
        llm_output = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', llm_output)
        # Remove markdown code fences
        llm_output = re.sub(r'```(?:json)?\s*\n?', '', llm_output)
        llm_output = llm_output.strip()

        if not llm_output:
            logger.error("LLM returned empty response after cleaning")
            return {"actions": []}, raw_stdout, raw_stderr

        # Parse JSON
        try:
            parsed = json.loads(llm_output)
            return parsed, raw_stdout, raw_stderr
        except json.JSONDecodeError as e:
            logger.error(f"LLM output is not valid JSON: {e}")
            logger.debug(f"Cleaned output: {llm_output[:500]}")
            return {"actions": []}, raw_stdout, raw_stderr

    def _build_context(self) -> str:
        """Build context string for LLM."""
        # Get workspace path (should be single path in most cases)
        workspace_path = str(self.tool_registry.allowed_paths[0]) if self.tool_registry.allowed_paths else "."

        context_parts = [
            "OUTPUT RAW JSON ONLY. NO MARKDOWN. NO CODE FENCES.",
            "",
            f"TASK: {self.prompt}",
            "",
            f"ALLOWED WORKSPACE: {workspace_path}",
            f"PATH RULES: All file paths must be inside workspace. Use full paths like {workspace_path}/file.txt",
            "",
            "TOOLS: read_file(path) write_file(path,content) create_file(path,content) list_directory(path) find_files(pattern,start_path,max_depth) transform_text(text,operation) finish(reason)",
            f"TRANSFORM OPS: uppercase, lowercase, title, strip",
            "",
            f"SCHEMA: {{\"actions\":[{{\"tool\":\"create_file\",\"args\":{{\"path\":\"{workspace_path}/file.txt\",\"content\":\"text\"}}}}]}}",
            "CHAINING: Use {{result}} for previous output, {{actions[N]}} for specific action",
            "",
            f"ITERATION: {self.iteration}/{self.max_iterations}",
            "",
        ]

        # Add action history (last 3 iterations, compact)
        if self.action_history:
            context_parts.append("RECENT:")
            for entry in self.action_history[-3:]:
                llm_response = entry.get("llm_response", {})
                # Handle case where LLM returned a list instead of dict (validation error)
                if isinstance(llm_response, dict):
                    actions = llm_response.get("actions", [])
                else:
                    actions = []
                results = entry.get("results", [])

                if actions:
                    tools_used = [a.get("tool") for a in actions if isinstance(a, dict)]
                    statuses = [r.get("status", "?") for r in results]
                    context_parts.append(f"  iter{entry['iteration']}: {','.join(tools_used)} -> {','.join(statuses)}")

                    # Show errors
                    for r in results:
                        if r.get("error"):
                            context_parts.append(f"    ERR: {r['error'][:100]}")
            context_parts.append("")

        context_parts.append("OUTPUT JSON NOW:")

        return "\n".join(context_parts)

    def _resolve_references(self, args: dict, results: list[dict]) -> dict:
        """
        Resolve template references in action arguments.

        Supports:
        - {{result}} - previous action's result
        - {{actions[N]}} - action N's result by index

        Args:
            args: Action arguments (may contain template strings)
            results: Previously executed action results

        Returns:
            Resolved arguments with templates replaced
        """
        import copy

        resolved = copy.deepcopy(args)

        def resolve_value(value):
            """Recursively resolve a value (string, dict, list, etc.)"""
            if isinstance(value, str):
                # Replace {{result}} with previous action's result
                if "{{result}}" in value and results:
                    prev_result = results[-1].get("result", "")
                    # If the entire string is just {{result}}, return the raw result
                    if value.strip() == "{{result}}":
                        return prev_result
                    # Otherwise do string replacement
                    value = value.replace("{{result}}", str(prev_result))

                # Replace {{actions[N]}} with specific action's result
                pattern = r"\{\{actions\[(\d+)\]\}\}"
                matches = re.findall(pattern, value)
                for match in matches:
                    idx = int(match)
                    if 0 <= idx < len(results):
                        result_val = results[idx].get("result", "")
                        # If entire string is just the template, return raw result
                        if value.strip() == f"{{{{actions[{idx}]}}}}":
                            return result_val
                        # Otherwise do string replacement
                        value = value.replace(f"{{{{actions[{idx}]}}}}", str(result_val))
                    else:
                        logger.warning(f"Invalid action reference: actions[{idx}] (only {len(results)} actions executed)")

                return value

            elif isinstance(value, dict):
                return {k: resolve_value(v) for k, v in value.items()}

            elif isinstance(value, list):
                return [resolve_value(item) for item in value]

            else:
                return value

        return resolve_value(resolved)

    def _execute_actions(self, llm_response: dict) -> list[dict]:
        """
        Execute actions from LLM response.

        Actions are executed sequentially. Each action can reference
        results from previously executed actions using template syntax:
        - {{result}} - previous action's result
        - {{actions[N]}} - specific action's result

        Returns:
            List of execution results
        """
        results = []

        try:
            validated = validate_llm_response(llm_response)
        except ValidationError as e:
            logger.error(f"Schema validation failed: {e}")
            return [{"status": "validation_error", "error": str(e)}]

        # Execute each action sequentially
        for i, action in enumerate(validated.actions):
            logger.info(f"Executing action {i + 1}/{len(validated.actions)}: {action.tool}")

            # Resolve references in args before execution
            resolved_args = self._resolve_references(action.args, results)

            try:
                result = self.tool_registry.execute(action.tool, resolved_args)
                results.append({
                    "tool": action.tool,
                    "args": resolved_args,  # Store resolved args
                    "status": "success",
                    "result": result,
                })
            except SecurityError as e:
                logger.error(f"Security violation: {e}")
                results.append({
                    "tool": action.tool,
                    "args": resolved_args,
                    "status": "security_error",
                    "error": str(e),
                })
            except ToolError as e:
                logger.error(f"Tool execution failed: {e}")
                results.append({
                    "tool": action.tool,
                    "args": resolved_args,
                    "status": "error",
                    "error": str(e),
                })

        return results
