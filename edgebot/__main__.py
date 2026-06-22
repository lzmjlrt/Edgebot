"""python -m edgebot entry point."""

import asyncio
import sys


def _usage_error(message: str) -> None:
    print(message, file=sys.stderr)
    print('Usage: edgebot exec "<instruction>"', file=sys.stderr)
    sys.exit(2)


def main() -> None:
    """Run the interactive Edgebot CLI or a non-interactive command."""
    if len(sys.argv) > 1 and sys.argv[1] == "exec":
        if len(sys.argv) < 3 or not sys.argv[2].strip():
            _usage_error("Missing instruction for edgebot exec.")
        from edgebot.cli.exec_once import exec_main

        output = asyncio.run(exec_main(sys.argv[2]))
        if output:
            print(output)
        return

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
