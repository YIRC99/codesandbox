import asyncio
import sys
from pathlib import Path

import uvicorn

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


if __name__ == "__main__":
    env_file = Path(__file__).resolve().parent / ".env"
    uvicorn.run(
        "src.sandbox.main:app",
        host="127.0.0.1",
        port=32004,
        reload=True,
        reload_dirs=["src"],
        env_file=str(env_file) if env_file.is_file() else None,
    )
