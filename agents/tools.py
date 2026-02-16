"""
Tool implementations for agent actions.
All tools are sandboxed and validate paths.
"""

import os
import glob
from pathlib import Path
from typing import Any


class ToolError(Exception):
    """Tool execution failed."""
    pass


class SecurityError(ToolError):
    """Path validation failed."""
    pass


class ToolRegistry:
    """Registry of available tools with path sandboxing."""

    def __init__(self, allowed_paths: list[str]):
        """
        Initialize tool registry.

        Args:
            allowed_paths: List of allowed base paths for file operations
        """
        self.allowed_paths = [Path(p).resolve() for p in allowed_paths]

        # Map tool names to methods
        self.tools = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "create_file": self.create_file,
            "list_directory": self.list_directory,
            "find_files": self.find_files,
            "transform_text": self.transform_text,
            "finish": self.finish,
        }

    def _validate_path(self, path: str) -> Path:
        """
        Validate path is within allowed directories.

        Raises:
            SecurityError: If path is outside allowed paths
        """
        resolved = Path(path).resolve()

        for allowed in self.allowed_paths:
            try:
                resolved.relative_to(allowed)
                return resolved
            except ValueError:
                continue

        raise SecurityError(f"Path {path} is outside allowed paths: {self.allowed_paths}")

    def execute(self, tool_name: str, args: dict[str, Any]) -> Any:
        """
        Execute a tool by name.

        Args:
            tool_name: Name of tool to execute
            args: Arguments to pass to tool

        Returns:
            Tool result

        Raises:
            ToolError: If tool doesn't exist or execution fails
        """
        if tool_name not in self.tools:
            raise ToolError(f"Unknown tool: {tool_name}")

        try:
            return self.tools[tool_name](**args)
        except TypeError as e:
            raise ToolError(f"Invalid arguments for {tool_name}: {e}")
        except Exception as e:
            raise ToolError(f"{tool_name} failed: {e}")

    def read_file(self, path: str) -> str:
        """Read file contents."""
        validated_path = self._validate_path(path)

        if not validated_path.exists():
            raise ToolError(f"File not found: {path}")

        if not validated_path.is_file():
            raise ToolError(f"Not a file: {path}")

        return validated_path.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> bool:
        """Write content to file (overwrites if exists)."""
        validated_path = self._validate_path(path)

        # Create parent directories if needed
        validated_path.parent.mkdir(parents=True, exist_ok=True)

        validated_path.write_text(content, encoding="utf-8")
        return True

    def create_file(self, path: str, content: str) -> bool:
        """Create new file (fails if exists)."""
        validated_path = self._validate_path(path)

        if validated_path.exists():
            raise ToolError(f"File already exists: {path}")

        # Create parent directories if needed
        validated_path.parent.mkdir(parents=True, exist_ok=True)

        validated_path.write_text(content, encoding="utf-8")
        return True

    def list_directory(self, path: str) -> list[str]:
        """List files and directories in path."""
        validated_path = self._validate_path(path)

        if not validated_path.exists():
            raise ToolError(f"Directory not found: {path}")

        if not validated_path.is_dir():
            raise ToolError(f"Not a directory: {path}")

        return sorted([item.name for item in validated_path.iterdir()])

    def find_files(self, pattern: str, start_path: str, max_depth: int = 3) -> list[str]:
        """
        Find files matching glob pattern.

        Args:
            pattern: Glob pattern (e.g., "*.py", "**/*.txt")
            start_path: Where to start searching
            max_depth: Maximum directory depth to search

        Returns:
            List of matching file paths (relative to start_path)
        """
        validated_path = self._validate_path(start_path)

        if not validated_path.exists():
            raise ToolError(f"Start path not found: {start_path}")

        if not validated_path.is_dir():
            raise ToolError(f"Start path is not a directory: {start_path}")

        # Limit depth to prevent excessive searching
        if max_depth > 5:
            max_depth = 5

        # Build glob pattern with depth limit
        if "**" in pattern:
            # Pattern already has recursive glob
            search_pattern = str(validated_path / pattern)
        else:
            # Add depth-limited recursion
            search_pattern = str(validated_path / f"{'*/' * max_depth}{pattern}")

        matches = glob.glob(search_pattern, recursive=True)

        # Return relative paths
        results = []
        for match in matches:
            try:
                rel_path = Path(match).relative_to(validated_path)
                results.append(str(rel_path))
            except ValueError:
                # Skip files outside start_path
                continue

        return sorted(results)

    def transform_text(self, text: str, operation: str) -> str:
        """
        Transform text using specified operation.

        Args:
            text: Text to transform
            operation: One of: "uppercase", "lowercase", "title", "strip"

        Returns:
            Transformed text
        """
        operations = {
            "uppercase": lambda t: t.upper(),
            "lowercase": lambda t: t.lower(),
            "title": lambda t: t.title(),
            "strip": lambda t: t.strip(),
        }

        if operation not in operations:
            raise ToolError(f"Unknown operation: {operation}. Available: {list(operations.keys())}")

        return operations[operation](text)

    def finish(self, **kwargs) -> dict:
        """
        Signal job completion.

        This is a special tool that tells the harness the job is done.
        Returns a dict with a special __FINISH__ marker that the harness checks.

        Args:
            **kwargs: Any arguments (e.g., reason, result, etc.)

        Returns:
            Dict with __FINISH__ marker and provided kwargs
        """
        return {"__FINISH__": True, **kwargs}
