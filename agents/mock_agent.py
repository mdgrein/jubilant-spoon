#!/usr/bin/env python3
"""
Mock Agent - Simulates agent work for testing pipelines.

Features:
- Configurable failure rate (default 10%)
- Configurable duration (simulates work)
- Inline commands (lambdas)
- Realistic logging output
"""

import argparse
import random
import time
import sys
import subprocess
import io

# Fix Windows console encoding issues
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def main():
    parser = argparse.ArgumentParser(description="Mock agent for testing pipelines")
    parser.add_argument("--agent-type", default="mock", help="Agent type (planner, dev, tester, etc.)")
    parser.add_argument("--failure-rate", type=float, default=0.1, help="Probability of failure (0.0-1.0, default: 0.1)")
    parser.add_argument("--duration", type=float, default=1.0, help="Simulated work duration in seconds (default: 1.0)")
    parser.add_argument("--command", type=str, help="Inline shell command to execute")
    parser.add_argument("--python", type=str, help="Inline Python code to execute (like a lambda)")
    parser.add_argument("--prompt", type=str, default="", help="Task prompt (for logging)")

    args = parser.parse_args()

    # Start message
    print(f"[MOCK {args.agent_type.upper()}] Starting...")
    if args.prompt:
        print(f"[MOCK {args.agent_type.upper()}] Prompt: {args.prompt[:100]}")

    # Simulate work
    print(f"[MOCK {args.agent_type.upper()}] Working for {args.duration}s...")
    time.sleep(args.duration)

    # Execute inline shell command if provided
    if args.command:
        print(f"[MOCK {args.agent_type.upper()}] Executing inline shell command...")
        try:
            result = subprocess.run(
                args.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.stdout:
                print(result.stdout.rstrip())
            if result.stderr:
                print(result.stderr.rstrip(), file=sys.stderr)

            if result.returncode != 0:
                print(f"[MOCK {args.agent_type.upper()}] Inline command failed with code {result.returncode}")
                sys.exit(result.returncode)
        except subprocess.TimeoutExpired:
            print(f"[MOCK {args.agent_type.upper()}] Inline command timed out!")
            sys.exit(124)
        except Exception as e:
            print(f"[MOCK {args.agent_type.upper()}] Error executing inline command: {e}")
            sys.exit(1)

    # Execute inline Python code if provided
    if args.python:
        print(f"[MOCK {args.agent_type.upper()}] Executing inline Python code...")
        try:
            # Create a safe namespace with useful imports pre-loaded
            namespace = {
                '__builtins__': __builtins__,
                'json': __import__('json'),
                'random': __import__('random'),
                'datetime': __import__('datetime'),
                'time': __import__('time'),
                'sys': sys,
                'print': print,
                # Pass arguments to the Python code
                'prompt': args.prompt,
                'agent_type': args.agent_type,
            }

            # Execute the inline Python code
            exec(args.python, namespace)

        except Exception as e:
            print(f"[MOCK {args.agent_type.upper()}] Python code error: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            sys.exit(1)

    # Random failure simulation
    if random.random() < args.failure_rate:
        print(f"[MOCK {args.agent_type.upper()}] FAILED (simulated failure)")
        sys.exit(1)

    # Success
    print(f"[MOCK {args.agent_type.upper()}] Complete [OK]")
    sys.exit(0)


if __name__ == "__main__":
    main()
