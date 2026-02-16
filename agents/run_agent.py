"""
Standalone agent CLI.

Run the LLM agent directly without Clowder's job/pipeline system.
Useful for quick tasks, testing, and working on Clowder itself.

Example:
    python agents/run_agent.py "Read agents/agent.py and count the lines"
"""

import argparse
import sys
import logging
from pathlib import Path

from agent import Agent, AgentError


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    """Run standalone agent."""
    parser = argparse.ArgumentParser(
        description="Run standalone LLM agent (no Clowder required)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Simple task
  python run_agent.py "Create a file called hello.txt with 'Hello World'"

  # With custom workspace
  python run_agent.py "List all Python files" --workspace /path/to/dir

  # Change model
  python run_agent.py "Count lines in main.py" --model qwen3:14b

  # Adjust limits
  python run_agent.py "Complex task" --max-iterations 100 --timeout 600
        """
    )

    parser.add_argument(
        "prompt",
        type=str,
        help="Task for the agent to perform"
    )
    parser.add_argument(
        "--workspace",
        type=str,
        help="Workspace directory (default: current directory)"
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=50,
        help="Maximum iterations (default: 50)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds (default: 300)"
    )
    parser.add_argument(
        "--model",
        type=str,
        default="qwen3:8b",
        help="Model name (default: qwen3:8b)"
    )

    args = parser.parse_args()

    # Determine workspace
    if args.workspace:
        workspace_path = Path(args.workspace).resolve()
    else:
        workspace_path = Path.cwd()

    if not workspace_path.exists():
        logger.error(f"Workspace does not exist: {workspace_path}")
        sys.exit(1)

    logger.info(f"Workspace: {workspace_path}")

    # Create agent
    agent = Agent(
        prompt=args.prompt,
        allowed_paths=[str(workspace_path)],
        model=args.model,
        max_iterations=args.max_iterations,
        timeout_seconds=args.timeout,
    )

    print(f"\n{'='*60}")
    print(f"TASK: {args.prompt}")
    print(f"{'='*60}\n")

    # Run agent
    try:
        result = agent.run()

        print(f"\n{'='*60}")
        print(f"COMPLETED: {result['termination_reason']}")
        print(f"Iterations: {result['iteration']}")
        print(f"{'='*60}\n")

        # Exit with success if finished via finish tool
        if result['termination_reason'].startswith('finish:'):
            sys.exit(0)

        # Exit with failure for max iterations or timeout
        if 'max_iterations' in result['termination_reason'] or 'timeout' in result['termination_reason']:
            sys.exit(1)

    except AgentError as e:
        logger.error(f"Agent failed: {e}")
        sys.exit(1)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)


if __name__ == "__main__":
    main()
