"""Экспорт/импорт правил разбора (и их поставщиков) как JSON-сида.

Зачем: правила, наработанные на одном стенде (локальный прогон корпуса,
ручные подтверждения маппинга), должны переезжать на другой стенд вместе с
приложением. Сид самодостаточен: поставщик задаётся кодом/именем, не UUID,
а когда сид несёт интеграционный конверт (``project``, ``service_clients``,
``suppliers``), поставщик создаётся service-scoped — с тем же
``service_client_id + external_id``, по которому engineering upload ищет и
создаёт поставщика при загрузке прайса.

    python -m app.seed export > parse_rules_seed.json
    python -m app.seed import /path/to/parse_rules_seed.json

Импорт идемпотентен: правило с тем же (header_hash, код поставщика, revision)
не дублируется; поставщики создаются по коду при отсутствии и привязываются к
клиенту по ``service_client_ref``, если он указан. Правила без ``project_slug``
попадают в проект из контекста сида (по умолчанию original-prices — тот же
проект, куда пишет инженерная загрузка). Валидные правила из сида
(``validation.passed == true``) считаются подготовленными к работе и
переводятся в ``active`` также при повторном импорте уже существующей записи.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import engine, SessionLocal as session_factory
from app.models import (
    ORIGINAL_PROJECT_ID,
    ParseRule,
    Project,
    ServiceClient,
    Supplier,
)

# Скоуп инженерной загрузки под UI-администратором: поставщик, созданный
# price-imports API, привязан к внутреннему UI-клиенту (is_internal=True) и
# находится по (service_client_id, external_id). Сид повторяет эту identity.
_INTERNAL_UI_REF = "internal-ui"
# Внутренние клиенты не получают bearer-ключ; digest-заглушка никогда не
# участвует в аутентификации (аутентификация разбирает только ключи plk_*).
_DISABLED_CLIENT_DIGEST = "0" * 64


def _service_client_ref(client: ServiceClient | None) -> str | None:
    """Stable client ref for an exported rule row.

    ``internal-ui`` resolves back to the single internal UI client; other
    clients keep their ``key_prefix`` as the scope handle.
    """
    if client is None:
        return None
    if client.is_internal:
        return _INTERNAL_UI_REF
    return client.key_prefix or None


async def export_rules() -> list[dict]:
    async with session_factory() as session:
        rules = (await session.execute(select(ParseRule))).scalars().all()
        suppliers = {
            s.id: s for s in (await session.execute(select(Supplier))).scalars().all()
        }
        projects = {
            p.id: p for p in (await session.execute(select(Project))).scalars().all()
        }
        clients = {
            c.id: c for c in (await session.execute(select(ServiceClient))).scalars().all()
        }
    out = []
    for r in rules:
        sup = suppliers.get(r.supplier_id)
        out.append(
            {
                "supplier_code": sup.code if sup else None,
                "supplier_name": sup.name if sup else None,
                "supplier_external_id": sup.external_id if sup else None,
                "supplier_service_client_ref": _service_client_ref(
                    clients.get(sup.service_client_id) if sup is not None else None
                ),
                "project_slug": projects[r.project_id].slug if r.project_id in projects else None,
                "name": r.name,
                "loader": r.loader,
                "header_hash": r.header_hash,
                "fingerprint": r.fingerprint,
                "recipe": r.recipe,
                "revision": r.revision,
                "status": r.status,
                "provenance": r.provenance,
                "validation": r.validation,
            }
        )
    return out


async def _get_or_create_supplier(
    session: AsyncSession,
    code: str,
    name: str | None,
    *,
    service_client: ServiceClient | None = None,
    external_id: str | None = None,
) -> Supplier:
    """Supplier with the same identity engineering upload uses.

    Upload looks a supplier up by ``(service_client_id, external_id)`` and
    creates it service-scoped when absent; seed rows carry the same pair, so a
    later upload of the same supplier reuses the seeded row and its rules are
    found automatically. Legacy seed suppliers created unscoped are upgraded in
    place; when both a legacy unscoped row and the scoped upload row exist, the
    legacy row's rules are re-pointed to the scoped row.
    """
    existing = (
        await session.execute(select(Supplier).where(Supplier.code == code))
    ).scalar_one_or_none()
    if service_client is not None and external_id:
        scoped = (
            await session.execute(
                select(Supplier).where(
                    Supplier.service_client_id == service_client.id,
                    Supplier.external_id == external_id,
                )
            )
        ).scalar_one_or_none()
        if scoped is not None:
            if (
                existing is not None
                and existing.id != scoped.id
                and existing.service_client_id is None
            ):
                await _repoint_rule_supplier(
                    session,
                    from_supplier_id=existing.id,
                    to_supplier_id=scoped.id,
                )
            return scoped
        if existing is not None:
            if existing.service_client_id is None:
                existing.service_client_id = service_client.id
                existing.external_id = external_id
                return existing
            # Unscoped fallback for legacy data: an already-scoped row with a
            # different identity stays the code owner.
            return existing
        supplier = Supplier(
            code=code,
            name=name or code,
            service_client_id=service_client.id,
            external_id=external_id,
        )
        session.add(supplier)
        await session.flush()
        return supplier
    if existing is not None:
        return existing
    supplier = Supplier(code=code, name=name or code)
    session.add(supplier)
    await session.flush()
    return supplier


def _seed_status(row: dict) -> str:
    """Return the lifecycle state that is safe for a shipped seed row.

    The corpus audit stores a passed validation snapshot for rules that were
    checked against a real file. Those rules must be usable immediately after
    deployment; an old seed may still say ``draft``, so the passed snapshot is
    the source of truth for promotion. Unvalidated rows remain drafts.
    """
    requested = row.get("status", "draft")
    if requested != "deprecated" and (row.get("validation") or {}).get("passed") is True:
        return "active"
    return requested


_STATUS_PRIORITY = {"active": 0, "draft": 1, "deprecated": 2}
_PROVENANCE_PRIORITY = {"user_confirmed": 0, "manual": 1, "auto": 2}


def _existing_rule_priority(rule: ParseRule) -> tuple[int, int, int, float, str]:
    """Pick one predictable target while upgrading a legacy duplicate group.

    The database migration keeps this condition from recurring.  This ordering
    makes seed import resilient on an old database too: a usable current rule
    wins over a deprecated one, then a user-reviewed rule wins over an auto one.
    """
    return (
        _STATUS_PRIORITY.get(rule.status, 3),
        _PROVENANCE_PRIORITY.get(rule.provenance, 3),
        0 if (rule.validation or {}).get("passed") is True else 1,
        -(rule.updated_at.timestamp() if rule.updated_at is not None else 0),
        str(rule.id),
    )


def _spec_int(spec: dict | None, key: str, default: int) -> int:
    value = spec.get(key) if spec else None
    return value if isinstance(value, int) and not isinstance(value, bool) else default


async def _get_or_create_service_client(
    session: AsyncSession,
    *,
    ref: str,
    spec: dict | None = None,
) -> ServiceClient | None:
    """Return the service client a seeded supplier is scoped to.

    The internal UI client is matched by the same single-internal invariant the
    engineering upload relies on; other refs match by ``key_prefix``. Clients
    created from a seed are disabled (no usable bearer key) and only provide a
    stable supplier/rule scope.
    """
    kind = str((spec or {}).get("kind") or "") if spec else ""
    is_internal = kind == "internal_ui" or ref == _INTERNAL_UI_REF
    if is_internal:
        existing = (
            await session.execute(
                select(ServiceClient).where(ServiceClient.is_internal.is_(True))
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        client = ServiceClient(
            name=str((spec or {}).get("name") or "Внутренний интерфейс"),
            key_prefix=_INTERNAL_UI_REF,
            key_digest=_DISABLED_CLIENT_DIGEST,
            is_internal=True,
            is_active=False,
            requests_per_minute=_spec_int(spec, "requests_per_minute", 60),
            max_concurrent_imports=_spec_int(spec, "max_concurrent_imports", 4),
            max_input_tokens_per_import=_spec_int(spec, "max_input_tokens_per_import", 5_000_000),
            max_output_tokens_per_import=_spec_int(spec, "max_output_tokens_per_import", 1_000_000),
        )
        session.add(client)
        await session.flush()
        return client
    existing = (
        await session.execute(
            select(ServiceClient).where(ServiceClient.key_prefix == ref)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    client = ServiceClient(
        name=str((spec or {}).get("name") or ref),
        key_prefix=ref[:32],
        key_digest=_DISABLED_CLIENT_DIGEST,
        is_internal=False,
        is_active=False,
        requests_per_minute=_spec_int(spec, "requests_per_minute", 60),
        max_concurrent_imports=_spec_int(spec, "max_concurrent_imports", 4),
        max_input_tokens_per_import=_spec_int(spec, "max_input_tokens_per_import", 5_000_000),
        max_output_tokens_per_import=_spec_int(spec, "max_output_tokens_per_import", 1_000_000),
    )
    session.add(client)
    await session.flush()
    return client


async def _service_client_for_ref(
    session: AsyncSession,
    cache: dict[str, ServiceClient | None],
    ref: str | None,
    spec: dict | None = None,
) -> ServiceClient | None:
    if not ref:
        return None
    if ref in cache:
        return cache[ref]
    client = await _get_or_create_service_client(session, ref=ref, spec=spec)
    cache[ref] = client
    return client


async def _repoint_rule_supplier(
    session: AsyncSession,
    *,
    from_supplier_id,
    to_supplier_id,
) -> int:
    """Re-bind legacy rules of an unscoped seed supplier to the scoped one.

    Rows that already have an identical rule on the target supplier stay put,
    so repeated imports do not manufacture duplicates on the canonical row.
    """
    legacy_rules = (
        (
            await session.execute(
                select(ParseRule).where(ParseRule.supplier_id == from_supplier_id)
            )
        )
        .scalars()
        .all()
    )
    if not legacy_rules:
        return 0
    target_keys = {
        (rule.project_id, rule.header_hash, rule.revision)
        for rule in (
            (
                await session.execute(
                    select(ParseRule).where(ParseRule.supplier_id == to_supplier_id)
                )
            )
            .scalars()
            .all()
        )
    }
    moved = 0
    for rule in legacy_rules:
        if (rule.project_id, rule.header_hash, rule.revision) in target_keys:
            continue
        rule.supplier_id = to_supplier_id
        moved += 1
    return moved


async def _context_project_id(session: AsyncSession, payload) -> uuid.UUID:
    """Default project for rules without an explicit per-row ``project_slug``.

    Integration-export seeds carry their own project context; bare seeds land
    in original-prices — the project engineering upload writes into. The
    original-prices row is created lazily so a fresh database is ready for
    uploads immediately.
    """
    if isinstance(payload, dict):
        info = payload.get("project")
        if isinstance(info, dict) and info.get("slug"):
            slug = str(info["slug"]).strip()
            project = (
                await session.execute(select(Project).where(Project.slug == slug))
            ).scalar_one_or_none()
            if project is None:
                if slug == "original-prices":
                    project = Project(
                        id=ORIGINAL_PROJECT_ID,
                        slug=slug,
                        name=str(info.get("name") or "Original prices"),
                        description=info.get("description"),
                        is_default=True,
                        is_archived=bool(info.get("is_archived")),
                    )
                else:
                    project = Project(
                        slug=slug,
                        name=str(info.get("name") or slug),
                        description=info.get("description"),
                        is_archived=bool(info.get("is_archived")),
                    )
                session.add(project)
                await session.flush()
            return project.id
    project = await session.get(Project, ORIGINAL_PROJECT_ID)
    if project is None:
        session.add(
            Project(
                id=ORIGINAL_PROJECT_ID,
                slug="original-prices",
                name="Original prices",
                is_default=True,
            )
        )
        await session.flush()
    return ORIGINAL_PROJECT_ID


async def import_rules(path: str) -> tuple[int, int, int]:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = payload.get("rules", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("seed must be a rule list or an integration export with a rules list")
    supplier_specs: dict[str, dict] = {}
    client_specs: dict[str, dict] = {}
    if isinstance(payload, dict):
        supplier_specs = {
            spec["code"]: spec
            for spec in (payload.get("suppliers") or [])
            if isinstance(spec, dict) and spec.get("code")
        }
        client_specs = {
            spec["ref"]: spec
            for spec in (payload.get("service_clients") or [])
            if isinstance(spec, dict) and spec.get("ref")
        }
    created = skipped = updated = 0
    async with session_factory() as session:
        default_project_id = await _context_project_id(session, payload)
        client_cache: dict[str, ServiceClient | None] = {}
        for row in rows:
            project_id = default_project_id
            if row.get("project_slug"):
                project = (
                    await session.execute(
                        select(Project).where(Project.slug == row["project_slug"])
                    )
                ).scalar_one_or_none()
                if project is None:
                    raise ValueError(f"Проект с кодом {row['project_slug']} не найден")
                project_id = project.id
            supplier_id = None
            supplier_code = row.get("supplier_code")
            if supplier_code:
                spec = supplier_specs.get(supplier_code)
                if spec is not None:
                    ref = spec.get("service_client_ref")
                    external_id = spec.get("external_id")
                    fallback_name = spec.get("name")
                else:
                    ref = row.get("supplier_service_client_ref")
                    external_id = row.get("supplier_external_id")
                    fallback_name = None
                ref = ref if isinstance(ref, str) and ref.strip() else None
                external_id = (
                    external_id if isinstance(external_id, str) and external_id.strip() else None
                )
                client = await _service_client_for_ref(
                    session,
                    client_cache,
                    ref,
                    client_specs.get(ref) if ref else None,
                )
                supplier = await _get_or_create_supplier(
                    session,
                    supplier_code,
                    row.get("supplier_name") or fallback_name,
                    service_client=client,
                    external_id=external_id,
                )
                supplier_id = supplier.id
            existing_rules = (
                await session.execute(
                    select(ParseRule).where(
                        ParseRule.project_id == project_id,
                        ParseRule.header_hash == row["header_hash"],
                        ParseRule.supplier_id == supplier_id,
                        ParseRule.revision == row.get("revision", 1),
                    )
                )
            ).scalars().all()
            exists = min(existing_rules, key=_existing_rule_priority, default=None)
            if exists:
                changed = False
                seed_status = _seed_status(row)
                if (
                    seed_status == "active"
                    and exists.status != "active"
                    and exists.status != "deprecated"
                ):
                    exists.status = "active"
                    changed = True
                if exists.validation is None and row.get("validation") is not None:
                    exists.validation = row["validation"]
                    changed = True
                if changed:
                    updated += 1
                else:
                    skipped += 1
                continue
            session.add(
                ParseRule(
                    project_id=project_id,
                    supplier_id=supplier_id,
                    name=row["name"],
                    loader=row["loader"],
                    header_hash=row["header_hash"],
                    fingerprint=row["fingerprint"],
                    recipe=row["recipe"],
                    revision=row.get("revision", 1),
                    status=_seed_status(row),
                    provenance=row.get("provenance", "manual"),
                    validation=row.get("validation"),
                )
            )
            created += 1
        await session.commit()
    return created, skipped, updated


async def _main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("export", "import"):
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    if sys.argv[1] == "export":
        rules = await export_rules()
        json.dump(rules, sys.stdout, ensure_ascii=False, indent=1)
        print(f"\nэкспортировано правил: {len(rules)}", file=sys.stderr)
    else:
        if len(sys.argv) != 3:
            print("usage: python -m app.seed import <file.json>", file=sys.stderr)
            raise SystemExit(2)
        created, skipped, updated = await import_rules(sys.argv[2])
        print(
            f"создано: {created}, обновлено: {updated}, уже было без изменений: {skipped}",
            file=sys.stderr,
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
