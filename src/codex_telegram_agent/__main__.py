import asyncio
import os
from pathlib import Path

from .worker import serve


def main():
    os.umask(0o077)
    try:
        asyncio.run(
            serve(
                Path("/home/maksym/.local/share/codex-telegram"),
                Path("/home/maksym/projects"),
                Path("/home/maksym/projects/luxpower-telegram-bot/state/codex.sock"),
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
