"""Entry point: python -m app  (PORT defaults to 7860, the Hugging Face Spaces convention)."""

from __future__ import annotations

import os
import sys

import uvicorn


def main() -> int:
    raw_port = os.environ.get("PORT", "7860")
    if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
        print("PORT must be an integer between 1 and 65535", file=sys.stderr)
        return 2
    host = os.environ.get("HOST", "0.0.0.0")  # containers must listen on all interfaces; use 127.0.0.1 locally
    uvicorn.run("app.main:app", host=host, port=int(raw_port), workers=1, proxy_headers=False,
                server_header=False, date_header=False, access_log=False, log_level="info", timeout_keep_alive=5,
                limit_concurrency=100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
