"""Порт объектного хранилища и его реализация поверх S3 (MinIO).

Порт нужен по двум причинам. Первая — тесты: поднимать MinIO ради проверки
правил загрузки незачем, достаточно подменить зависимость. Вторая — контракт
uploads требует карантина: файл сначала кладётся в отдельный бакет и переезжает
в основной только после проверок, а это операция хранилища, а не сервиса.

boto3 синхронный, поэтому вызовы уходят в пул потоков: иначе загрузка файла
блокировала бы event loop на всё время обращения к MinIO. Пул берётся у
starlette — тот же, в котором фреймворк исполняет синхронные обработчики,
поэтому его ограничитель считает и эти задачи тоже.
"""

from __future__ import annotations

from typing import Protocol

from starlette.concurrency import run_in_threadpool

from app.config import Settings


class StoragePort(Protocol):
    """Минимум операций, который нужен загрузке документов."""

    async def put(
        self, bucket: str, key: str, data: bytes, *, content_type: str
    ) -> None: ...

    async def get(self, bucket: str, key: str) -> bytes: ...

    async def move(
        self, *, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str
    ) -> None:
        """Перенос между бакетами: карантин → постоянное хранилище."""
        ...

    async def delete(self, bucket: str, key: str) -> None: ...


class S3Storage:
    """Реализация порта поверх S3-совместимого хранилища (MinIO).

    Клиент создаётся один раз на процесс: boto3-клиент потокобезопасен на
    чтение и переиспользует пул соединений.
    """

    def __init__(self, settings: Settings) -> None:
        # Импорт внутри конструктора: boto3 тянет botocore на ~30 МБ, а тестам
        # и alembic-миграциям он не нужен вовсе.
        import boto3

        # Шаг миграции 03: канон кред — поля фазы 2 (docs/migration/01-backend-core.md).
        # Поле minio_endpoint в compose фазы 2 записано без схемы («minio:9000»):
        # его ждёт legacy-клиент minio-py (app/storage.py). boto3 же требует
        # полный URL, поэтому схема добавляется здесь, а не в окружении.
        endpoint = settings.minio_endpoint
        if "://" not in endpoint:
            endpoint = ("https://" if settings.minio_secure else "http://") + endpoint
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            region_name=settings.minio_region,
        )

    async def put(
        self, bucket: str, key: str, data: bytes, *, content_type: str
    ) -> None:
        await run_in_threadpool(
            lambda: self._client.put_object(
                Bucket=bucket, Key=key, Body=data, ContentType=content_type
            )
        )

    async def get(self, bucket: str, key: str) -> bytes:
        def read() -> bytes:
            response = self._client.get_object(Bucket=bucket, Key=key)
            return response["Body"].read()

        return await run_in_threadpool(read)

    async def move(
        self, *, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str
    ) -> None:
        def copy_then_drop() -> None:
            self._client.copy_object(
                Bucket=dst_bucket,
                Key=dst_key,
                CopySource={"Bucket": src_bucket, "Key": src_key},
            )
            self._client.delete_object(Bucket=src_bucket, Key=src_key)

        await run_in_threadpool(copy_then_drop)

    async def delete(self, bucket: str, key: str) -> None:
        await run_in_threadpool(
            lambda: self._client.delete_object(Bucket=bucket, Key=key)
        )


class InMemoryStorage:
    """Хранилище в памяти процесса — для тестов и локального запуска без MinIO.

    Живёт здесь, а не в тестах: на нём же держится dev-режим без поднятого
    объектного хранилища, и подмена в тестах должна проверять ту же реализацию,
    что работает в этом режиме.
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def put(
        self, bucket: str, key: str, data: bytes, *, content_type: str
    ) -> None:
        self.objects[(bucket, key)] = data

    async def get(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]

    async def move(
        self, *, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str
    ) -> None:
        self.objects[(dst_bucket, dst_key)] = self.objects.pop((src_bucket, src_key))

    async def delete(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)
