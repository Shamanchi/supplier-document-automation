"""Provider ports and safe adapters for OCR and LLM extraction."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_LLM_RESPONSE_BYTES = 1024 * 1024
MAX_RENDERED_PAGE_PX = 4000


class ProviderError(RuntimeError):
    transient = False


class ProviderTransientError(ProviderError):
    transient = True


class ProviderContractError(ProviderError):
    pass


@dataclass(frozen=True)
class OcrOptions:
    language: str
    timeout_seconds: int


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    bbox: dict[str, int]


@dataclass(frozen=True)
class OcrPageResult:
    provider: str
    model: str
    width_px: int | None
    height_px: int | None
    lines: tuple[OcrLine, ...]


class PdfPageRenderer(Protocol):
    async def render(self, pdf: bytes, page_no: int, *, dpi: int) -> bytes: ...


class OcrProvider(Protocol):
    async def recognize(
        self, page_image: bytes, options: OcrOptions
    ) -> OcrPageResult: ...


@dataclass(frozen=True)
class LlmFragment:
    fragment_id: str
    text: str


@dataclass(frozen=True)
class LlmCandidate:
    fragment_id: str
    name: str
    value: str
    unit: str | None
    quote: str
    confidence: float


class LlmExtractionProvider(Protocol):
    provider: str
    model: str
    prompt_version: str

    async def extract(
        self,
        fragments: list[LlmFragment],
        *,
        correlation_id: str,
        guidance: str | None = None,
    ) -> list[LlmCandidate]: ...


class PopplerPdfRenderer:
    async def render(self, pdf: bytes, page_no: int, *, dpi: int) -> bytes:
        return await asyncio.to_thread(self._render, pdf, page_no, dpi)

    @staticmethod
    def _render(pdf: bytes, page_no: int, dpi: int) -> bytes:
        try:
            with tempfile.TemporaryDirectory(prefix="docprice-ocr-") as directory:
                root = Path(directory)
                source = root / "source.pdf"
                target = root / "page"
                source.write_bytes(pdf)
                completed = subprocess.run(
                    [
                        "pdftoppm",
                        "-f",
                        str(page_no),
                        "-l",
                        str(page_no),
                        "-r",
                        str(dpi),
                        "-png",
                        "-scale-to",
                        str(MAX_RENDERED_PAGE_PX),
                        "-singlefile",
                        str(source),
                        str(target),
                    ],
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
                rendered = target.with_suffix(".png")
                if completed.returncode != 0 or not rendered.exists():
                    raise ProviderContractError("PDF_PAGE_RENDER_FAILED")
                return rendered.read_bytes()
        except FileNotFoundError as exc:
            raise ProviderContractError("PDF_RENDERER_UNAVAILABLE") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderTransientError("PDF_PAGE_RENDER_TIMEOUT") from exc


class TesseractOcrProvider:
    provider = "tesseract"
    model = "tesseract-5"

    async def recognize(
        self, page_image: bytes, options: OcrOptions
    ) -> OcrPageResult:
        return await asyncio.to_thread(self._recognize, page_image, options)

    @staticmethod
    def _recognize(page_image: bytes, options: OcrOptions) -> OcrPageResult:
        try:
            completed = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", options.language, "tsv"],
                input=page_image,
                check=False,
                capture_output=True,
                timeout=options.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ProviderContractError("OCR_PROVIDER_UNAVAILABLE") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderTransientError("OCR_PROVIDER_TIMEOUT") from exc
        if completed.returncode != 0:
            raise ProviderContractError("OCR_PROVIDER_FAILED")

        reader = csv.DictReader(
            io.StringIO(completed.stdout.decode("utf-8", errors="replace")),
            delimiter="\t",
        )
        grouped: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
        width: int | None = None
        height: int | None = None
        for row in reader:
            if row.get("level") == "1":
                width = _integer(row.get("width"))
                height = _integer(row.get("height"))
            text = (row.get("text") or "").strip()
            if row.get("level") != "5" or not text:
                continue
            key = (
                row.get("page_num") or "",
                row.get("block_num") or "",
                row.get("par_num") or "",
                row.get("line_num") or "",
            )
            grouped.setdefault(key, []).append(row)

        lines: list[OcrLine] = []
        for words in grouped.values():
            line_text = " ".join(
                (word.get("text") or "").strip() for word in words
            ).strip()
            confidences = [
                value
                for value in (_confidence(word.get("conf")) for word in words)
                if value is not None
            ]
            left = min(_integer(word.get("left")) or 0 for word in words)
            top = min(_integer(word.get("top")) or 0 for word in words)
            right = max(
                (_integer(word.get("left")) or 0)
                + (_integer(word.get("width")) or 0)
                for word in words
            )
            bottom = max(
                (_integer(word.get("top")) or 0)
                + (_integer(word.get("height")) or 0)
                for word in words
            )
            if line_text:
                lines.append(
                    OcrLine(
                        text=line_text,
                        confidence=(
                            sum(confidences) / len(confidences)
                            if confidences
                            else 0.0
                        ),
                        bbox={
                            "x": left,
                            "y": top,
                            "width": max(0, right - left),
                            "height": max(0, bottom - top),
                        },
                    )
                )
        return OcrPageResult(
            provider=TesseractOcrProvider.provider,
            model=TesseractOcrProvider.model,
            width_px=width,
            height_px=height,
            lines=tuple(lines),
        )


class _LlmCandidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fragment_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=160)
    value_text: str = Field(min_length=1, max_length=500)
    unit_text: str | None = Field(default=None, max_length=100)
    quote: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(default=0.7, ge=0, le=1)


class _LlmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameters: list[_LlmCandidatePayload]


class OpenAiCompatibleLlmProvider:
    provider = "openai-compatible"

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        prompt_version: str,
        timeout_seconds: int,
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self.model = model
        self.prompt_version = prompt_version
        self._timeout_seconds = timeout_seconds

    async def extract(
        self,
        fragments: list[LlmFragment],
        *,
        correlation_id: str,
        guidance: str | None = None,
    ) -> list[LlmCandidate]:
        return await asyncio.to_thread(self._extract, fragments, correlation_id, guidance)

    def _extract(
        self,
        fragments: list[LlmFragment],
        correlation_id: str,
        guidance: str | None,
    ) -> list[LlmCandidate]:
        prompt = {
            "task": (
                "Extract technical parameters only from supplied fragments. "
                "Every quote, value and unit must be copied verbatim."
            ),
            "response_schema": {
                "parameters": [
                    {
                        "fragment_id": "string",
                        "name": "string",
                        "value_text": "string",
                        "unit_text": "string|null",
                        "quote": "string",
                        "confidence": "0..1",
                    }
                ]
            },
            "fragments": [
                {"fragment_id": fragment.fragment_id, "text": fragment.text}
                for fragment in fragments
            ],
        }
        messages = [
            {
                "role": "system",
                "content": "Return JSON only. Never infer facts absent from a fragment.",
            }
        ]
        if guidance:
            messages.append({"role": "system", "content": guidance})
        messages.append(
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False),
            }
        )
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "messages": messages,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "X-Correlation-ID": correlation_id,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw_response = response.read(MAX_LLM_RESPONSE_BYTES + 1)
                if len(raw_response) > MAX_LLM_RESPONSE_BYTES:
                    raise ProviderContractError("LLM_PROVIDER_RESPONSE_TOO_LARGE")
                envelope = json.loads(raw_response.decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise ProviderTransientError("LLM_PROVIDER_TRANSIENT_ERROR") from exc
            raise ProviderContractError("LLM_PROVIDER_REJECTED_REQUEST") from exc
        except (TimeoutError, URLError) as exc:
            raise ProviderTransientError("LLM_PROVIDER_UNAVAILABLE") from exc
        except json.JSONDecodeError as exc:
            raise ProviderContractError("LLM_PROVIDER_INVALID_JSON") from exc

        try:
            content = envelope["choices"][0]["message"]["content"]
            payload = _LlmResponse.model_validate_json(_json_content(content))
        except (KeyError, IndexError, TypeError, ValidationError) as exc:
            raise ProviderContractError("LLM_PROVIDER_INVALID_PAYLOAD") from exc
        return [
            LlmCandidate(
                fragment_id=item.fragment_id,
                name=item.name,
                value=item.value_text,
                unit=item.unit_text,
                quote=item.quote,
                confidence=item.confidence,
            )
            for item in payload.parameters
        ]


def _json_content(value: object) -> str:
    if not isinstance(value, str):
        raise ProviderContractError("LLM_PROVIDER_INVALID_PAYLOAD")
    stripped = value.strip()
    fence = chr(96) * 3
    if stripped.startswith(fence):
        stripped = stripped.removeprefix(f"{fence}json").removeprefix(fence)
        stripped = stripped.removesuffix(fence).strip()
    return stripped


def _integer(value: str | None) -> int | None:
    try:
        return int(value or "")
    except ValueError:
        return None


def _confidence(value: str | None) -> float | None:
    try:
        parsed = float(value or "")
    except ValueError:
        return None
    if parsed < 0:
        return None
    return min(1.0, parsed / 100)
