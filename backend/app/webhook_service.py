"""Signed terminal webhook delivery with SSRF-resistant destination checks."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
import ssl
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.import_auth import webhook_secret_for_client
from app.models import PriceImport, PriceImportWebhookDelivery


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CallbackDestination:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]
    request_target: str


def resolve_callback_destination(url: str) -> CallbackDestination:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("callback_url должен быть публичным HTTPS URL без credentials")
    hostname = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or 443
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("callback_url hostname не разрешается") from exc
    if not addresses:
        raise ValueError("callback_url hostname не разрешается")
    verified: list[str] = []
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("callback_url указывает на непубличный адрес")
        canonical = str(ip)
        if canonical not in verified:
            verified.append(canonical)
    target = quote(
        parsed.path or "/",
        safe="/%:@-._~!$&'()*+,;=",
    )
    if parsed.query:
        target += "?" + quote(parsed.query, safe="%:@-._~!$&'()*+,;=/?")
    return CallbackDestination(url, hostname, port, tuple(verified), target)


def validate_callback_url(url: str) -> str:
    return resolve_callback_destination(url).url


async def _post_pinned_https(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST to the verified IP while retaining original TLS SNI and Host.

    This closes the DNS-rebinding gap created by validating a hostname and
    then asking a generic HTTP client to resolve it a second time.
    """

    destination = await asyncio.to_thread(resolve_callback_destination, url)
    host_header = destination.hostname
    if destination.port != 443:
        host_header = f"{host_header}:{destination.port}"
    request_headers = {
        "Host": host_header,
        **headers,
        "Content-Length": str(len(body)),
        "Connection": "close",
    }
    encoded_headers = "".join(
        f"{name}: {value}\r\n" for name, value in request_headers.items()
    ).encode("ascii")
    request = (
        f"POST {destination.request_target} HTTP/1.1\r\n".encode("ascii")
        + encoded_headers
        + b"\r\n"
        + body
    )
    last_error: Exception | None = None
    for address in destination.addresses:
        writer = None
        try:
            async with asyncio.timeout(settings.price_import_webhook_timeout_seconds):
                reader, writer = await asyncio.open_connection(
                    address,
                    destination.port,
                    ssl=ssl.create_default_context(),
                    server_hostname=destination.hostname,
                )
                writer.write(request)
                await writer.drain()
                status_line = await reader.readline()
                parts = status_line.decode("ascii", errors="replace").strip().split(" ", 2)
                if len(parts) < 2 or not parts[0].startswith("HTTP/"):
                    raise OSError("Некорректный HTTP-ответ webhook")
                status_code = int(parts[1])
                for _ in range(100):
                    if await reader.readline() in {b"\r\n", b"\n", b""}:
                        break
                else:
                    raise OSError("Слишком много HTTP-заголовков webhook")
                return status_code
        except (OSError, TimeoutError, ssl.SSLError, ValueError) as exc:
            last_error = exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
    raise OSError(type(last_error).__name__ if last_error else "Webhook connection failed")


def sign_webhook_payload(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return f"v1={digest.hexdigest()}"


async def deliver_webhook_once(
    session: AsyncSession, delivery_id: uuid.UUID
) -> tuple[bool, bool]:
    delivery = await session.get(PriceImportWebhookDelivery, delivery_id)
    if delivery is None or delivery.status == "delivered":
        return True, False
    price_import = await session.get(PriceImport, delivery.price_import_id)
    if price_import is None:
        delivery.status = "failed"
        delivery.last_error = "PriceImport не найден"
        await session.commit()
        return False, False
    try:
        await asyncio.to_thread(validate_callback_url, delivery.callback_url)
    except (OSError, ValueError) as exc:
        delivery.status = "failed"
        delivery.last_error = str(exc)
        await session.commit()
        return False, False
    payload = {
        "event": "price_import.terminal",
        "delivery_id": str(delivery.id),
        "import_id": str(price_import.id),
        "status": price_import.status,
        "stage": price_import.stage,
        "progress_percent": price_import.progress_percent,
        "error_code": price_import.error_code,
        "error_message": price_import.error_message,
        "completeness": price_import.completeness_summary,
        "telemetry": price_import.telemetry_summary,
        "finished_at": (
            price_import.finished_at.isoformat() if price_import.finished_at else None
        ),
        "status_url": f"/api/v1/price-imports/{price_import.id}",
        "result_url": f"/api/v1/price-imports/{price_import.id}/result",
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(_now().timestamp()))
    secret = webhook_secret_for_client(price_import.service_client_id)
    headers = {
        "Content-Type": "application/json",
        "X-Price-Import-Event": "price_import.terminal",
        "X-Price-Import-Delivery": str(delivery.id),
        "X-Price-Import-Timestamp": timestamp,
        "X-Price-Import-Signature": sign_webhook_payload(secret, timestamp, body),
    }
    delivery.attempt_count += 1
    try:
        status_code = await _post_pinned_https(delivery.callback_url, body, headers)
        delivery.last_http_status = status_code
        if 200 <= status_code < 300:
            delivery.status = "delivered"
            delivery.delivered_at = _now()
            delivery.last_error = None
            await session.commit()
            return True, False
        delivery.last_error = f"HTTP {status_code}"
        retryable = status_code == 429 or status_code >= 500
    except ValueError as exc:
        delivery.last_error = str(exc)
        retryable = False
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        delivery.last_error = type(exc).__name__
        retryable = True
    if delivery.attempt_count >= 5 or not retryable:
        delivery.status = "failed"
        delivery.next_attempt_at = None
        retryable = False
    else:
        delivery.status = "retrying"
        delivery.next_attempt_at = _now() + timedelta(seconds=2 ** delivery.attempt_count)
    await session.commit()
    return False, retryable
