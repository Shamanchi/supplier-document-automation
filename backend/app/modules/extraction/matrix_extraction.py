# -*- coding: utf-8 -*-
"""Вариант Б (v2): детерминированное извлечение «матриц» технических характеристик.

Два формата таблиц:
1) строки = модели, колонки = параметры (СМ80-50-200-2, ЭЦВ 5-4-75, КММ100-65-200/2);
2) «обратный» формат: строки = показатели, колонки = модели (1К50-32-125 и т.п.).

Заголовки колонок разрешаются по координатам x ячеек: если ячейка шапки
содержит несколько названий параметров (склеенные заголовки PDF), каждое имя
приписывается своей колонке слева направо по перекрытию x-диапазонов.
"""
from __future__ import annotations

import collections
import re

from app.modules.extraction.matrix_types import Evidence, Parameter, Table
from app.modules.extraction.matrix_utils import collapse_ws, normalize_header

# маркеры таблиц-перечней деталей/кодов/дефектов (лишний «мусор» для ТХ)
JUNK_TABLE_MARKERS = (
    "критерии предельных состояний",
    "разрушение подшипников",
    "возможные неисправности",
    "методы устранения",
    "код для заказа",
    "консерваци",
    "наименование детали",
    "масса одной детали",
    "быстроизнашиваемых",
    "цветных металлов",
    "материал",
    "установочные размеры",
    "размеры, мм, не более",
    "нагрузки на всас",
    "напорные патрубки",
)

# канонизация параметра по подстроке заголовка (порядок важен)
_PARAM_PATTERNS: list[tuple[str, str]] = [
    (r"подач", "Подача"),
    (r"напор", "Напор"),
    (r"мощн", "Мощность"),
    (r"частот|вращен|об/мин|об\мин|rpm|r/min|с-1|1/с", "Частота вращения"),
    (r"\bток|сила тока", "Ток"),
    (r"давлен|манометр", "Давление"),
    (r"напряж", "Напряжение"),
    (r"расход", "Расход"),
    (r"масс|вес|weight", "Масса"),
    (r"кпд|к\.п\.д", "КПД"),
    (r"температур|temp", "Температура"),
    (r"габарит", "Габариты"),
    (r"диаметр|диам|проход|dn|ду", "Условный проход"),
    (r"длина|кабел", "Длина"),
    (r"размер|разм", "Размер"),
    (r"кавитац", "Кавитационный запас"),
    (r"ступен", "Число ступеней"),
    (r"утечк", "Утечки"),
    (r"шум|noise|sound", "Уровень шума"),
    (r"полюс", "Число полюсов"),
    (r"фаз", "Число фаз"),
    (r"наработк|ресурс|срок служб|готовност|сохраняемост", "Надёжность"),
    (r"power|voltage|current|pressure|flow|speed", ""),  # англ. — не должно оставаться
]

# допустимые единицы для каждого канонического имени (нижний регистр)
_NAME_UNITS: dict[str, set[str]] = {
    "Подача": {"м3/час", "м3/ч", "м3/с", "м3/мин", "м³/час", "м³/ч", "м³/с",
               "л/с", "л/мин", "л/ч", "л", "м3", "м³", "m3/h", "m3/s", "m3/min", "l/s"},
    "Расход": {"м3/час", "м3/ч", "м3/с", "м3/мин", "м³/ч", "л/с", "л/мин", "л/ч", "м3", "л"},
    "Напор": {"м"},
    "Кавитационный запас": {"м"},
    "Мощность": {"квт", "квт/ч", "вт", "л.с.", "ква", "кв·а", "кв·ч", "kw", "w", "kva"},
    "Ток": {"а", "ма", "ка"},
    "Давление": {"мпа", "кгс/см2", "кгс/см²", "кг/см2", "бар", "па", "кпа", "атм", "psi"},
    "Напряжение": {"в", "кв", "мв"},
    "Частота вращения": {"об/мин", "об/с", "мин-1", "с-1", "1/с", "об/мин.", "гц", "hz", "rpm"},
    "Масса": {"кг", "т", "г", "kg"},
    "Габариты": {"мм", "см", "м", "дюйм"},
    "Длина": {"мм", "см", "м", "км"},
    "Размер": {"мм", "см", "м", "дюйм"},
    "Условный проход": {"мм", "дюйм", "dn"},
    "КПД": {"%"},
    "Температура": {"°с", "°c", "°", "к"},
    "Число полюсов": set(),
    "Число фаз": set(),
    "Число ступеней": {"шт", "ст."},
    "Уровень шума": {"дба", "дб"},
    "Надёжность": {"ч", "час", "ч.", "лет", "год", "цикл", "с"},
    "Утечки": {"см3/ч", "мл/ч", "мл", "см3/мин", "мл/с"},
}
_UNIT_KNOWN = {u for st in _NAME_UNITS.values() for u in st}
# единица-«подпись» в значении ячейки, если в шапке её не было
_VALUE_UNIT_RE = re.compile(r"([A-Za-zА-ЯЁа-яё0-9°%./·]+)$")

# «модель»: одно название (СМ80-50-200-2, ЭЦВ 5-4-75, 1К50-32-125-т-Е, МКПВ-10/3С(Т)2Р)
_MODEL_RE = re.compile(
    r"^(?:[A-Za-zА-ЯЁа-яё]{1,6}[ \-–]?)?[0-9][A-Za-zА-ЯЁа-яё0-9.,/()\-–—+]*$"
)
_GLUED_SPLIT_RE = re.compile(r"(?<=[0-9])(?=[0-9][A-ZА-ЯЁа-яё]{1,2}[0-9])")

_UNIT_CHARS = re.compile(r"[0-9.,+\-–—/()≤≥±%°:·×xхX*… ]")
_STOP_WORDS = {
    "не", "более", "менее", "на", "при", "до", "с", "и", "в", "агрегата", "агрегат",
    "насоса", "насос", "двигателя", "двигатель", "компрессора", "эл", "тока", "ток",
    "номинальные", "номинальный", "номинальное", "параметры", "напорной", "стороны",
    "мощность", "частота", "вращения", "подача", "давление", "напряжение", "масса",
    "насосов", "агрегатов", "изделия", "непосредственно",
}


def _clean_token(tok: str) -> str:
    return re.sub(r"[.,;:»«()\[\]]+$", "", tok).strip()


def _is_single_model(text: str) -> bool:
    s = collapse_ws(text)
    if not s or len(s) > 36 or "..." in s:
        return False
    low = s.lower()
    if low.startswith(("таблица", "продолж", "окончан", "приложен", "обозначен")):
        return False
    if not re.search(r"[A-Za-zА-ЯЁа-яё]", s) or not re.search(r"[0-9]", s):
        return False
    # посадки/допуски («32h7(-0.025)», «42 h7(-0.025)») — не модели
    if re.search(r"\(\s*[-+\u2212\u2013]?\s*\d", s):
        return False
    # суффикс исполнения в скобках («ЭЦВ 8-25-35 (нрк)») — часть маркировки модели
    sfx = re.match(r"^(.*)\s+\(([A-Za-zА-ЯЁа-яё]{2,6})\)$", s)
    if sfx:
        s = sfx.group(1)
    # код модели с разделителями (СМ80-50-200, ЭЦВ 5-4-75, 1К50-32-125, МКПВ-10/3С(Т)2Р);
    # одиночные «D1», «l1», «a2» — обозначения размеров, не модели
    if not re.search(r"[-–/]", s):
        return False
    if _MODEL_RE.match(s):
        return True
    # короткий хвостовой суффикс без пробела-разделителя («ЭЦВ 8-46-120 нрк»)
    words = s.split(" ")
    if len(words) >= 3 and _MODEL_RE.match(" ".join(words[:-1])) and re.fullmatch(r"[A-Za-zА-ЯЁа-яё0-9]{1,6}", words[-1]):
        return True
    return False


def _split_models(text: str) -> list[str]:
    """Разбить ячейку на отдельные модели (склейка переносов '1К50-32- 125')."""
    s = collapse_ws(text)
    if not s:
        return []
    s = re.sub(r"-[ \t]+", "-", s)
    if "..." in s:
        return []
    if _is_single_model(s) and not _GLUED_SPLIT_RE.search(s):
        return [s]
    out: list[str] = []
    for tok in s.split(" "):
        tok = tok.strip()
        if not tok:
            continue
        if _is_single_model(tok) and not _GLUED_SPLIT_RE.search(tok):
            out.append(tok)
            continue
        # «слипшиеся» модели без пробела: ...1251К65...
        glued = [g for g in re.split(_GLUED_SPLIT_RE, tok) if g]
        if glued and all(_is_single_model(g) for g in glued):
            out.extend(glued)
        else:
            return []
    return out


def _looks_like_value(text: str) -> bool:
    """Ячейка-значение: число/диапазон/допуск (не текст двигателя вида 4А132М2)."""
    t = collapse_ws(text)
    if not t:
        return False
    m = re.match(r"^[-–—(\u2264\u2265\u00b1]?\s*\d", t)
    if not m:
        return False
    return _UNIT_CHARS.sub("", t) == ""


def _main_tokens(vt: str) -> list[str]:
    """Значимые числовые фрагменты ячейки (без полностью скобочных альтернатив)."""
    return [t for t in collapse_ws(vt).split(" ") if t and not re.fullmatch(r"[\(\[][^)\]]*[\)\]]", t)]


def _single_main_value(vt: str) -> str | None:
    mains = _main_tokens(vt)
    if len(mains) == 1:
        return mains[0]
    return None


def _normalize_unit_name(name: str) -> str:
    if name == "°с":
        return "°С"
    return name


def _unit_from_slice(slice_text: str, canon: str) -> str | None:
    """Единица для параметра по фрагменту текста шапки (оригинальный регистр)."""
    allowed = _NAME_UNITS.get(canon)
    if not allowed:
        return None
    for tok in re.split(r"[\s,;]+", slice_text):
        tok_clean = _clean_token(tok)
        low = tok_clean.lower()
        if low in allowed and low:
            return _normalize_unit_name(tok_clean)
    return None


def _parse_header_text(text: str) -> list[tuple[str, str | None]]:
    """(name, unit) для всех названий параметров в тексте шапки, слева направо."""
    original = collapse_ws(text)
    low = original.lower()
    if not low:
        return []
    hits: list[tuple[int, str]] = []
    for pat, canon in _PARAM_PATTERNS:
        if not canon:
            continue
        try:
            for m in re.finditer(pat, low):
                hits.append((m.start(), canon))
        except re.error:
            continue
    if not hits:
        return []
    hits.sort()
    dedup: list[tuple[int, str]] = []
    for pos, canon in hits:
        if dedup and dedup[-1][1] == canon:
            continue
        dedup.append((pos, canon))
    out: list[tuple[str, str | None]] = []
    for i, (pos, canon) in enumerate(dedup):
        end = dedup[i + 1][0] if i + 1 < len(dedup) else len(low)
        unit = _unit_from_slice(original[pos:end], canon)
        if unit is None:
            unit = _unit_from_slice(original, canon)
        out.append((canon, unit))
    return out


def _is_junk_table(table: Table) -> bool:
    sample = " ".join(c.text for row in table.rows[:40] for c in row).lower()
    return any(m in sample for m in JUNK_TABLE_MARKERS)


def _make_key(name: str, value: str, unit: str | None) -> tuple[str, str, str]:
    return (normalize_header(name), collapse_ws(value).lower(), (unit or "").lower())


def _row_param(model: str, name: str, unit: str | None, value_text: str,
               t: Table, ri: int, doc_name: str, row_index_1based: int | None = None) -> Parameter:
    quote = f"{model} | {name} ({unit or '-'}) : {value_text}"
    return Parameter(
        name=f"{name} ({model})",
        value=value_text,
        unit=unit,
        evidence=Evidence(document=doc_name, page=(t.page + 1) if t.page is not None else None,
                          quote=collapse_ws(quote)[:200], table=t.id, row=row_index_1based or (ri + 1)),
        source="preset",
        verified=True,
    )


# шапки-«коды» буквенных размеров (L, Hф, h1, l2...) и аксессуаров обвязки (фильтр и т.п.)
_DIM_CODE_RE = re.compile(r'^[A-Za-zА-ЯЁа-яё]{1,3}\d{0,2}$')
_ACCESSORY_HDRS = {
    'фильтр', 'компенсатор', 'компенсатор1', 'компенсатор2', 'муфта', 'фланец',
    'патрубок', 'штуцер', 'заглушка', 'переходник', 'кронштейн', 'опора',
}


def _is_dim_code_header(text: str) -> bool:
    s = collapse_ws(text)
    return bool(s) and bool(_DIM_CODE_RE.match(s))


def _is_accessory_header(text: str) -> bool:
    return normalize_header(collapse_ws(text)) in _ACCESSORY_HDRS


_GLUED_ECV_RE = re.compile(r"^ЭЦВ\s+ЭЦВ\b")


def _glued_ecv_word_rows(t, headers, band, page_words, doc_name, dedup, out):
    """Двухмодельные строки ЭЦВ (rowspan по печати): модели и значения идут двумя
    горизонтальными под-строками; общие слова («6,5», «120») стоят между ними."""
    rows = t.rows
    glue_idx = []
    for ri, row in enumerate(rows):
        if row and _GLUED_ECV_RE.match(row[0].text.strip()):
            glue_idx.append(ri)
    if not glue_idx or t.page is None or t.page >= len(page_words) or not band:
        return
    page = page_words[t.page]
    data_x0 = min(b[0] for b in band.values())
    num_re = re.compile(r"^[\-\u2013\u2014(\u2264\u2265\u00b1]?\s*\d")
    for ri in glue_idx:
        row = rows[ri]
        ys0 = [c.y0 for c in row if c.y0 is not None]
        ys1 = [c.y1 for c in row if c.y1 is not None]
        if not ys0 or not ys1:
            continue
        y_lo = min(ys0) - 3.0
        y_hi = max(ys1) + 3.0
        words = [w for w in page if w[4].strip() and w[1] > y_lo and w[3] < y_hi]
        anchors: list[tuple[str, float]] = []
        i = 0
        while i < len(words):
            w = words[i]
            if w[4] == "ЭЦВ" and i + 1 < len(words) and re.search(r"-", words[i + 1][4]):
                tok = words[i + 1]
                model = "ЭЦВ " + tok[4]
                ym = (tok[1] + tok[3]) / 2.0
                j = i + 2
                if j < len(words) and re.fullmatch(r"[A-Za-zА-ЯЁа-яё()]{1,8}", words[j][4]):
                    model += " " + words[j][4]
                anchors.append((model, ym))
                i += 2
                continue
            i += 1
        if len(anchors) < 2:
            continue
        vals: dict[str, dict[int, list[str]]] = {}
        for w in words:
            if w[0] < data_x0 - 5 or not num_re.match(w[4]) or _UNIT_CHARS.sub("", w[4]) != "":
                continue
            cx = (w[0] + w[2]) / 2.0
            best_j = None
            best_d = 1e9
            for j, (a, b) in band.items():
                d = abs(cx - (a + b) / 2.0)
                if d < best_d:
                    best_d = d
                    best_j = j
            if best_j is None or best_d >= 30.0:
                continue
            wy = (w[1] + w[3]) / 2.0
            model = None
            bd = 1e9
            for mn, ym in anchors:
                d = abs(wy - ym)
                if d < bd:
                    bd = d
                    model = mn
            if model is None or bd > 5.5:
                continue
            vals.setdefault(model, {}).setdefault(best_j, []).append(w[4])
        for model, mv in vals.items():
            if not mv:
                continue
            for j, lst in mv.items():
                main = _single_main_value(" ".join(lst))
                if main is None:
                    continue
                hdr = headers.get(j)
                if not hdr:
                    continue
                name, unit = hdr
                p = _row_param(model, name, unit, main, t, ri, doc_name)
                key = _make_key(p.name, p.value, p.unit)
                if key in dedup:
                    continue
                dedup.add(key)
                out.append(p)


def _extract_model_rows(tables: list[Table], doc_name: str, dedup: set[tuple],
                     page_words=None) -> list[Parameter]:
    """Формат 1: строки = модели, колонки = параметры с шапкой."""
    out: list[Parameter] = []
    for t in tables:
        rows = t.rows
        if len(rows) < 4 or _is_junk_table(t):
            continue
        body_start: int | None = None
        for i, row in enumerate(rows):
            if not row:
                continue
            if not _is_single_model(row[0].text.strip()):
                continue
            vals = [j for j in range(1, len(row))
                    if row[j].text.strip() and _looks_like_value(row[j].text)]
            if len(vals) >= 2:
                body_start = i
                break
        if body_start is None or body_start <= 0:
            continue
        body_idx = [i for i in range(body_start, len(rows))
                    if rows[i] and _is_single_model(rows[i][0].text.strip())]
        if len(body_idx) < 3:
            continue
        counts: dict[int, int] = {}
        for i in body_idx:
            row = rows[i]
            for j in range(1, len(row)):
                vt = row[j].text.strip()
                if vt and _looks_like_value(vt):
                    counts[j] = counts.get(j, 0) + 1
        thr = max(2, int(len(body_idx) * 0.35))
        data_cols = [j for j in sorted(counts) if counts[j] >= thr]
        if len(data_cols) < 2:
            continue
        # x-диапазон данных каждой колонки (по одиночным значениям — без «вариантных» ячеек)
        band: dict[int, tuple[float, float]] = {}
        for j in data_cols:
            x0s = [rows[i][j].x0 for i in body_idx
                   if rows[i][j].text.strip() and _looks_like_value(rows[i][j].text)
                   and _single_main_value(rows[i][j].text) is not None and rows[i][j].x0 is not None]
            x1s = [rows[i][j].x1 for i in body_idx
                   if rows[i][j].text.strip() and _looks_like_value(rows[i][j].text)
                   and _single_main_value(rows[i][j].text) is not None and rows[i][j].x1 is not None]
            if x0s and x1s:
                band[j] = (min(x0s), max(x1s))

        def _overlap(cell, j):
            b = band.get(j)
            if b is None or cell.x0 is None or cell.x1 is None:
                return 0.0
            return max(0.0, min(cell.x1, b[1]) - max(cell.x0, b[0]))
        # самая нижняя подпись шапки над каждой колонкой (отсев «L/H/h1» и обвязки)
        low_hdr: dict[int, str] = {}
        for j in data_cols:
            for i in range(body_start - 1, -1, -1):
                found = ''
                for c in rows[i]:
                    if c.text.strip() and _overlap(c, j) > 1.0:
                        found = collapse_ws(c.text)
                        break
                if found:
                    low_hdr[j] = found
                    break

        # назначение заголовков: сверху вниз (нижние строки шапки перекрывают верхние)
        headers: dict[int, tuple[str, str | None]] = {}
        for i in range(body_start):
            for c in rows[i]:
                txt = c.text.strip()
                if not txt:
                    continue
                parsed = _parse_header_text(txt)
                if not parsed:
                    continue
                overl = sorted(((j, _overlap(c, j)) for j in data_cols), key=lambda x: -x[1])
                cols = [j for j, ov in overl if ov > 1.0]
                if not cols:
                    continue
                # случайные «перекрытия» по краю ячейки: оставляем только значимые
                if len(cols) > 1:
                    best_ov = overl[0][1]
                    cols = [j for j, ov in overl if ov > 1.0 and ov >= max(1.5, best_ov * 0.5)]
                if not cols:
                    continue
                names = list(parsed)
                if len(names) > len(cols):
                    names = names[len(names) - len(cols):]
                elif len(names) < len(cols):
                    # «Подача Q» поверх двух узких колонок: имя относится к левым колонкам
                    cols = cols[:len(names)]
                if len(parsed) >= 3 and len(cols) * 2 < len(parsed):
                    # шапка-«перечисление» нескольких параметров над 1-2 колонками данных
                    # (например, разорванный заголовок размерной таблицы) — мусор
                    continue
                for j, (name, unit) in zip(sorted(cols), names):
                    if name == "Размер" and not unit:
                        # «Размер» без единицы — безымянная размерная колонка (L, L2, ...)
                        continue
                    low = low_hdr.get(j, "")
                    if name == "Условный проход" and _is_accessory_header(low):
                        # Ду фильтра/компенсатора, а не самого насоса — мусор для ТХ
                        continue
                    if name == "Размер" and _is_dim_code_header(low):
                        # буквенный размер из размерной таблицы (L, H, h1, Lф, ...) — мусор
                        continue
                    headers[j] = (name, unit)
        if not headers:
            continue
        for i in body_idx:
            row = rows[i]
            model = collapse_ws(row[0].text)
            if not model:
                continue
            for j in sorted(headers):
                if j >= len(row):
                    continue
                vt = row[j].text.strip()
                if not vt or not _looks_like_value(vt):
                    continue
                main = _single_main_value(vt)
                if main is None:
                    continue
                name, unit = headers[j]
                value = collapse_ws(main)
                p = _row_param(model, name, unit, value, t, i, doc_name)
                key = _make_key(p.name, p.value, p.unit)
                if key in dedup:
                    continue
                dedup.add(key)
                out.append(p)
        # строки с двумя моделями (печать = rowspan): значения лежат двумя под-строками слов
        _glued_ecv_word_rows(t, headers, band, page_words, doc_name, dedup, out)
    return out


def _extract_transposed(tables: list[Table], doc_name: str, dedup: set[tuple],
                     page_words=None) -> list[Parameter]:
    """Формат 2: строки = показатели, колонки = модели (например, 1К50-32-125...)."""
    out: list[Parameter] = []

    def _numeric_tokens(vt: str) -> list[str]:
        """Числовые фрагменты ячейки без «чисто скобочных» альтернатив единиц ((13), (3,5))."""
        return [m for m in _main_tokens(vt)
                if re.match(r"^[\-\u2013\u2014(\u2264\u2265\u00b1]?\s*\d", m)
                and not re.fullmatch(r"[\(\[][^)\]]*[\)\]]", m)]

    def _merge_label_fragments(pending: list[str], raw: str) -> str:
        text = " ".join(list(pending) + [raw])
        return re.sub(r"-\s+", "", text)

    def _model_at(models, spans, cx):
        """Индекс модели, чей поддиапазон x содержит центр значения (приближённо, поровну)."""
        if cx is None:
            return None
        for idx, (a, b) in enumerate(spans):
            if a <= cx <= b:
                return idx
        return None

    for t in tables:
        rows = t.rows
        if len(rows) < 3 or _is_junk_table(t):
            continue
        # строка с моделями в колонках (>=3 моделей, без числовых значений)
        model_row: int | None = None
        for i, row in enumerate(rows):
            total = 0
            has_value = False
            for c in row:
                txt = c.text.strip()
                if not txt:
                    continue
                if _looks_like_value(txt):
                    has_value = True
                total += len(_split_models(txt))
            if total >= 3 and not has_value:
                model_row = i
        if model_row is None:
            continue
        model_cols: dict[int, list[str]] = {}
        model_spans: dict[int, list[tuple[float, float]]] = {}
        for j, c in enumerate(rows[model_row]):
            txt = c.text.strip()
            if not txt:
                continue
            ms = _split_models(txt)
            if ms and c.x0 is not None and c.x1 is not None:
                model_cols[j] = ms
                w = (c.x1 - c.x0) / len(ms)
                model_spans[j] = [(c.x0 + w * k, c.x0 + w * (k + 1)) for k in range(len(ms))]
        if not model_cols:
            continue
        j0 = min(model_cols)
        cur_name: str | None = None
        cur_unit: str | None = None

        def emit(model, name, unit, value, ri):
            p = Parameter(
                name=f"{name} ({model})",
                value=value,
                unit=unit,
                evidence=Evidence(document=doc_name,
                                  page=(t.page + 1) if t.page is not None else None,
                                  quote=f"{model} | {name} ({unit or '-'}) : {value}"[:200],
                                  table=t.id, row=(ri + 1)),
                source="preset",
                verified=True,
            )
            key = _make_key(p.name, p.value, p.unit)
            if key in dedup:
                return
            dedup.add(key)
            out.append(p)

        pending: list[str] = []       # фрагменты подписи показателя до строки со значениями
        prev_had_values = False
        for ri in range(model_row + 1, len(rows)):
            row = rows[ri]
            label_text = " ".join(row[j].text.strip() for j in range(0, j0)
                                  if j < len(row) and row[j].text.strip())
            value_cells: dict[int, tuple[str, float | None]] = {}
            for j in sorted(model_cols):
                if j < len(row) and row[j].text.strip():
                    cell = row[j]
                    cx = ((cell.x0 or 0) + (cell.x1 or 0)) / 2 if cell.x0 is not None and cell.x1 is not None else None
                    value_cells[j] = (cell.text.strip(), cx)
            had_values = bool(value_cells)
            raw = label_text.strip()
            parsed = _parse_header_text(raw) if raw else []
            name: str | None = None
            unit: str | None = None
            if not parsed and pending and raw:
                parsed = _parse_header_text(_merge_label_fragments(pending, raw))
                if parsed:
                    name, unit = parsed[0]
            elif parsed:
                name, unit = parsed[0]
            if name is not None:
                cur_name, cur_unit = name, unit
                pending = []
                if had_values:
                    for j in sorted(model_cols):
                        models = model_cols[j]
                        vt, cx = value_cells.get(j, ("", None))
                        if not vt:
                            continue
                        seq = _numeric_tokens(vt)
                        if len(models) == 1:
                            if seq:
                                emit(models[0], cur_name, cur_unit, collapse_ws(seq[0]), ri)
                        else:
                            if len(seq) == len(models):
                                for mod, val in zip(models, seq):
                                    emit(mod, cur_name, cur_unit, collapse_ws(val), ri)
                            elif len(seq) == 1:
                                # одно значение в группе моделей: привязываем по x-центру ячейки
                                idx = _model_at(models, model_spans.get(j, []), cx)
                                if idx is not None:
                                    emit(models[idx], cur_name, cur_unit, collapse_ws(seq[0]), ri)
                    prev_had_values = True
                    continue
            # имени нет: уточнение единицы либо фрагмент/продолжение подписи
            if raw:
                if cur_name is not None:
                    for _n, u in _parse_header_text(raw):
                        if u:
                            cur_unit = u
                if not had_values and not prev_had_values:
                    pending.append(raw)
                elif not had_values and prev_had_values:
                    nxt = rows[ri + 1] if ri + 1 < len(rows) else None
                    if nxt is not None:
                        nxt_vals = any(
                            j < len(nxt) and nxt[j].text.strip() and
                            _looks_like_value(nxt[j].text)
                            for j in sorted(model_cols))
                        if nxt_vals:
                            pending.append(raw)
            prev_had_values = had_values
        # «висящая» последняя строка-метка без значений -> добор из слов той же страницы
        # (детектор не положил цифры в ячейки: например, «Максимальная мощность» у 1К)
        if (rows and j0 is not None and model_cols and page_words
                and t.page is not None and t.page < len(page_words)):
            last_row = rows[-1]
            if last_row:
                last_label = " ".join(
                    c.text.strip() for c in last_row if c.col < j0 and c.text.strip()
                )
                last_has_vals = any(
                    j < len(last_row) and last_row[j].text.strip()
                    and _looks_like_value(last_row[j].text)
                    for j in sorted(model_cols))
                if last_label and not last_has_vals:
                    out.extend(
                        _gap_continuation(t, model_cols, model_spans, last_label,
                                          page_words, tables, doc_name, dedup)
                    )
    return out




# --- добор по словам страницы и «Подача ЭЦВ» из маркировки -----------------

# номинальная подача ЭЦВ из маркировки: «ЭЦВ 5-6,5-50» -> 6,5 м3/ч
_ECV_FLOW_RE = re.compile(r"ЭЦВ[\s\-]*\d+[\s\-]*(\d+(?:[.,]\d+)?)")
_ECV_DASH_RE = re.compile(r"^\d+(?:[.,]\d+)?-\d")


def _ecv_models_in_cell(text: str) -> list[str]:
    """Модели ЭЦВ в ячейке: «ЭЦВ 5-6,5-50», склейки «ЭЦВ ЭЦВ 5-6,5-80 5-6,5-100»;
    суффиксы исполнения сохраняются («(нрк)», «нрк», «нро»)."""
    tokens = re.split(r"\s+", collapse_ws(text))
    out: list[str] = []
    suffixes: list[str] = []
    prev_ecv = False
    for tk in tokens:
        if not tk:
            continue
        if tk == "ЭЦВ":
            prev_ecv = True
            continue
        if re.match(r"^\d", tk) and "-" in tk:
            if prev_ecv or "ЭЦВ" in text:
                out.append("ЭЦВ " + tk)
                prev_ecv = True
            else:
                prev_ecv = False
            continue
        if prev_ecv and re.fullmatch(r"[A-Za-zА-ЯЁа-яё()]{1,8}", tk) \
                and re.search(r"[A-Za-zА-ЯЁа-яё]", tk):
            suffixes.append(tk)
            prev_ecv = True
            continue
        prev_ecv = False
    # суффиксы печатаются в конце ячейки — раздать последним моделям по одному
    for k, sfx in enumerate(suffixes):
        idx = len(out) - len(suffixes) + k
        if 0 <= idx < len(out):
            out[idx] += " " + sfx
    return out


def _ecv_feed_models(tables: list[Table], doc_name: str, dedup: set[tuple]) -> list[Parameter]:
    """Подача ЭЦВ из маркировки (в PDF значение печатается не в каждой строке)."""
    out: list[Parameter] = []
    for t in tables:
        rows = t.rows
        if len(rows) < 3:
            continue
        found_rows = 0
        multi_value = False
        for row in rows:
            if not row:
                continue
            models = _ecv_models_in_cell(row[0].text)
            if not models:
                continue
            found_rows += 1
            vals = [j for j in range(1, len(row))
                    if row[j].text.strip() and _looks_like_value(row[j].text)]
            if len(vals) >= 2:
                multi_value = True
        if found_rows < 3 or not multi_value:
            continue
        for ri, row in enumerate(rows):
            if not row:
                continue
            for model in _ecv_models_in_cell(row[0].text):
                m = _ECV_FLOW_RE.search(model)
                if not m:
                    continue
                value = m.group(1)
                p = _row_param(model, "Подача", "м3/ч", value, t, ri, doc_name)
                key = _make_key(p.name, p.value, p.unit)
                if key in dedup:
                    continue
                dedup.add(key)
                out.append(p)
    return out


def _same_line(a: tuple, b: tuple, factor: float = 0.4) -> bool:
    """Слова на одной строке, если вертикальные интервалы пересекаются."""
    y0a, y1a = a[1], a[3]
    y0b, y1b = b[1], b[3]
    h = min(y1a - y0a, y1b - y0b)
    inter = min(y1a, y1b) - max(y0a, y0b)
    return inter > factor * h


def _group_lines(words: list[tuple]) -> list[list[tuple]]:
    words = sorted(words, key=lambda w: (round(w[1], 1), w[0]))
    lines: list[list[tuple]] = []
    for w in words:
        if lines and _same_line(lines[-1][-1], w):
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w[0])
    return [ln for ln in lines if ln]


def _gap_continuation(t, model_cols, model_spans, label_prefix, page_words,
                      tables, doc_name, dedup) -> list[Parameter]:
    """Значения «под» последней строкой таблицы, не попавшие в ячейки детектора."""
    # Шаг миграции 03: в фазе 3 здесь был ленивый `from app.detect import
    # _group_lines` — модуля, которого нет ни в одном из репозиториев (он
    # остался в tech-docs-reader). Функции перенесены дословно, чтобы ветка
    # не падала ImportError'ом на живых документах.
    spans: list[tuple[str, float, float]] = []
    for j in sorted(model_cols):
        ms = model_cols[j]
        sp = model_spans.get(j) or []
        for k, name in enumerate(ms):
            if k < len(sp):
                spans.append((name, sp[k][0], sp[k][1]))
    if len(spans) < 2:
        return []
    x_min = min(a for _, a, _ in spans)
    y_lo = max((c.y1 or 0.0) for row in t.rows for c in row if c.y1 is not None) or 0.0
    y_hi = None
    for o in tables:
        if o is t or o.page != t.page:
            continue
        for row in o.rows:
            for c in row:
                if c.y0 is not None and c.y0 > y_lo + 1:
                    y_hi = min(y_hi, c.y0) if y_hi is not None else c.y0
    if y_hi is None:
        y_hi = y_lo + 120.0
    words = [
        w for w in page_words[t.page]
        if w[4].strip() and w[1] > y_lo and (y_hi is None or w[3] < y_hi + 1)
    ]
    if not words:
        return []
    parts: list[str] = []
    value_lines: list[list[tuple[float, str]]] = []
    for ln in _group_lines(words):
        ln = sorted(ln, key=lambda w: w[0])
        labels = [w[4] for w in ln if w[2] < x_min - 1]
        if labels:
            parts.append(" ".join(labels))
        nums = []
        for w in ln:
            if w[0] < x_min - 1:
                continue
            if re.match(r"^[\-\u2013\u2014(\u2264\u2265\u00b1]?\s*\d", w[4]):
                nums.append(((w[0] + w[2]) / 2.0, w[4]))
        if nums:
            value_lines.append(nums)
    if not value_lines:
        return []
    merged = re.sub(r"-\s*", "", " ".join([label_prefix] + parts))
    parsed = _parse_header_text(merged)
    if not parsed:
        return []
    name, unit = parsed[0]
    ordered = sorted(spans, key=lambda s: (s[1] + s[2]) / 2.0)
    out: list[Parameter] = []
    for nums in value_lines:
        if len(nums) != len(ordered):
            continue
        nums_sorted = sorted(nums, key=lambda p: p[0])
        ok = True
        for (_, a, b), (cx, _tok) in zip(ordered, nums_sorted):
            if abs(cx - (a + b) / 2.0) > 40.0:
                ok = False
                break
        if not ok:
            continue
        for (model, _, _), (_cx, tok) in zip(ordered, nums_sorted):
            p = _row_param(model, name, unit, tok, t, len(t.rows), doc_name,
                           row_index_1based=len(t.rows) + 1)
            key = _make_key(p.name, p.value, p.unit)
            if key in dedup:
                continue
            dedup.add(key)
            out.append(p)
    return out



# --- «Таблица 1» ЦНС-типа: строки моделей напечатаны rowspan-парами, а значения -
# --- на отдельных под-строках между ними (числа вне ячеек детектора). -----------
# Модели: ЦНС/ЦНСГ/ЦНСМ/ЦНСн + код «13-70», «38-44-РР», «60-198»; пара моделей
# (например, ЦНС 13-280 и ЦНСГ 13-280) делит одну логическую строку таблицы:
# ступени/напор печатаются один раз на блок исполнений, мощность/масса - на пару,
# а подача/частота/кавитационный запас - один раз на серию страницы.
_ROWSPAN_PREFIXES = ("ЦНСГ", "ЦНСМ", "ЦНСн", "ЦНС")
_ROWSPAN_CODE_RE = re.compile(r"^(\d{1,3})-(\d{2,4})(-[A-Za-zА-ЯЁа-яё]{1,4})?$")
_ROWSPAN_NUM_RE = re.compile(r"^[-–—(\u2264\u2265\u00b1]?\s*\d")
_PAREN_DIGITS_RE = re.compile(r"^\((\d+)\)$")
_HEADER_UNIT_PRIORITY = (
    ("м3/с", r"м3/с|м³/с"),
    ("м3/ч", r"м3/ч|м³/ч"),
    ("об/мин", r"об/мин|об/мн"),
    ("кВт", r"кВт"),
    ("кг", r"кг"),
    ("шт", r"шт"),
    ("%", r"%"),
    ("м", r"(?<![а-яёa-z0-9])м(?![а-яёa-z0-9])"),
)
_HEADER_NAME_RE = (
    (r"подач", "Подача"),
    (r"напор", "Напор"),
    (r"мощн", "Мощность"),
    (r"частот|вращен|об/мин|об\s*мин", "Частота вращения"),
    (r"кавитац", "Кавитационный запас"),
    (r"ступен|шт", "Число ступеней"),
    (r"масс|вес", "Масса"),
)


def _word_print_rows(words):
    """Печатные строки страницы (квантование по y0, ~4pt)."""
    ws = sorted([w for w in words if w[4].strip()], key=lambda w: (w[1], w[0]))
    rows: list[list[tuple]] = []
    for w in ws:
        b = round(w[1] / 4.0)
        if rows and b == round(rows[-1][0][1] / 4.0):
            rows[-1].append(w)
        else:
            rows.append([w])
    return rows


def _rowspan_header_cols(rows, models):
    """(name, unit) колонок по словам шапки над первыми строками моделей."""
    y_lim = min(m["y"] for m in models) + 2.0
    top = None
    for r in rows:
        if any(w[4] == "Обозначение" for w in r):
            top = sorted(r, key=lambda w: w[0])
            break
    if not top:
        return None, None
    hw = [w for w in top if w[4].strip()]
    merged: list[tuple] = []
    for w in hw:
        if merged and w[0] - merged[-1][2] <= 3.0:
            prev = merged[-1]
            merged[-1] = (prev[0], min(prev[1], w[1]), max(prev[2], w[2]), max(prev[3], w[3]),
                          prev[4] + " " + w[4])
        else:
            merged.append(w)
    hw = merged
    bounds = []
    for i, w in enumerate(hw):
        if i + 1 < len(hw):
            bounds.append((w[2] + hw[i + 1][0]) / 2.0)
        else:
            bounds.append(w[2] + 40.0)
    col_texts: dict[int, list[str]] = {}
    for r in rows:
        ry1 = max(w[3] for w in r)
        if ry1 > y_lim:
            break
        for w in sorted(r, key=lambda w: w[0]):
            cx = (w[0] + w[2]) / 2.0
            for j, b in enumerate(bounds):
                if cx <= b:
                    col_texts.setdefault(j, []).append(w[4])
                    break
    def _unit(text):
        low = " ".join(text).lower()
        for u, rx in _HEADER_UNIT_PRIORITY:
            if re.search(rx, low, re.IGNORECASE):
                return u
        return None
    def _name(text):
        low = " ".join(text).lower()
        for rx, nm in _HEADER_NAME_RE:
            if re.search(rx, low, re.IGNORECASE):
                return nm
        return None
    hdrs = {}
    for j, text in col_texts.items():
        if j == 0:
            continue
        nm = _name(text)
        if nm:
            hdrs[j] = (nm, _unit(text))
    return hdrs, bounds


def _rowspan_matrix_pairs(page_words, doc_name: str, dedup: set[tuple]):
    """Параметры матриц с rowspan-строками (ЦНС «Таблица 1» и подобные)."""
    pages = []
    for pi, words in enumerate(page_words):
        rows = _word_print_rows(words)
        models: list[dict] = []
        nums: list[dict] = []
        for r in rows:
            r = sorted(r, key=lambda w: w[0])
            texts = [w[4] for w in r]
            found = None
            for i, t in enumerate(texts):
                if t in _ROWSPAN_PREFIXES:
                    for k in range(i + 1, min(i + 3, len(texts))):
                        m = _ROWSPAN_CODE_RE.match(texts[k])
                        if m:
                            found = (t, texts[k], m)
                            break
                        mm = re.match(r"^(\d{1,3})-(\d{2,4})$", texts[k])
                        if mm and k + 1 < len(texts) and texts[k + 1].upper() in ("РР", "PP"):
                            code = texts[k] + "-РР"
                            found = (t, code, _ROWSPAN_CODE_RE.match(code))
                            break
                    break
            y0 = min(w[1] for w in r)
            y1 = max(w[3] for w in r)
            yc = (y0 + y1) / 2.0
            if found:
                pref, code, m = found
                models.append({"name": (pref + " " + code).replace("-PP", "-РР").replace("-Pp", "-РР"),
                               "series": m.group(1), "code": code, "y": yc})
            for w in r:
                cx = (w[0] + w[2]) / 2.0
                if 100.0 < cx < 400.0 and _ROWSPAN_NUM_RE.match(w[4]):
                    nums.append({"y": yc, "cx": cx, "tok": w[4]})
        if len(models) < 4:
            continue
        hdrs, bounds = _rowspan_header_cols(rows, models)
        if not hdrs or not bounds:
            continue

        def _col_of(cx):
            for j, b in enumerate(bounds):
                if cx <= b:
                    return j
            return len(bounds)

        for n in nums:
            n["col"] = _col_of(n["cx"])
        pages.append({"index": pi, "models": models, "nums": nums,
                      "hdrs": hdrs, "bounds": bounds})

    if not pages:
        return []
    # пары моделей и блоки исполнений (серия-напор без суффикса)
    pairs_by_page = []
    for pg in pages:
        pairs: list[dict] = []
        for m in pg["models"]:
            if pairs and pairs[-1]["code"] == m["code"] and len(pairs[-1]["models"]) < 2:
                pairs[-1]["models"].append(m)
            else:
                pairs.append({"code": m["code"], "models": [m], "own": {}})
        blocks: list[dict] = []
        for pr in pairs:
            base = re.sub(r"-[A-Za-zА-ЯЁа-яё]{1,4}$", "", pr["code"])
            if blocks and blocks[-1]["base"] == base:
                blocks[-1]["pairs"].append(pr)
            else:
                blocks.append({"base": base, "pairs": [pr]})
        pairs_by_page.append((pg, pairs, blocks))

    model_params: dict[str, dict[str, str]] = {}
    series_tokens: dict[str, dict[int, set[str]]] = collections.defaultdict(lambda: collections.defaultdict(set))
    for pg, pairs, blocks in pairs_by_page:
        centers = [(p["models"][0]["y"] + p["models"][-1]["y"]) / 2.0 for p in pairs]
        for bl in blocks:
            first_y = bl["pairs"][0]["models"][0]["y"]
            last_y = bl["pairs"][-1]["models"][-1]["y"]
            b13: dict[int, list[tuple[str, float]]] = collections.defaultdict(list)
            for v in pg["nums"]:
                if first_y - 6.0 <= v["y"] <= last_y + 8.0 and v["col"] in (1, 3):
                    b13[v["col"]].append((v["tok"], v["y"]))
            block_vals = {}
            for col, key in ((1, "Число ступеней"), (3, "Напор")):
                toks = sorted({t for t, _ in b13[col]})
                if len(toks) == 1:
                    block_vals[key] = toks[0]
            for pr in bl["pairs"]:
                ci = pairs.index(pr)
                yc = centers[ci]
                lo = (centers[ci - 1] + yc) / 2.0 if ci > 0 else (pr["models"][0]["y"] - 16.0)
                hi = (yc + centers[ci + 1]) / 2.0 if ci + 1 < len(centers) else (pr["models"][-1]["y"] + 16.0)
                own: dict[int, list[tuple[str, float]]] = collections.defaultdict(list)
                for v in pg["nums"]:
                    if lo <= v["y"] <= hi and v["col"] in (6, 7):
                        own[v["col"]].append((v["tok"], v["y"]))
                pr["own"] = {col: sorted({t for t, _ in lst}, key=lambda s: s)
                             for col, lst in own.items()}
            # масса печатается один раз на блок — она общая для всех пар исполнений
            mass_toks = [t for pr in bl["pairs"] for t in pr["own"].get(7, [])]
            if len(mass_toks) == 1 and len(bl) > 1:
                for pr in bl["pairs"]:
                    pr["own"][7] = list(mass_toks)
            # мощность печатается в ячейке, объединённой с базовой парой (ЦНС/ЦНСГ):
            # у пары «-РР» значение одно на обе, копируем его базовой паре
            pp_power = None
            for pr in bl["pairs"]:
                if "-РР" in pr["code"].replace("-PP", "-РР") and len(pr["own"].get(6, [])) == 1:
                    pp_power = pr["own"][6][0]
            for pr in bl["pairs"]:
                own = pr["own"]
                vals = dict(block_vals)
                for col, key in ((6, "Мощность"), (7, "Масса")):
                    if len(own.get(col, [])) == 1:
                        vals[key] = own[col][0]
                if (not own.get(6) and pp_power is not None and "-РР" not in pr["code"].replace("-PP", "-РР")
                        and all(m["name"].split(" ", 1)[0] in ("ЦНС", "ЦНСГ")
                                for m in pr["models"])):
                    vals["Мощность"] = pp_power
                for m in pr["models"]:
                    d = model_params.setdefault(m["name"], {"series": m["series"], "p": {}})
                    d.setdefault("page", pg["index"] + 1)
                    for key, val in vals.items():
                        d["p"][key] = val
        # серийные характеристики (подача/частота/кавитационный запас)
        for ser in {m["series"] for m in pg["models"]}:
            ser_models = [m for m in pg["models"] if m["series"] == ser]
            y0 = min(m["y"] for m in ser_models) - 10.0
            y1 = max(m["y"] for m in ser_models) + 6.0
            for v in pg["nums"]:
                if not (y0 <= v["y"] <= y1) or v["col"] not in (2, 4, 5):
                    continue
                if _PAREN_DIGITS_RE.match(v["tok"]):
                    if v["col"] == 4:
                        series_tokens[ser][4].add(v["tok"])
                    continue
                if v["col"] == 4:
                    continue
                series_tokens[ser][v["col"]].add(v["tok"])
    # серийные значения применяются ко всем моделям своей серии (печатаются один раз)
    series_vals: dict[str, dict[str, str]] = {}
    for ser, cols in series_tokens.items():
        sv = {}
        if len(cols.get(2, set())) == 1:
            sv["Подача"] = next(iter(cols[2]))
        pn = {m.group(1) for t in cols.get(4, set()) if (m := _PAREN_DIGITS_RE.match(t))}
        if len(pn) == 1:
            sv["Частота вращения"] = next(iter(pn))
        if len(cols.get(5, set())) == 1:
            sv["Кавитационный запас"] = next(iter(cols[5]))
        if sv:
            series_vals[ser] = sv
    for name, d in model_params.items():
        sv = series_vals.get(d["series"])
        if sv:
            d["p"].update(sv)
    # материализация пар «модель × параметр»
    out: list[Parameter] = []
    hdrs_by_col = {}
    for pg in pages:
        for j, (nm, unit) in pg["hdrs"].items():
            hdrs_by_col[j] = (nm, unit)
    for name, d in model_params.items():
        for key, val in d["p"].items():
            unit = next((u for j, (nm, u) in hdrs_by_col.items() if nm == key), None)
            quote = f"{name} | {key} ({unit or '-'}) : {val}"
            p_ = Parameter(
                name=f"{key} ({name})",
                value=val,
                unit=unit,
                evidence=Evidence(document=doc_name, page=d.get("page"),
                              quote=collapse_ws(quote)[:200]),
                source="preset",
                verified=True,
            )
            key_ = _make_key(p_.name, p_.value, p_.unit)
            if key_ in dedup:
                continue
            dedup.add(key_)
            out.append(p_)
    return out



# --- «транспонированные» grid-таблицы (модели в шапке): 1Кs Таблицы 1/2 ----
_GRID_MODEL_CELL_RE = re.compile(r"^[А-ЯЁа-яёA-Za-z0-9]{0,6}?\d{1,3}-\d{1,4}(?:-\d{1,4})?$")
_GRID_NUM_WORD_RE = re.compile(r"^[-–—(\u2264\u2265\u00b1]?\s*\d")
_GRID_SKIP_SUB = ("энергопитани", "утечк")


def _grid_model_name(code: str, prefix: str) -> str:
    """Полное имя модели: «20-50» + префикс «1Кс» -> «1Кс 20-50»;
    «1Кс12-50-...» -> «1Кс 12-50»."""
    m = re.match(r"^(\d?[А-ЯЁа-яёA-Za-z]{1,6})(\d[\d\-–]*)$", code)
    if m and re.search(r"[А-ЯЁа-яёA-Za-z]", m.group(1)):
        return f"{m.group(1)} {m.group(2)}"
    if prefix:
        return f"{prefix} {code}"
    return code


def _grid_transposed_pairs(tables: list[Table], doc_name: str, dedup: set[tuple],
                           page_words) -> list[Parameter]:
    """Пары «модель × параметр» из транспонированных grid-таблиц.

    Только таблицы, собранные сеточным детектором (source="grid"): модели
    напечатаны в шапке («20-50»…, «1Кс12-50-...»), строки = показатели, значение
    показателя печатается между строками его многострочной подписи. Значения
    читаются по словам страницы в полосе grid-строки и привязываются к модельным
    колонкам по x-центру. Одно значение на строку считается общим для сегмента
    (печатается один раз) и размножается на все модели строки.
    """
    out: list[Parameter] = []
    for t in tables:
        if getattr(t, "source", None) != "grid":
            continue
        rows = t.rows
        if (t.page is None or t.page >= len(page_words) or len(rows) < 5
                or not t.bbox or len(t.bbox) != 4):
            continue
        page = page_words[t.page]
        # строка моделей в шапке: >=3 ячеек-кодов типоразмеров
        model_row: int | None = None
        model_cols: dict[int, tuple[str, float, float]] = {}
        for ri, row in enumerate(rows[:4]):
            cells = []
            for c in row[1:]:
                txt = collapse_ws(c.text).strip()
                if not txt or c.x0 is None or c.x1 is None:
                    continue
                code = re.sub(r"[.\u2026]+$", "", txt).rstrip("-\u2013\u2014")
                if not code or not re.search(r"\d", code) or not _GRID_MODEL_CELL_RE.match(code):
                    continue
                cells.append((c, code))
            if len(cells) < 3:
                continue
            prefix = ""
            if all(re.match(r"^\d", code) for _c, code in cells):
                # семейство в заголовке над строкой кодов («… насоса (агрегата) 1Кс»)
                for hrow in rows[:ri]:
                    for c in hrow:
                        m = re.search(r"\d[А-ЯЁа-яёA-Za-z]{1,6}(?![А-ЯЁA-Za-z0-9])",
                                      collapse_ws(c.text))
                        if m:
                            prefix = m.group(0)
                            break
                    if prefix:
                        break
            model_row = ri
            model_cols = {}
            for c, code in cells:
                model_cols[c.col] = (_grid_model_name(code, prefix), c.x0, c.x1)
            break
        if model_row is None or len(model_cols) < 3:
            continue
        j0 = min(model_cols)
        spans = sorted((name, x0, x1) for name, x0, x1 in model_cols.values())
        model_x0 = min(x0 for _n, x0, _x1 in spans)
        model_names = [n for n, _a, _b in spans]

        ymin: list[float | None] = []
        ymax: list[float | None] = []
        for row in rows:
            y0s = [c.y0 for c in row if c.y0 is not None]
            y1s = [c.y1 for c in row if c.y1 is not None]
            ymin.append(min(y0s) if y0s else None)
            ymax.append(max(y1s) if y1s else None)

        def band(ri: int) -> tuple[float, float]:
            lo = ymin[ri] if ymin[ri] is not None else 0.0
            hi = ymax[ri] if ymax[ri] is not None else 0.0
            if ri > 0 and ymin[ri] is not None and ymax[ri - 1] is not None:
                lo = (ymax[ri - 1] + ymin[ri]) / 2.0
            if ri + 1 < len(rows) and ymax[ri] is not None and ymin[ri + 1] is not None:
                hi = (ymax[ri] + ymin[ri + 1]) / 2.0
            return lo, hi

        def row_label(ri: int) -> str:
            row = rows[ri]
            return " ".join(c.text.strip() for c in row if c.col < j0 and c.text.strip())

        def vals_in(lo: float, hi: float) -> list[tuple]:
            return [w for w in page
                    if lo <= (w[1] + w[3]) / 2.0 <= hi
                    and w[0] >= model_x0 - 3.0 and _GRID_NUM_WORD_RE.match(w[4])
                    and _looks_like_value(w[4])]

        def blocks_text(ws: list[tuple]) -> list[tuple[str, str | None, tuple[float, float]]]:
            """(имя, единица, x-диапазон) колонок подписи (дефисные переносы склеены)."""
            ws = sorted(ws, key=lambda w: (w[0], w[1]))
            blocks: list[dict] = []
            for w in ws:
                if blocks and w[0] - blocks[-1]["x1"] <= 4.0:
                    b = blocks[-1]
                    b["words"].append(w)
                    b["x1"] = max(b["x1"], w[2])
                else:
                    blocks.append({"x0": w[0], "x1": w[2], "words": [w]})
            res = []
            for b in blocks:
                lines = _word_print_rows(b["words"])
                parts = []
                for ln in lines:
                    ln = sorted(ln, key=lambda w: w[0])
                    parts.append(" ".join(w[4] for w in ln))
                text = collapse_ws(re.sub(r"-\s+", "", " ".join(parts)))
                parsed = _parse_header_text(text) if text else []
                if len(parsed) == 1:
                    nm, un = parsed[0]
                    if nm == "КПД" and un is None and "%" in text:
                        un = "%"
                    res.append((nm, un, (b["x0"], b["x1"])))
            return res

        def parse_label(label_text: str) -> tuple[str | None, str | None]:
            parsed = _parse_header_text(label_text) if label_text else []
            if not parsed:
                return None, None
            nm, un = parsed[0]
            if nm == "КПД" and un is None and "%" in label_text:
                un = "%"
            return nm, un

        def emit_row(ri: int, name: str, unit: str | None, lo: float, hi: float):
            vals = vals_in(lo, hi)
            assigned: dict[str, list[str]] = {}
            for w in vals:
                cx = (w[0] + w[2]) / 2.0
                model = spans[0][0]
                best_d = 1e9
                for nm, x0, x1 in spans:
                    if x0 - 1.5 <= cx <= x1 + 1.5:
                        model = nm
                        break
                    d = abs((x0 + x1) / 2.0 - cx)
                    if d < best_d:
                        best_d = d
                        model = nm
                assigned.setdefault(model, []).append(w[4].strip())
            if not vals:
                return
            if len(vals) == 1:
                # одно значение напечатано на сегмент — общее для всех моделей строки
                shared = [tok for toks in assigned.values() for tok in toks]
                assigned = {m: list(shared) for m in model_names}
            for model in sorted(assigned):
                for tok in assigned[model]:
                    p = Parameter(
                        name=f"{name} ({model})",
                        value=collapse_ws(tok),
                        unit=unit,
                        evidence=Evidence(
                            document=doc_name,
                            page=(t.page + 1),
                            quote=f"{model} | {name} ({unit or '-'}) : {tok}"[:200],
                            table=t.id, row=(ri + 1)),
                        source="preset", verified=True,
                    )
                    key = _make_key(p.name, p.value, p.unit)
                    if key in dedup:
                        continue
                    dedup.add(key)
                    out.append(p)

        # данные под строками моделей
        for ri in range(model_row + 1, len(rows)):
            if not rows[ri]:
                continue
            label_text = row_label(ri)
            if any(s in label_text.lower() for s in _GRID_SKIP_SUB):
                continue
            lo, hi = band(ri)
            if hi <= lo or not vals_in(lo, hi):
                continue
            name, unit = parse_label(label_text)
            if name is None:
                # многострочная подпись занимает несколько grid-строк
                # («Потребляемая мощность насоса, кВт: номинальная/максимальная»)
                lo2, hi2 = lo, hi
                ri2 = ri
                while ri2 + 1 < len(rows):
                    nxt = row_label(ri2 + 1)
                    if parse_label(nxt)[0] is not None:
                        break
                    lo2 = min(lo2, band(ri2 + 1)[0])
                    hi2 = max(hi2, band(ri2 + 1)[1])
                    ri2 += 1
                label_cache: dict[tuple[float, float], tuple[str, str | None]] = {}
                key = (round(lo2, 1), round(hi2, 1))
                if key not in label_cache:
                    blk = blocks_text([w for w in page if w[4].strip()
                                       and w[2] <= model_x0 + 1.0
                                       and lo2 <= (w[1] + w[3]) / 2.0 <= hi2])
                    label_cache[key] = blk[0][:2] if blk else (None, None)
                name, unit = label_cache[key]
                if name is not None:
                    # каждая строка группы несёт свои значения
                    for gr in range(ri, ri2 + 1):
                        glo, ghi = band(gr)
                        if glo < ghi and vals_in(glo, ghi):
                            emit_row(gr, name, unit, glo, ghi)
                continue
            emit_row(ri, name, unit, lo, hi)
    return out


def extract_matrices(tables: list[Table], doc_name: str, units: list[str],
                     dedup: set[tuple] | None = None, page_words=None) -> list[Parameter]:
    """Извлечь пары «модель × параметр» из матричных таблиц (обе ориентации)."""
    seen = dedup if dedup is not None else set()
    out = _extract_model_rows(tables, doc_name, seen, page_words)
    out.extend(_extract_transposed(tables, doc_name, seen, page_words))
    out.extend(_ecv_feed_models(tables, doc_name, seen))
    if page_words:
        out.extend(_rowspan_matrix_pairs(page_words, doc_name, seen))
        out.extend(_grid_transposed_pairs(tables, doc_name, seen, page_words))
    return out
