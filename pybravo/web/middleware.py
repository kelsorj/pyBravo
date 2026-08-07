"""HTTP request/response logging middleware for the PyBravo FastAPI server."""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("pybravo.web.http")

_SKIP_PATHS = frozenset({
    "/api/state",
    "/api/deck/state",
})

_SKIP_PREFIXES = (
    "/ws/",
    "/static/",
    "/labware/",
)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every HTTP request with method, path, status, and duration.

    Skips high-frequency polling endpoints (``/api/state``, WebSocket
    upgrades, static assets) to avoid flooding the logs.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if path in _SKIP_PATHS or path.startswith(_SKIP_PREFIXES):
            return await call_next(request)

        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000

        logger.info(
            "%s %s -> %d (%.1fms)",
            request.method,
            path,
            response.status_code,
            duration_ms,
        )
        return response
