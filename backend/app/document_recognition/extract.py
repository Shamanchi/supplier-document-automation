"""Извлечение реквизитов из текста распознанного PDF.

Классификация и эвристики построены по типовым паспортам и сертификатам
противопожарного оборудования (корпус OCR_prices/Прайсы/прайсы/*):

- наименование изделия — первая и вторая страницы паспорта, строки перед
  маркером «Паспорт» / «Руководство по эксплуатации» / «Формуляр»;
- сертификат — предпоследняя и последняя страницы паспорта («сведения о
  сертификации», «Качество подтверждено сертификатом соответствия № …»),
  у файла сертификата номер ищется по всему тексту.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DOC_CERTIFICATE = "certificate"
DOC_PASSPORT = "passport"
DOC_OTHER = "other"

# Маркеры в порядке «сначала самое специфичное». Классифицируем по первым
# двум страницам: у паспорта слова о сертификации стоят в конце документа и
# не должны перевешивать маркер «Паспорт» на титуле.
_CERTIFICATE_MARKERS = (
    "СЕРТИФИКАТ СООТВЕТСТВИЯ",
    "СЕРТИФИКАТ ПОЖАРНОЙ БЕЗОПАСНОСТИ",
    "ЕВРАЗИЙСКИЙ ЭКОНОМИЧЕСКИЙ СОЮЗ",
)
_PASSPORT_MARKERS = (
    "ФОРМУЛЯР",
    "ПАСПОРТ",
    "РУКОВОДСТВО ПО ЭКСПЛУАТАЦИИ",
    "РУКОВОДСТВО ПО МОНТАЖУ",
    "ИНСТРУКЦИЯ ПО ЭКСПЛУАТАЦИИ",
)

# --- наименование изделия -------------------------------------------------

_SECTION_START_RE = re.compile(r"^\s*\d{1,2}\s*([\.\)]|$)\s*[А-ЯЁA-Z]")
_PAGE_NUMBER_RE = re.compile(r"^\s*[-–—]?\s*\d{1,3}\s*[-–—]?\s*$")
_PASSPORT_CODE_RE = re.compile(
    r"^[А-ЯЁA-Z0-9][А-ЯЁA-Z0-9\.\-\/]{2,30}\s+(ПС|РЭ|ФО|ИЭ|РЭУ|ЭД)\s*$"
)
_SHORT_CODE_RE = re.compile(r"^[А-ЯЁA-Z]{2,4}\d{1,3}$")
_STOP_WORDS = (
    "СЕРИЙНЫЙ ВЫПУСК",
    "НАСТОЯЩЕЕ РУКОВОДСТВО",
    "РУКОВОДСТВО ПО ЭКСПЛУАТАЦИИ",
    "И РУКОВОДСТВО ПО ЭКСПЛУАТАЦИИ",
    "ИНСТРУКЦИЯ ПО ЭКСПЛУАТАЦИИ",
    "И РУКОВОДСТВО",
    "ПАСПОРТ И",
    "ПАСПОРТ",
    "ФОРМУЛЯР",
    "ТЕХНИЧЕСКИЕ УСЛОВИЯ",
)
_JUNK_PATTERNS = (
    re.compile(r"\b(?:www\.|http|@|е\-?mail|тел\.?|факс|факсимиле)", re.I),
    re.compile(r"\b(?:Россия|России|Российск|Беларусь|Белоруссия|Казахстан|Украина)\b"),
    re.compile(r"\b(?:г\.|ул\.|пр\-т|проспект|пер\.|д\.)\s*[А-ЯЁA-Z]", re.I),
    re.compile(r"^[А-ЯЁA-Z]{2,8}(?:®|™)?(\s*«[^»]{1,40}»)?\s*$"),  # бренд строкой (ТУНГУС®)
    re.compile(r"^[А-ЯЁA-Z]{2,6}[а-яё]{2,}$"),  # «логотип» со смешанным регистром (АОЖшша)
    re.compile(r"\.(?:ru|by|com|net|org)\b", re.I),
    re.compile(r"филиал", re.I),
    re.compile(r"обслуживание заказчиков", re.I),
    re.compile(r"благодарим", re.I),
    re.compile(r"торговой марки", re.I),
    re.compile(r"^[«\"']?[А-ЯЁA-Z][а-яёa-z]+(\s+[А-ЯЁA-Z][а-яёa-z]+){0,3}[»\"']?$"),
    re.compile(r"^\s*(?:№|номер)\s+", re.I),
)
_COMPANY_RE = re.compile(
    r"^\s*(?:акционерн|закрытое|открытое|общество|ооо|зао|оао|ао|ип|"
    r"предприятие|изготовитель|производитель|заявитель|поставщик)[\s«(]",
    re.I,
)


def _looks_junk(line: str) -> bool:
    if len(line) < 3:
        return True
    if _SECTION_START_RE.match(line):
        return True
    if _PAGE_NUMBER_RE.match(line):
        return True
    if _PASSPORT_CODE_RE.match(line):
        return True
    if _COMPANY_RE.match(line):
        return True
    upper = line.upper()
    return any(pattern.search(upper) for pattern in _JUNK_PATTERNS)


def _marker_line(line: str) -> str | None:
    """Вернуть название маркера, если строка ОТКРЫВАЕТ титул паспорта.

    Маркер засчитывается только в начале строки: предложение «Настоящее
    руководство по эксплуатации предназначено…» или упоминание «…паспорт
    огнетушителя…» в тексте титулом не являются.
    """
    upper = re.sub(r"\s+", " ", line.upper().strip())
    if upper.startswith("ФОРМУЛЯР"):
        return "ФОРМУЛЯР"
    if upper.startswith("ПАСПОРТ"):
        return "ПАСПОРТ"
    for marker in (
        "РУКОВОДСТВО ПО ЭКСПЛУАТАЦИИ",
        "РУКОВОДСТВО ПО МОНТАЖУ",
        "ИНСТРУКЦИЯ ПО ЭКСПЛУАТАЦИИ",
    ):
        if upper.startswith(marker):
            return marker
    return None


def _is_bare_code(part: str, cleaned: list[str]) -> bool:
    """Повторяется ли «голый» шифр/обозначение («ОСП-1») в уже собранных строках."""
    if any(ch.isspace() for ch in part):
        return False
    def norm(text: str) -> str:
        return re.sub(r"[^А-ЯЁA-Z0-9]", "", text.upper())
    code = norm(part)
    if len(code) < 3:
        return False
    return any(code in norm(item) for item in cleaned)


def _clean_candidate_parts(parts: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        part = re.sub(r"\s+", " ", part).strip(" .;,:-–—")
        if not part or len(part) < 3:
            continue
        if part in seen:
            continue
        if _SHORT_CODE_RE.match(part) and cleaned:
            # «ПБ04» между строками наименования — служебный номер чертежа.
            continue
        if cleaned and _is_bare_code(part, cleaned):
            # «ОСП-1» после строки «ОСП-1 УХЛ2.1 …» — повтор, а не часть названия.
            continue
        seen.add(part)
        cleaned.append(part)
    return cleaned


def _cut_stop_words(text: str) -> str:
    upper = text.upper()
    for stop in _STOP_WORDS:
        if not stop:
            continue
        idx = upper.find(stop)
        if idx > 0:
            text = text[:idx]
    return text


def _title_case(text: str) -> str:
    """Первая буква — заглавная, остальное как в документе."""
    text = text.strip()
    if not text:
        return text
    return text[0].upper() + text[1:]


def _leading_name_candidates(lines: list[str], marker_idx: int) -> list[str]:
    """Строки названия: до маркера, либо сразу после него (титул «ПАСПОРТ/РЭ…»)."""
    before = [
        line
        for line in lines[:marker_idx]
        if not _looks_junk(line)
    ]
    if before:
        return before
    # Маркер открывает страницу («ПАСПОРТ / РЭ 28.29.22-… / (Руководство по
    # эксплуатации) / на огнетушители …»): название идёт сразу за служебными
    # строками титула.
    after: list[str] = []
    for line in lines[marker_idx + 1 : marker_idx + 6]:
        upper = line.upper()
        if (
            upper.startswith("РЭ ")
            or "РУКОВОДСТВО ПО ЭКСПЛУАТАЦИИ" in upper
            or "ИНСТРУКЦИЯ ПО ЭКСПЛУАТАЦИИ" in upper
            or _PASSPORT_CODE_RE.match(line)
        ):
            continue
        if _looks_junk(line) or _SECTION_START_RE.match(line):
            break
        after.append(line)
    return after


def _marker_index(lines: list[str]) -> tuple[int, str] | None:
    """(индекс, маркер) первой строки-титула на странице."""
    for idx, line in enumerate(lines):
        marker = _marker_line(line)
        if marker is not None:
            return idx, marker
    return None


def _build_name(candidates: list[str]) -> str | None:
    parts = _clean_candidate_parts(candidates)
    if not parts:
        return None
    name = _cut_stop_words(" ".join(parts))
    name = re.sub(r"\s+", " ", name).strip(" .,;:-–—*")
    # «ПАСПОРТ / … / на огнетушители …»: служебное «НА» в начале не нужно.
    if name.upper().startswith("НА ") and len(name) > 6:
        name = name[3:].strip()
    if not (5 <= len(name) <= 240 and re.search(r"[А-ЯЁA-ZА-Яа-я]", name)):
        return None
    if any(word in name.upper() for word in ("СЕРТИФИКАТ", "ВНИМАНИЕ", "СРОК СЛУЖБЫ")):
        return None
    return _title_case(name)

def find_product_name(pages: tuple[str, ...]) -> str | None:
    """Наименование изделия с первой и второй страниц паспорта.

    Сначала ищем классический титул («… название / ПАСПОРТ», «ФОРМУЛЯР (…)
    … название»); если на первых двух страницах титула нет — пробуем
    заголовочный блок в начале страницы (обложки без слова «Паспорт»).
    """
    if not pages:
        return None
    head = list(pages[:2])

    # Фаза A: строка-титул («Паспорт», «Формуляр», «Руководство по эксплуатации»).
    for page_text in head:
        lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        found = _marker_index(lines)
        if found is None:
            continue
        marker_idx, form = found
        if marker_idx > 20:
            # Маркер стоит глубоко в тексте страницы — это не титул.
            continue
        if form == "ФОРМУЛЯР":
            candidates: list[str] = []
            for line in lines[marker_idx + 1 :]:
                if _SECTION_START_RE.match(line) or _looks_junk(line):
                    break
                candidates.append(line)
                if len(candidates) >= 3:
                    break
        else:
            candidates = _leading_name_candidates(lines, marker_idx)
        name = _build_name(candidates)
        if name:
            return name

    # Фаза B: титул без слова «Паспорт» (обложка-реклама, «ОГНЕТУШИТЕЛЬ …
    # ИНЕИ*»). Пропускаем до 3 ведущих служебных строк (логотип, адрес
    # изготовителя) и останавливаемся на первом же «служебном» тексте.
    for page_text in head:
        lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        start = 0
        while start < min(4, len(lines)) and (
            ":" in lines[start]
            or _looks_junk(lines[start])
            or _SECTION_START_RE.match(lines[start])
        ):
            start += 1
        if start >= len(lines):
            continue
        candidates = []
        for line in lines[start:]:
            if ":" in line or _SECTION_START_RE.match(line) or _looks_junk(line):
                break
            candidates.append(line)
            if len(candidates) >= 4:
                break
        name = _build_name(candidates)
        if name:
            return name
    return None


# --- классификация документа ----------------------------------------------

def _short_title_line(line: str) -> bool:
    """Строка-титул («ПАСПОРТ», «ФОРМУЛЯР»): короткая, без «:»/«№» — не ячейка таблицы."""
    stripped = line.strip()
    return bool(stripped) and len(stripped) <= 60 and ":" not in stripped and "№" not in stripped


def classify(head_text: str, *, pages: tuple[str, ...] | None = None) -> str:
    """Класс документа по первым страницам (паспорт/сертификат/другое).

    Сначала — строка-титул паспорта на первой-второй странице: даже если выше
    на обложке упомянут сертификат («Сертификат соответствия: № … / ПАСПОРТ»),
    документ остаётся паспортом. Иначе — «какой маркер стоит раньше»: у бланка
    сертификата «СЕРТИФИКАТ СООТВЕТСТВИЯ» открывает первую страницу, а
    упоминание сертификата в конце паспорта не перевешивает титул.
    """
    for page_text in (pages or (head_text,)):
        for idx, line in enumerate(page_text.splitlines()):
            if idx > 25:
                break
            marker = _marker_line(line)
            if marker is not None and _short_title_line(line):
                return DOC_PASSPORT
    upper = (head_text or "").upper()
    best: tuple[int, str] = (len(upper), DOC_OTHER)
    for marker in _CERTIFICATE_MARKERS:
        idx = upper.find(marker)
        if idx != -1 and idx < best[0]:
            best = (idx, DOC_CERTIFICATE)
    for marker in _PASSPORT_MARKERS:
        idx = upper.find(marker)
        if idx != -1 and idx < best[0]:
            best = (idx, DOC_PASSPORT)
    return best[1]

# --- сведения о сертификате -----------------------------------------------

_CERT_WORD_RE = re.compile(r"ертификат", re.I)

# Регистрационный номер сертификата ЕАЭС/РФ. Допускает обе раскладки
# (C-RU латиницей и С-RU кириллицей), лишние пробелы внутри номера и
# варианты без «/год» («C-RU.ПБ597.B.00191»).
_CERT_NUMBER_RE = re.compile(
    r"(?:ЕАЭС\s+)?(?:RU\s+)?[СCС]\s*-?\s*RU\."
    r"[А-ЯЁA-Z0-9]{1,12}(?:\.[А-ЯЁA-Z0-9]{1,4})?\.\d{3,8}(?:/\d{2,4})?",
    re.IGNORECASE,
)
# Белорусский сертификат: «BY/112 02.01.033 00536».
_BY_NUMBER_RE = re.compile(r"BY/\d{1,4}\s+\d{2}\.\d{2}\.\d{3}\s+\d{4,6}", re.I)
# Добровольный сертификат СТО: «СТО № 15.00030.125».
_STO_NUMBER_RE = re.compile(r"СТО\s+№?\s*[\d.]{6,}", re.I)
_VALIDITY_RE = re.compile(
    r"(?:действителен|действует|действительно)\s+(?:по|до)\s*"
    r"(\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE,
)
# Бланк сертификата: «СРОК ДЕЙСТВИЯ С 15.11.2021 ПО 14.11.2026».
_VALIDITY_PERIOD_RE = re.compile(
    r"СРОК\s+ДЕЙСТВИЯ\s*(?:С\s*\d{2}\.\d{2}\.\d{4}\s*)?ПО\s*(\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE,
)

_NUMBER_PATTERNS = (_CERT_NUMBER_RE, _BY_NUMBER_RE, _STO_NUMBER_RE)


def _normalize_number(raw: str) -> str:
    """Привести номер к читаемому виду: «ЕАЭС RU C-RU.ЧС13.В.00348/21».

    У номеров ЕАЭС/РФ убираем случайные пробелы внутри («ЧС 13» -> «ЧС13»);
    белорусские и СТО-номера оставляем с разделителями, как в документе.
    """
    value = " ".join(raw.upper().split())
    for candidate in ("ЕАЭС RU ", "RU "):
        if value.startswith(candidate):
            return candidate + value[len(candidate):].replace(" ", "")
    return value


def _extract_numbers(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _NUMBER_PATTERNS:
        for match in pattern.finditer(text):
            token = _normalize_number(match.group(0))
            if token and token not in seen:
                seen.add(token)
                found.append(token)
    return found


def _mention_label(text: str) -> str:
    upper = text.upper()
    if "СООТВЕТСТВИЯ" in upper:
        return "сертификат соответствия"
    if "ПОЖАРНОЙ БЕЗОПАСНОСТИ" in upper:
        return "сертификат пожарной безопасности"
    if "СЕРТИФИКАЦИИ" in upper:
        return "сведения о сертификации"
    return "сертификат"


@dataclass(frozen=True, slots=True)
class CertificateMention:
    """Ссылка на сертификат внутри документа (номер, страница, срок)."""

    page: int
    label: str
    number: str = ""
    valid_until: str = ""
    context: str = ""


def find_certificate_mentions(
    pages: tuple[str, ...],
    *,
    tail_pages: int = 2,
    whole_document: bool = False,
) -> list[CertificateMention]:
    """Сертификаты, указанные в документе.

    Для паспорта смотрим предпоследнюю и последнюю страницы (по умолчанию),
    для файла сертификата — весь текст (`whole_document=True`). Каждый номер
    возвращается один раз вместе со страницей, на которой найден.
    """
    if not pages:
        return []
    selected = list(pages) if whole_document else list(pages[-tail_pages:])
    base_page = 1 if whole_document else max(1, len(pages) - tail_pages + 1)

    mentions: list[CertificateMention] = []
    seen_numbers: set[str] = set()
    for offset, page_text in enumerate(selected):
        page_no = base_page + offset
        lines = page_text.splitlines()
        for idx, line in enumerate(lines):
            if not _CERT_WORD_RE.search(line):
                continue
            window_lines = lines[max(0, idx - 1): idx + 5]
            window = "\n".join(window_lines)
            numbers = _extract_numbers(window)
            validity = _VALIDITY_RE.search(window) or _VALIDITY_PERIOD_RE.search(window)
            number = numbers[0] if numbers else ""
            if number and number in seen_numbers:
                continue
            if number:
                seen_numbers.add(number)
            mentions.append(
                CertificateMention(
                    page=page_no,
                    label=_mention_label(window),
                    number=number,
                    valid_until=(
                        f"{validity.group(1)}" if validity else ""
                    ),
                    context=re.sub(r"\s+", " ", window).strip()[:220],
                )
            )
    # Номера, которые стоят отдельной строкой после «сертификат …» без
    # самого слова в той же строке, уже покрыты окном ±1 строка.
    return mentions