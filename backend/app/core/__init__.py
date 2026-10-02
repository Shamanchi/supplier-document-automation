"""Базовый слой публичного API: соглашения, общие для всех модулей.

Состав:
  * `schemas`     — BaseSchema (camelCase наружу), типы Money/Rate/Confidence;
  * `errors`      — envelope Фазы 3 и Problem Details витрины Фазы 2;
  * `pagination`  — cursor (Ф3) и page/page_size (Ф2);
  * `concurrency` — Idempotency-Key, ETag/If-Match;
  * `middleware`  — сквозной correlationId.

Соглашения описаны в docs/api/API_CONVENTIONS.md, границы контуров —
в docs/architecture/CONTOUR_BOUNDARIES.md.

Перенесено из supplier-document-automation без изменений (docs/migration/01-backend-core.md).
"""
