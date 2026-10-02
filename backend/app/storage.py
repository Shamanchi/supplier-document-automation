"""MinIO wrapper.

Uploads go through the internal endpoint (minio:9000 inside compose);
presigned URLs are generated against MINIO_PUBLIC_ENDPOINT so a browser
outside the compose network can open them.
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
    name = _SAFE_RE.sub("_", filename.strip())
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
    # region задан явно: без него minio-py делает сетевой GetBucketLocation,
    # а публичный endpoint из контейнера недоступен — подпись же от region не страдает.
    return Minio(
        settings.minio_public_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
        region="us-east-1",
    )


def ensure_buckets() -> None:
    client = _client()
    for bucket in (settings.minio_bucket_price_files, settings.minio_bucket_product_docs):
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)


def put_price_file(data: bytes, sha256: str, filename: str, content_type: str) -> str:
    """Store a blob under sha256/<hash>/<safe_name>; returns the storage key."""
    key = f"sha256/{sha256}/{safe_name(filename)}"
    _client().put_object(
        settings.minio_bucket_price_files,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type or "application/octet-stream",
    )
    return key


def put_report(data: bytes, key: str, content_type: str) -> str:
    """Store a report artifact (corpus run md/json) in the price-files bucket."""
    _client().put_object(
        settings.minio_bucket_price_files,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    return key


def get_price_file(storage_key: str) -> bytes:
    resp = _client().get_object(settings.minio_bucket_price_files, storage_key)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def presigned_get(storage_key: str, expires_seconds: int | None = None) -> str:
    expires = timedelta(seconds=expires_seconds or settings.presign_expiry_seconds)
    return _public_client().presigned_get_object(
        settings.minio_bucket_price_files, storage_key, expires=expires
    )


def put_product_document(
    data: bytes, sha256: str, filename: str, content_type: str | None = None
) -> str:
    """Store an uploaded passport/certificate in the product-docs bucket."""
    key = f"sha256/{sha256}/{safe_name(filename)}"
    _client().put_object(
        settings.minio_bucket_product_docs,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type or "application/pdf",
    )
    return key


def get_product_document(storage_key: str) -> bytes:
    resp = _client().get_object(settings.minio_bucket_product_docs, storage_key)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def presigned_product_document(
    storage_key: str, expires_seconds: int | None = None
) -> str:
    expires = timedelta(seconds=expires_seconds or settings.presign_expiry_seconds)
    return _public_client().presigned_get_object(
        settings.minio_bucket_product_docs, storage_key, expires=expires
    )
