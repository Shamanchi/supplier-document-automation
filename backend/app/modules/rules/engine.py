"""Детерминированный движок сравнения требования с фактом.

Здесь нет ни базы, ни LLM: на вход — два типизированных значения и политика
параметра, на выходе — статус, оператор и `explanation`. Так правила
проверяются golden-кейсами из DOMAIN_RULES_ADR §10 без поднятия окружения, а
вердикт остаётся объяснимым: каждое число в `explanation` видно менеджеру и
попадает в отчёт.

Что здесь сознательно **не** делается:

* **Никакого fuzzy.** Текст и материал сравниваются точно (после нормализации
  каталога); несовпадение даёт `REVIEW_REQUIRED`, а не `NON_COMPLIANT` —
  канон §15.3 разрешает fuzzy только как повод для проверки человеком.
* **Никаких float.** Только `Decimal`: 4.98 против 5 с допуском 2 % обязано
  давать один и тот же ответ на любой машине (§14.4).
* **Правила не зашиты в код как константы.** Таблица операторов, допуски по
  умолчанию и семантика диапазонов приходят снимком (`rules` из
  `rule_snapshots`), зафиксированным на момент запуска прогона: перезапуск
  через год обязан дать тот же результат (§15.8).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.modules.catalogs.normalize import normalize as normalize_text

#: Версия набора правил. Меняется вместе с содержимым `default_rules()`:
#: снимок неизменяем, новая версия правил — новая запись `rule_snapshots`.
RULE_VERSION = "rules-1.0.0"

#: Статусы сравнения (schema.sql: comparison_status).
COMPLIANT = "COMPLIANT"
NON_COMPLIANT = "NON_COMPLIANT"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
NOT_FOUND_IN_FACT = "NOT_FOUND_IN_FACT"
NOT_REQUIRED = "NOT_REQUIRED"

#: Уровни критичности (DOMAIN_RULES_ADR §3).
BLOCKER = "BLOCKER"
MAJOR = "MAJOR"
MINOR = "MINOR"

#: Источники допуска по убыванию приоритета (DOMAIN_RULES_ADR §2). Уровень
#: `CHECK_ITEM_OVERRIDE` появится вместе с ручными правками менеджера.
TOLERANCE_SOURCES = (
    "CHECK_ITEM_OVERRIDE",
    "EQUIPMENT_TEMPLATE",
    "CANONICAL_PARAMETER",
    "GLOBAL_DEFAULT",
    "NONE",
)


def default_rules() -> dict:
    """Содержимое снимка правил.

    Формат заранее рассчитан на будущий DSL (§15.9): узел сравнения описан как
    данные (`rule_type: COMPARE`), а не как имя Python-функции, и допускает
    `COMPOSITE` для AND/OR. Компилятора DSL в MVP нет — движок исполняет
    таблицу ниже, но формат менять под него не придётся.
    """
    return {
        "ruleVersion": RULE_VERSION,
        # Диапазон факта обязан покрывать диапазон ТЗ (DOMAIN_RULES_ADR §1).
        "rangeSemantics": "COVERAGE",
        # Оператор по типу значения, если шаблон типа оборудования молчит.
        "defaultOperators": {
            "NUMBER": "EQUAL",
            "RANGE": "RANGE",
            "BOOLEAN": "EQUAL",
            "SET": "SET_CONTAINS",
            "TEXT": "TEXT_MATCH",
            "MATERIAL": "MATERIAL_MATCH",
        },
        # Знак в самом требовании («≥ 30») сильнее оператора по умолчанию.
        "operatorTokens": {
            ">=": "GREATER_OR_EQUAL",
            "<=": "LESS_OR_EQUAL",
            ">": "GREATER",
            "<": "LESS",
        },
        # Глобального допуска нет: молчаливый допуск на всё — это тихое
        # смягчение проверки, его назначает администратор осознанно.
        "toleranceDefaults": {"global": None},
        "criticalityDefault": MAJOR,
        # Параметр без явного источника обязателен (DOMAIN_RULES_ADR §4).
        "mandatoryDefault": True,
        "nodes": [
            {
                "rule_type": "COMPARE",
                "applies_to": "*",
                "operator": "FROM_TEMPLATE_OR_DEFAULT",
                "tolerance": "FROM_HIERARCHY",
            }
        ],
    }


def rules_checksum(rules: dict) -> str:
    """Отпечаток набора правил: один и тот же набор не дублируется в базе."""
    canonical = json.dumps(rules, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Policy:
    """Как сравнивать конкретный параметр этой позиции.

    Собирается из шаблона типа оборудования и умолчаний снимка; поля
    `tolerance_source*` обязаны дойти до `explanation` и отчёта (§15.3.1).
    """

    operator: str | None = None
    tolerance_value: Decimal | None = None
    tolerance_type: str = "NONE"
    tolerance_source: str = "NONE"
    tolerance_source_id: str | None = None
    mandatory: bool = True
    criticality: str = MAJOR

    @property
    def counts_towards_verdict(self) -> bool:
        """MINOR-параметры показываются в отчёте, но не влияют на вердикт."""
        return self.mandatory and self.criticality != MINOR


def policy_from_template(
    template_operator: str | None, tolerance: dict | None
) -> Policy:
    """Политика из строки `parameter_templates`.

    Критичность и допуск живут в JSONB `tolerance`: отдельных колонок под них
    в `db/schema.sql` нет, а придумывать миграцию под данные, которые пока
    некому вводить, — хуже, чем документированное соглашение о формате.
    Формат полей — из DOMAIN_RULES_ADR §2 и §3.
    """
    payload = tolerance or {}
    value = _decimal_or_none(payload.get("tolerance_value"))
    kind = str(payload.get("tolerance_type") or "NONE").upper()
    criticality = str(payload.get("criticality") or MAJOR).upper()
    if criticality not in (BLOCKER, MAJOR, MINOR):
        criticality = MAJOR
    if value is None or kind not in ("ABSOLUTE", "PERCENT"):
        value, kind = None, "NONE"
    return Policy(
        operator=template_operator,
        tolerance_value=value,
        tolerance_type=kind,
        tolerance_source="EQUIPMENT_TEMPLATE" if value is not None else "NONE",
        mandatory=True,
        criticality=criticality,
    )


@dataclass(frozen=True)
class RuleOutcome:
    """Результат правила: статус, применённый оператор и объяснение."""

    status: str
    operator: str | None
    explanation: dict


def compare(
    requirement: dict | None,
    fact: dict | None,
    *,
    value_kind: str,
    policy: Policy,
    rules: dict,
) -> RuleOutcome:
    """Сравнивает требование ТЗ с фактом паспорта.

    `requirement`/`fact` — `typed_value` нормализованного параметра
    (`normalization.service._parse_value`); `None` означает, что значение не
    разобрано или стороны нет вовсе.
    """
    operator = _operator(requirement, value_kind, policy, rules)
    explain: dict = {
        "required": _operand(requirement),
        "fact": _operand(fact),
        "operator": operator,
        "ruleVersion": rules.get("ruleVersion", RULE_VERSION),
        "tolerance": {
            "value": _text(policy.tolerance_value),
            "type": policy.tolerance_type,
            "source": policy.tolerance_source,
            "sourceId": policy.tolerance_source_id,
        },
        "criticality": policy.criticality,
        "mandatory": policy.mandatory,
    }

    if requirement is None:
        # Факт без требования: строка EXTRA_FACT, менеджер решает, нужна ли она.
        return _outcome(NOT_REQUIRED, operator, explain, "REQUIREMENT_ABSENT", policy)
    if fact is None:
        return _outcome(
            NOT_FOUND_IN_FACT,
            operator,
            explain,
            "FACT_ABSENT",
            policy,
            skip_soften=True,
        )

    unit_issue = _unit_mismatch(requirement, fact)
    if unit_issue is not None:
        return _outcome(REVIEW_REQUIRED, operator, explain, unit_issue, policy)

    handler = _HANDLERS.get((requirement.get("kind"), fact.get("kind")))
    if handler is None:
        return _outcome(
            REVIEW_REQUIRED, operator, explain, "VALUE_KINDS_NOT_COMPARABLE", policy
        )
    return handler(requirement, fact, operator, policy, rules, explain)


# --- правила по типам значений ------------------------------------------


def _compare_numbers(
    requirement: dict,
    fact: dict,
    operator: str,
    policy: Policy,
    rules: dict,
    explain: dict,
) -> RuleOutcome:
    required = _decimal_or_none(requirement.get("value"))
    actual = _decimal_or_none(fact.get("value"))
    if required is None or actual is None:
        return _outcome(REVIEW_REQUIRED, operator, explain, "VALUE_NOT_NUMERIC", policy)

    tolerance = _tolerance(required, policy)
    if operator == "GREATER_OR_EQUAL":
        threshold = required - tolerance
        result = actual >= threshold
        expression = f"{_text(actual)} >= {_text(threshold)}"
    elif operator == "GREATER":
        threshold = required - tolerance
        result = actual > threshold
        expression = f"{_text(actual)} > {_text(threshold)}"
    elif operator == "LESS_OR_EQUAL":
        threshold = required + tolerance
        result = actual <= threshold
        expression = f"{_text(actual)} <= {_text(threshold)}"
    elif operator == "LESS":
        threshold = required + tolerance
        result = actual < threshold
        expression = f"{_text(actual)} < {_text(threshold)}"
    elif operator == "NOT_EQUAL":
        result = abs(actual - required) > tolerance
        expression = f"|{_text(actual)} - {_text(required)}| > {_text(tolerance)}"
    else:  # EQUAL и всё, что не задано явно: равенство с допуском.
        result = abs(actual - required) <= tolerance
        expression = f"|{_text(actual)} - {_text(required)}| <= {_text(tolerance)}"

    explain["expression"] = expression
    explain["result"] = result
    return _outcome(
        COMPLIANT if result else NON_COMPLIANT, operator, explain, None, policy
    )


def _compare_ranges(
    requirement: dict,
    fact: dict,
    operator: str,
    policy: Policy,
    rules: dict,
    explain: dict,
) -> RuleOutcome:
    """Диапазон против диапазона: покрытие (DOMAIN_RULES_ADR §1)."""
    req_min = _decimal_or_none(requirement.get("min"))
    req_max = _decimal_or_none(requirement.get("max"))
    fact_min = _decimal_or_none(fact.get("min"))
    fact_max = _decimal_or_none(fact.get("max"))
    if None in (req_min, req_max, fact_min, fact_max):
        return _outcome(REVIEW_REQUIRED, operator, explain, "RANGE_INCOMPLETE", policy)

    if rules.get("rangeSemantics") == "INCLUSION":
        result = fact_min >= req_min and fact_max <= req_max
        expression = f"[{_text(fact_min)}; {_text(fact_max)}] ⊆ [{_text(req_min)}; {_text(req_max)}]"
    else:
        tolerance = _tolerance(req_max - req_min, policy)
        result = fact_min <= req_min + tolerance and fact_max >= req_max - tolerance
        expression = f"[{_text(fact_min)}; {_text(fact_max)}] ⊇ [{_text(req_min)}; {_text(req_max)}]"
    explain["expression"] = expression
    explain["result"] = result
    explain["rangeSemantics"] = rules.get("rangeSemantics", "COVERAGE")
    return _outcome(
        COMPLIANT if result else NON_COMPLIANT, operator, explain, None, policy
    )


def _compare_number_in_range(
    requirement: dict,
    fact: dict,
    operator: str,
    policy: Policy,
    rules: dict,
    explain: dict,
) -> RuleOutcome:
    """Требование-диапазон, факт-число: число обязано попасть в диапазон."""
    req_min = _decimal_or_none(requirement.get("min"))
    req_max = _decimal_or_none(requirement.get("max"))
    actual = _decimal_or_none(fact.get("value"))
    if None in (req_min, req_max, actual):
        return _outcome(REVIEW_REQUIRED, operator, explain, "RANGE_INCOMPLETE", policy)

    tolerance = _tolerance(req_max - req_min, policy)
    result = req_min - tolerance <= actual <= req_max + tolerance
    explain["expression"] = f"{_text(req_min)} <= {_text(actual)} <= {_text(req_max)}"
    explain["result"] = result
    return _outcome(
        COMPLIANT if result else NON_COMPLIANT, operator, explain, None, policy
    )


def _compare_range_against_number(
    requirement: dict,
    fact: dict,
    operator: str,
    policy: Policy,
    rules: dict,
    explain: dict,
) -> RuleOutcome:
    """Требование-число, факт-диапазон.

    Однозначного правила нет: «насос выдаёт 30–50 м³/ч» против «нужно 40» может
    означать и соответствие, и рабочую точку вне диапазона нагрузки. Гадать
    здесь опаснее, чем спросить менеджера.
    """
    return _outcome(REVIEW_REQUIRED, operator, explain, "RANGE_AGAINST_SCALAR", policy)


def _compare_booleans(
    requirement: dict,
    fact: dict,
    operator: str,
    policy: Policy,
    rules: dict,
    explain: dict,
) -> RuleOutcome:
    result = bool(requirement.get("value")) == bool(fact.get("value"))
    explain["expression"] = f"{fact.get('value')} == {requirement.get('value')}"
    explain["result"] = result
    return _outcome(
        COMPLIANT if result else NON_COMPLIANT, operator, explain, None, policy
    )


def _compare_sets(
    requirement: dict,
    fact: dict,
    operator: str,
    policy: Policy,
    rules: dict,
    explain: dict,
) -> RuleOutcome:
    """Набор факта обязан содержать весь требуемый набор (CONTAINS_ALL).

    Сравнение идёт по нормализованной форме, а в объяснение попадают исходные
    записи: нормализация подменяет похожие латинские буквы кириллическими, и
    «атех» вместо «atex» в отчёте выглядел бы опечаткой системы.
    """
    required_values = list(requirement.get("values", []))
    actual_values = list(fact.get("values", []))
    actual = {normalize_text(item) for item in actual_values}
    missing = sorted(
        item for item in required_values if normalize_text(item) not in actual
    )
    result = not missing
    explain["expression"] = f"{sorted(actual_values)} ⊇ {sorted(required_values)}"
    explain["result"] = result
    if missing:
        explain["missingValues"] = missing
    return _outcome(
        COMPLIANT if result else NON_COMPLIANT, operator, explain, None, policy
    )


def _compare_texts(
    requirement: dict,
    fact: dict,
    operator: str,
    policy: Policy,
    rules: dict,
    explain: dict,
) -> RuleOutcome:
    """Текст и материал: точное совпадение после нормализации.

    Несовпадение — не отказ: «сталь 12Х18Н10Т» и «AISI 321» это один материал,
    а иерархии материалов в каталоге пока нет. Решает менеджер.
    """
    required = normalize_text(str(requirement.get("value") or ""))
    actual = normalize_text(str(fact.get("value") or ""))
    result = bool(required) and required == actual
    explain["expression"] = f"«{fact.get('value')}» == «{requirement.get('value')}»"
    explain["result"] = result
    if result:
        return _outcome(COMPLIANT, operator, explain, None, policy)
    return _outcome(REVIEW_REQUIRED, operator, explain, "TEXT_NOT_EQUAL", policy)


_TEXT_KINDS = ("TEXT", "MATERIAL", "UNKNOWN")

_HANDLERS: dict[tuple[str, str], object] = {
    ("NUMBER", "NUMBER"): _compare_numbers,
    ("RANGE", "RANGE"): _compare_ranges,
    ("RANGE", "NUMBER"): _compare_number_in_range,
    ("NUMBER", "RANGE"): _compare_range_against_number,
    ("BOOLEAN", "BOOLEAN"): _compare_booleans,
    ("SET", "SET"): _compare_sets,
    **{
        (required_kind, fact_kind): _compare_texts
        for required_kind in _TEXT_KINDS
        for fact_kind in _TEXT_KINDS
    },
}


# --- вспомогательное ----------------------------------------------------


def _operator(
    requirement: dict | None, value_kind: str, policy: Policy, rules: dict
) -> str | None:
    """Оператор: шаблон типа → знак в самом требовании → умолчание по типу."""
    if policy.operator:
        return policy.operator
    token = (requirement or {}).get("operator")
    if token:
        mapped = rules.get("operatorTokens", {}).get(token)
        if mapped:
            return mapped
    return rules.get("defaultOperators", {}).get(value_kind)


def _tolerance(base: Decimal, policy: Policy) -> Decimal:
    """Допуск в единицах значения: процент считается от требования."""
    if policy.tolerance_value is None:
        return Decimal(0)
    if policy.tolerance_type == "PERCENT":
        return abs(base) * policy.tolerance_value / Decimal(100)
    return abs(policy.tolerance_value)


def _unit_mismatch(requirement: dict, fact: dict) -> str | None:
    """Единицы обязаны совпасть: нормализация уже привела их к канонической.

    Расхождение означает, что одна из сторон не конвертировалась (неизвестная
    или неоднозначная единица) — сравнивать числа в разных единицах нельзя.
    """
    required_unit = requirement.get("canonicalUnit")
    fact_unit = fact.get("canonicalUnit")
    if required_unit == fact_unit:
        return None
    if required_unit is None or fact_unit is None:
        return "UNIT_MISSING"
    return "UNIT_MISMATCH"


def _outcome(
    status: str,
    operator: str | None,
    explain: dict,
    issue: str | None,
    policy: Policy,
    *,
    skip_soften: bool = False,
) -> RuleOutcome:
    """Собирает результат и смягчает отказ для справочного параметра.

    Informational (`mandatory = false`) с несоответствием даёт
    `REVIEW_REQUIRED`, а не `NON_COMPLIANT` (DOMAIN_RULES_ADR §9): иначе
    менеджер не отличит критичные расхождения от справочных.
    `NOT_FOUND_IN_FACT` не смягчается — у него свой вклад в вердикт.
    """
    if issue:
        explain["issue"] = issue
        explain.setdefault("result", False)
    if not skip_soften and status == NON_COMPLIANT and not policy.mandatory:
        explain["softenedTo"] = REVIEW_REQUIRED
        explain["softenedReason"] = "INFORMATIONAL_PARAMETER"
        status = REVIEW_REQUIRED
    return RuleOutcome(status=status, operator=operator, explanation=explain)


def _operand(typed: dict | None) -> dict | None:
    """Операнд для `explanation`: значение и единица в человеческом виде."""
    if typed is None:
        return None
    kind = typed.get("kind")
    if kind == "RANGE":
        value = f"{typed.get('min')}…{typed.get('max')}"
    elif kind == "SET":
        value = ", ".join(typed.get("values", []))
    elif kind == "BOOLEAN":
        value = "да" if typed.get("value") else "нет"
    else:
        value = typed.get("value")
    return {
        "kind": kind,
        "value": None if value is None else str(value),
        "unit": typed.get("canonicalUnit"),
    }


def _decimal_or_none(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _text(value: Decimal | None) -> str | None:
    """Decimal в текст без экспоненты: значение уходит в JSON и в отчёт."""
    if value is None:
        return None
    return format(value.normalize(), "f")
