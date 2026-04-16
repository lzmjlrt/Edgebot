"""python -m edgebot entry point."""

import asyncio

from edgebot.cli.repl import main

if __name__ == "__main__":
    asyncio.run(main())
