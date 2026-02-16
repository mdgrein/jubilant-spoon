"""
Test if the model can produce valid JSON outputs.

This helps verify the model will work with the orchestrator's strict schema.
"""

import subprocess
import json
import sys


def test_json_format(model="qwen2.5-coder:7b"):
    """Test if model produces valid JSON."""
    print(f"Testing JSON output from '{model}'...")
    print("=" * 60)

    prompt = """You must respond with valid JSON only. No markdown, no explanations.

Output Format:
{
  "reasoning": "your thought process here",
  "actions": [
    {"tool": "read_file", "args": {"path": "/example.txt"}}
  ]
}

Task: Read a file called config.json

Remember: Output ONLY valid JSON, nothing else."""

    # Escape for bash
    escaped_prompt = prompt.replace("'", "'\\''")

    cmd = [
        "wsl",
        "bash",
        "-c",
        f"ollama run {model} '{escaped_prompt}'"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            print(f"✗ Model call failed: {result.stderr}")
            return False

        output = result.stdout.strip()

        print("\nRaw output:")
        print("-" * 60)
        print(output)
        print("-" * 60)

        # Try to parse as JSON
        try:
            data = json.loads(output)
            print("\n✓ Valid JSON!")
            print("\nParsed structure:")
            print(json.dumps(data, indent=2))

            # Check schema
            errors = []

            if "actions" not in data:
                errors.append("Missing required field: 'actions'")
            elif not isinstance(data["actions"], list):
                errors.append("'actions' must be a list")
            else:
                for i, action in enumerate(data["actions"]):
                    if "tool" not in action:
                        errors.append(f"Action {i} missing 'tool' field")
                    if "args" not in action:
                        errors.append(f"Action {i} missing 'args' field")

            if errors:
                print("\n✗ Schema validation errors:")
                for error in errors:
                    print(f"  - {error}")
                return False
            else:
                print("\n✓ Schema is valid!")
                return True

        except json.JSONDecodeError as e:
            print(f"\n✗ Invalid JSON: {e}")
            print("\nThe model may need prompting adjustment or may not support JSON mode.")
            return False

    except subprocess.TimeoutExpired:
        print("✗ Request timed out")
        return False
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


def main():
    """Run JSON output test."""
    success = test_json_format()

    print("\n" + "=" * 60)
    if success:
        print("✓ Model produces valid JSON! Ready for orchestrator.")
    else:
        print("✗ Model does not produce valid JSON.")
        print("\nTroubleshooting:")
        print("1. Try a different model (e.g., qwen2.5-coder:14b)")
        print("2. Adjust the prompt in _build_context() to be more explicit")
        print("3. Add post-processing to extract JSON from markdown blocks")
    print("=" * 60)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
