"""Launch Intelligence without raw access logs or dependency exception strings."""

import logging
import os

import uvicorn

from .http_runtime import SafeFormatter
from .main import app


def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter())
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    logging.captureWarnings(True)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("INTELLIGENCE_PORT") or os.environ.get("PORT", "8000")),
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
