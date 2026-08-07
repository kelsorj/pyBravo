from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


DEFAULT_VISION_SERVICE_URL = os.getenv("PYBRAVO_VISION_SERVICE_URL", "http://127.0.0.1:8101")


class VisionServiceError(RuntimeError):
    pass


class VisionServiceClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or DEFAULT_VISION_SERVICE_URL).rstrip("/")

    def url_for(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body: bytes | None = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read().decode("utf-8")
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as exc:  # pragma: no cover - thin network wrapper
            detail = exc.read().decode("utf-8", errors="ignore").strip()
            raise VisionServiceError(detail or f"Vision service HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - thin network wrapper
            raise VisionServiceError(f"Vision service unavailable at {self.base_url}") from exc

    def request_bytes(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> tuple[bytes, str]:
        url = f"{self.base_url}{path}"
        body: bytes | None = None
        headers = {"Accept": "*/*"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read()
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                return content, content_type
        except urllib.error.HTTPError as exc:  # pragma: no cover - thin network wrapper
            detail = exc.read().decode("utf-8", errors="ignore").strip()
            raise VisionServiceError(detail or f"Vision service HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - thin network wrapper
            raise VisionServiceError(f"Vision service unavailable at {self.base_url}") from exc

