"""python -m edgebot entry point."""

import asyncio
import sys


def main() -> None:
    """Run the interactive Edgebot CLI."""
    try:
        from edgebot.cli.repl import main as repl_main
    except Exception as exc:
        if exc.__class__.__name__ == "ConfigError":
            print("Edgebot configuration is missing.\n", file=sys.stderr)
            print(str(exc), file=sys.stderr)
            print("\nCreate a `.env` file in the current workspace, for example:", file=sys.stderr)
            print("MODEL_ID=deepseek/deepseek-chat", file=sys.stderr)
            print("API_KEY=your-api-key-here", file=sys.stderr)
            sys.exit(2)
        raise

    asyncio.run(repl_main())

if __name__ == "__main__":
    main()
