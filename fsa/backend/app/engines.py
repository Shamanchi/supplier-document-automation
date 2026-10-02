"""Каталог локальных OCR-сервисов.

Все шесть движков реализуют один и тот же внешний контракт (`../API_CONTRACT.md`):
`POST /v1/ocr` с multipart-полем `file`, ответ — ZIP с `result.json` и
`text.txt`. Поэтому приложение выбирает движок по имени и не знает о нём
ничего, кроме адреса и ключа.
"""

from __future__ import annotations

from dataclasses import dataclass

# Порты закреплены за движками в их `.env.example` и docker-compose.
DEFAULT_PORTS: dict[str, int] = {
    "paddle": 8101,
    "tesseract": 8102,
    "easy": 8103,
    "rapidocr": 8104,
    "phocr": 8105,
    "surya": 8106,
}

KNOWN_ENGINES = tuple(DEFAULT_PORTS)


@dataclass(frozen=True, slots=True)
class EngineEndpoint:
    name: str
    base_url: str
    service_key: str

    @property
    def ocr_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/ocr"

    @property
    def info_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/info"

    @property
    def health_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/health"


def default_base_url(engine: str, host: str) -> str:
    """Адрес движка по умолчанию.

    В Docker-сети именем хоста служит имя сервиса (`docprice-ocr-paddle`),
    локально — `127.0.0.1` с закреплённым портом. Явный `OCR_<ENGINE>_URL`
    всегда важнее этого умолчания.
    """
    port = DEFAULT_PORTS[engine]
    return f"http://{host}:{port}"
