"""Выгрузка канонической схемы в JSON.

Нужна потребителям, которые не импортируют этот пакет: price_loader_app,
фронтенд, внешние парсеры. Файл `schema.json` держится в репозитории и
проверяется тестом, чтобы не разъезжаться с кодом.

    python3 -m app.fsa.export_schema > app/fsa/schema.json
"""

from __future__ import annotations

import dataclasses
import json
import sys
import typing
from datetime import date
from enum import Enum

from . import schema as models

_EXPORTED = (
    models.RegistryCertificate,
    models.ExtractedDocument,
    models.SuccessorCandidate,
    models.Reconciliation,
    models.PositionDocumentLink,
)

_ENUMS = (
    models.CertificateStatus,
    models.DocumentClass,
    models.TextSource,
    models.Verdict,
    models.MatchedBy,
)


def _type_name(annotation: object) -> str:
    if annotation is date:
        return "date"
    origin = typing.get_origin(annotation)
    if origin is not None:
        args = [
            _type_name(arg)
            for arg in typing.get_args(annotation)
            if arg is not type(None)
        ]
        if origin is list or origin is tuple:
            return f"list[{args[0] if args else 'any'}]"
        return args[0] if len(args) == 1 else " | ".join(args)
    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            return annotation.__name__
        return annotation.__name__
    return str(annotation).replace("app.fsa.schema.", "")


def build() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": models.SCHEMA_VERSION,
        "enums": {
            enum.__name__: [member.value for member in enum] for enum in _ENUMS
        },
        "models": {},
    }
    for model in _EXPORTED:
        hints = typing.get_type_hints(model)
        payload["models"][model.__name__] = {  # type: ignore[index]
            "doc": (model.__doc__ or "").strip().splitlines()[0]
            if model.__doc__
            else "",
            "fields": {
                field.name: _type_name(hints.get(field.name, field.type))
                for field in dataclasses.fields(model)
            },
        }
    return payload


def main() -> int:
    json.dump(build(), sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
