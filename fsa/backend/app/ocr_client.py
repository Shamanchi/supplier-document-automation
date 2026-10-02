"""Клиент локальных OCR-сервисов.

Говорит на общем контракте `../API_CONTRACT.md`: multipart на `/v1/ocr`,
ответ — ZIP с `result.json` и `text.txt`. Ошибки движка транслируются в
доменное исключение, чтобы наружу не утекали его внутренние коды.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

import httpx

from .engines import EngineEndpoint


class OCRUnavailable(RuntimeError):
    """Движок недоступен, занят или вернул ошибку."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class OCRResult:
    engine: str
    schema_version: str
    page_count: int
    duration_ms: int
    text: str
    pages: tuple[dict[str, Any], ...]
    source_sha256: str = ""

    @property
    def mean_confidence(self) -> float | None:
        values = [
            page.get("mean_confidence")
            for page in self.pages
            if page.get("mean_confidence") is not None
        ]
        if not values:
            return None
        return sum(values) / len(values)


class OCRClient:
    def __init__(
        self,
        endpoint: EngineEndpoint,
        *,
        timeout: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "OCRClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._endpoint.service_key}"}

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._require_client().get(self._endpoint.health_url)
        except httpx.HTTPError as exc:
            raise OCRUnavailable(f"engine {self._endpoint.name} is unreachable") from exc
        return _json_or_raise(response)

    async def info(self) -> dict[str, Any]:
        try:
            response = await self._require_client().get(
                self._endpoint.info_url, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise OCRUnavailable(f"engine {self._endpoint.name} is unreachable") from exc
        return _json_or_raise(response)

    async def recognize(
        self, payload: bytes, filename: str, media_type: str
    ) -> OCRResult:
        files = {"file": (filename, payload, media_type)}
        try:
            response = await self._require_client().post(
                self._endpoint.ocr_url, headers=self._headers, files=files
            )
        except httpx.HTTPError as exc:
            raise OCRUnavailable(f"engine {self._endpoint.name} is unreachable") from exc

        if response.status_code != 200:
            raise OCRUnavailable(
                _error_message(response), status_code=response.status_code
            )
        return _parse_bundle(response.content, response.headers.get("X-Source-SHA256", ""))


def _json_or_raise(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise OCRUnavailable(_error_message(response), status_code=response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise OCRUnavailable("engine returned a malformed response") from exc


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error", {})
        code = error.get("code", "engine_error")
        return f"{code} (HTTP {response.status_code})"
    except ValueError:
        return f"engine_error (HTTP {response.status_code})"


def _parse_bundle(content: bytes, source_sha256: str) -> OCRResult:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            result = json.loads(bundle.read("result.json").decode("utf-8"))
            text = bundle.read("text.txt").decode("utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, ValueError) as exc:
        raise OCRUnavailable("engine returned a malformed bundle") from exc

    pages = tuple(result.get("pages", ()))
    return OCRResult(
        engine=result.get("engine", ""),
        schema_version=result.get("schema_version", ""),
        page_count=int(result.get("page_count", len(pages))),
        duration_ms=int(result.get("duration_ms", 0)),
        text=text,
        pages=pages,
        source_sha256=source_sha256 or result.get("source", {}).get("sha256", ""),
    )
