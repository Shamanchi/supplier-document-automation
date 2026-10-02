"""Обёртка MinIO.

Загрузка идёт через внутренний endpoint, presigned-ссылки подписываются от
публичного: браузер снаружи compose-сети до внутреннего имени не достучится.
"""

from __future__ import annotations

import io
import re
from datetime import timedelta
from functools import lru_cache

from minio import Minio

from app.config import settings

_SAFE_RE = re.compile(r"[^A-Za-z0-9А-Яа-яЁё._-]+")


def safe_name(filename: str) -> str:
    name = _SAFE_RE.sub("_", (filename or "").strip())
    return name.strip("._") or "file"


@lru_cache(maxsize=1)
def _client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


@lru_cache(maxsize=1)
def _public_client() -> Minio:
    # region задан явно: иначе minio-py дёргает GetBucketLocation по
    # публичному адресу, который из контейнера недоступен. На подпись region
    # не влияет.
    return Minio(
        settings.minio_public_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
        region="us-east-1",
    )


def ensure_buckets() -> None:
    client = _client()
    if not client.bucket_exists(settings.minio_bucket_documents):
        client.make_bucket(settings.minio_bucket_documents)


def document_key(sha256: str, filename: str) -> str:
    """Ключ по содержимому: один и тот же файл не хранится дважды."""
    return f"sha256/{sha256}/{safe_name(filename)}"


def put_document(data: bytes, sha256: str, filename: str, content_type: str) -> str:
    key = document_key(sha256, filename)
    _client().put_object(
        settings.minio_bucket_documents,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type or "application/octet-stream",
    )
    return key


def get_document(storage_key: str) -> bytes:
    response = _client().get_object(settings.minio_bucket_documents, storage_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def presigned_get(storage_key: str, expires_seconds: int | None = None) -> str:
    expires = timedelta(seconds=expires_seconds or settings.presign_expiry_seconds)
    return _public_client().presigned_get_object(
        settings.minio_bucket_documents, storage_key, expires=expires
    )
