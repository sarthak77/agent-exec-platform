"""Streamable-HTTP MCP server bootstrap."""

from __future__ import annotations

import logging

import uvicorn

from mcp_svc.config import settings
from mcp_svc.server import build_app


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    app = build_app()
    uvicorn.run(app, host=settings.http.host, port=settings.http.port)


if __name__ == "__main__":
    run()
