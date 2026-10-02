"""Add the ISO 4217 currency справочник and link price values to it.

Seeds every active ISO 4217 code, marks RUB as the project-wide default,
adds ``supplier_price_values.currency_id`` as the справочник link, backfills
the link for already-resolved prices, and resolves previously-unresolved
prices to the default RUB so the money gate no longer blocks them.

Revision ID: 7a3f8b2c1d40
Revises: 6f9a2c4d8e10
"""

from alembic import context, op
import sqlalchemy as sa
import uuid
from datetime import datetime, timezone

from app.currencies import ISO_4217_CODES


revision = "7a3f8b2c1d40"
down_revision = "6f9a2c4d8e10"
branch_labels = None
depends_on = None


# Display names and symbols for the currencies that realistically appear in
# supplier price lists; the rest of the ISO set is seeded with the code itself
# as the name so the справочник FK always resolves.
CURRENCY_DETAILS: dict[str, tuple[str, str | None]] = {
    "RUB": ("Российский рубль", "₽"),
    "USD": ("Доллар США", "$"),
    "EUR": ("Евро", "€"),
    "KZT": ("Казахстанский тенге", "₸"),
    "BYN": ("Белорусский рубль", "Br"),
    "CNY": ("Китайский юань", "¥"),
    "GBP": ("Фунт стерлингов", "£"),
    "UAH": ("Украинская гривна", "₴"),
    "UZS": ("Узбекский сум", None),
    "TRY": ("Турецкая лира", "₺"),
    "JPY": ("Японская иена", "¥"),
    "CHF": ("Швейцарский франк", None),
    "CZK": ("Чешская крона", "Kč"),
    "PLN": ("Польский злотый", "zł"),
    "SEK": ("Шведская крона", "kr"),
    "NOK": ("Норвежская крона", "kr"),
    "DKK": ("Датская крона", "kr"),
    "INR": ("Индийская рупия", "₹"),
    "KRW": ("Южнокорейская вона", "₩"),
    "BRL": ("Бразильский реал", "R$"),
    "ZAR": ("Южноафриканский рэнд", "R"),
    "AED": ("Дирхам ОАЭ", None),
    "EGP": ("Египетский фунт", None),
    "HUF": ("Венгерский форинт", "Ft"),
    "RON": ("Румынский лей", "L"),
    "BGN": ("Болгарский лев", "лв"),
    "RSD": ("Сербский динар", "дин."),
    "GEL": ("Грузинский лари", "₾"),
    "AMD": ("Армянский драм", "֏"),
    "AZN": ("Азербайджанский манат", "₼"),
    "KGS": ("Киргизский сом", None),
    "TJS": ("Таджикский сомони", None),
    "TMT": ("Туркменский манат", None),
    "MDL": ("Молдавский лей", "L"),
    "VND": ("Вьетнамский дон", "₫"),
    "THB": ("Таиландский бат", "฿"),
    "ILS": ("Израильский шекель", "₪"),
    "MXN": ("Мексиканский песо", "$"),
    "CAD": ("Канадский доллар", "$"),
    "AUD": ("Австралийский доллар", "$"),
    "SGD": ("Сингапурский доллар", "$"),
    "HKD": ("Гонконгский доллар", "$"),
    "SAR": ("Саудовский риял", "﷼"),
    "QAR": ("Катарский риял", "﷼"),
    "PKR": ("Пакистанская рупия", "₨"),
    "BDT": ("Бангладешская така", "৳"),
    "IDR": ("Индонезийская рупия", "Rp"),
    "MYR": ("Малайзийский ринггит", "RM"),
    "PHP": ("Филиппинское песо", "₱"),
    "LKR": ("Шри-ланкийская рупия", "₨"),
    "NPR": ("Непальская рупия", "₨"),
    "MNT": ("Монгольский тугрик", "₮"),
    "IRR": ("Иранский риал", "﷼"),
    "IQD": ("Иракский динар", None),
    "JOD": ("Иорданский динар", None),
    "KWD": ("Кувейтский динар", None),
    "OMR": ("Оманский риал", "﷼"),
    "BHD": ("Бахрейнский динар", None),
    "MAD": ("Марокканский дирхам", None),
    "TND": ("Тунисский динар", None),
    "DZD": ("Алжирский динар", None),
    "LYD": ("Ливийский динар", None),
    "CLP": ("Чилийское песо", "$"),
    "COP": ("Колумбийское песо", "$"),
    "PEN": ("Перуанский соль", "S/"),
    "ARS": ("Аргентинское песо", "$"),
    "UYU": ("Уругвайское песо", "$U"),
    "BOB": ("Боливийский боливиано", "Bs"),
    "PYG": ("Парагвайский гуарани", "₲"),
    "VES": ("Венесуэльский боливар", "Bs.S"),
    "CUP": ("Кубинское песо", "₱"),
    "DOP": ("Доминиканское песо", "RD$"),
    "GTQ": ("Гватемальский кетсаль", "Q"),
    "HNL": ("Гондурасская лемпира", "L"),
    "NIO": ("Никарагуанская кордоба", "C$"),
    "CRC": ("Костариканский колон", "₡"),
    "PAB": ("Панамский бальбоа", "B/."),
    "ISK": ("Исландская крона", "kr"),
    "HRK": ("Хорватская куна", "kn"),
    "MKD": ("Македонский денар", "ден"),
    "ALL": ("Албанский лек", "L"),
    "BAM": ("Конвертируемая марка Боснии", "KM"),
    "NGN": ("Нигерийская найра", "₦"),
    "GHS": ("Ганский седи", "₵"),
    "KES": ("Кенийский шиллинг", "KSh"),
    "TZS": ("Танзанийский шиллинг", "TSh"),
    "UGX": ("Угандийский шиллинг", "USh"),
    "ETB": ("Эфиопский быр", "Br"),
    "XOF": ("Франк КФА BCEAO", "CFA"),
    "XAF": ("Франк КФА BEAC", "FCFA"),
    "ZMW": ("Замбийская квача", "ZK"),
    "BWP": ("Ботсванская пула", "P"),
    "NAD": ("Намибийский доллар", "N$"),
    "MUR": ("Маврикийская рупия", "₨"),
    "SCR": ("Сейшельская рупия", "₨"),
    "MWK": ("Малавийская квача", "MK"),
    "MZN": ("Мозамбикский метикал", "MT"),
    "AOA": ("Ангольская кванза", "Kz"),
    "CDF": ("Конголезский франк", "FC"),
    "BIF": ("Бурундийский франк", "FBu"),
    "RWF": ("Руандийский франк", "FRw"),
    "SOS": ("Сомалийский шиллинг", "Sh"),
    "DJF": ("Франк Джибути", "Fdj"),
    "ERN": ("Эритрейская накфа", "Nkf"),
    "SSP": ("Южносуданский фунт", "£"),
    "SDG": ("Суданский фунт", "£"),
    "GNF": ("Гвинейский франк", "FG"),
    "SLE": ("Леоне Сьерра-Леоне", "Le"),
    "LRD": ("Либерийский доллар", "L$"),
    "GMD": ("Гамбийский даласи", "D"),
    "CVE": ("Эскудо Кабо-Верде", "Esc"),
    "MRU": ("Мавританская угия", "UM"),
    "SHP": ("Фунт Святой Елены", "£"),
    "MGA": ("Малагасийский ариари", "Ar"),
    "KMF": ("Франк Комор", "CF"),
    "MVR": ("Мальдивская руфия", "Rf"),
    "MMK": ("Мьянманский кьят", "K"),
    "KHR": ("Камбоджийский риель", "៛"),
    "LAK": ("Лаосский кип", "₭"),
    "FJD": ("Доллар Фиджи", "FJ$"),
    "PGK": ("Кина Папуа-Новой Гвинеи", "K"),
    "VUV": ("Вату Вануату", "VT"),
    "WST": ("Тала Самоа", "WS$"),
    "TOP": ("Паанга Тонги", "T$"),
    "SBD": ("Доллар Соломоновых островов", "SI$"),
    "BBD": ("Барбадосский доллар", "Bds$"),
    "BZD": ("Белизский доллар", "BZ$"),
    "GYD": ("Гайанский доллар", "GY$"),
    "SRD": ("Суринамский доллар", "Sr$"),
    "TTD": ("Доллар Тринидада и Тобаго", "TT$"),
    "JMD": ("Ямайский доллар", "J$"),
    "HTG": ("Гаитянский гурд", "G"),
    "CUC": ("Кубинский конвертируемый песо", "CUC$"),
    "AWG": ("Арубанский флорин", "ƒ"),
    "ANG": ("Нидерландский антильский гульден", "ƒ"),
    "BMD": ("Бермудский доллар", "BD$"),
    "BND": ("Брунейский доллар", "B$"),
    "BTN": ("Бутанский нгултрум", "Nu."),
    "AFN": ("Афгани", "؋"),
    "KPW": ("Северокорейская вона", "₩"),
    "TWD": ("Новый тайваньский доллар", "NT$"),
    "MOP": ("Патака Макао", "MOP$"),
    "SVC": ("Сальвадорский колон", "₡"),
    "SZL": ("Свазилендский лилангени", "L"),
    "LSL": ("Лоти Лесото", "L"),
    "SYP": ("Сирийский фунт", "£"),
    "LBP": ("Ливанский фунт", "ل.ل"),
    "YER": ("Йеменский риал", "﷼"),
    "VED": ("Венесуэльский боливар дигитал", "Bs.D"),
    "UYI": ("Уругвайское песо (индекс инфляции)", None),
    "UYW": ("Уругвайская индексная единица", None),
    "USN": ("Доллар США (следующий день)", None),
    "BOV": ("Боливийский мвдол", None),
    "CHE": ("Евро WIR", None),
    "CHW": ("Франк WIR", None),
    "CLF": ("Чилийская расчётная единица", None),
    "COU": ("Колумбийская единица реальной стоимости", None),
    "MXV": ("Мексиканская индексная единица", None),
    "XAG": ("Серебро (тройская унция)", None),
    "XAU": ("Золото (тройская унция)", None),
    "XBA": ("Европейская составная единица", None),
    "XBB": ("Европейская денежная единица", None),
    "XBC": ("Европейская расчётная единица 9", None),
    "XBD": ("Европейская расчётная единица 17", None),
    "XCD": ("Восточнокарибский доллар", "EC$"),
    "XCG": ("Карибский гульден", "Cg"),
    "XDR": ("Специальные права заимствования", None),
    "XPD": ("Палладий (тройская унция)", None),
    "XPF": ("Франк КФП", "₣"),
    "XPT": ("Платина (тройская унция)", None),
    "XSU": ("Сукре", None),
    "XTS": ("Код для тестирования", None),
    "XUA": ("Расчётная единица АБР", None),
    "XXX": ("Без валюты", None),
    "ZWG": ("Зимбабвийское золото", "ZiG"),
    "SLL": ("Леоне Сьерра-Леоне (до 2022)", "Le"),
    "STN": ("Добра Сан-Томе и Принсипи", "Db"),
    "FKP": ("Фунт Фолклендских островов", "FK£"),
    "GIP": ("Гибралтарский фунт", "£"),
}

DEFAULT_CURRENCY_CODE = "RUB"


def _seed_rows() -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = []
    for code in sorted(ISO_4217_CODES):
        name, symbol = CURRENCY_DETAILS.get(code, (code, None))
        rows.append(
            {
                "id": uuid.uuid4(),
                "code": code,
                "name": name,
                "symbol": symbol,
                "is_default": code == DEFAULT_CURRENCY_CODE,
                "now": now,
            }
        )
    return rows


def _seed_and_backfill() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    insert = sa.text(
        """
        INSERT INTO currencies (id, code, name, symbol, is_default, created_at, updated_at)
        VALUES (:id, :code, :name, :symbol, :is_default, :now, :now)
        """
    )
    rows = _seed_rows()
    for offset in range(0, len(rows), 500):
        bind.execute(insert, rows[offset : offset + 500])

    # Link already-resolved prices to the справочник.
    bind.execute(
        sa.text(
            """
            UPDATE supplier_price_values AS v
            SET currency_id = c.id
            FROM currencies AS c
            WHERE v.currency IS NOT NULL
              AND v.currency_id IS NULL
              AND c.code = upper(v.currency)
            """
        )
    )
    # Resolve previously-unresolved prices to the default currency (RUB).
    bind.execute(
        sa.text(
            """
            UPDATE supplier_price_values AS v
            SET currency = c.code,
                currency_source = 'default',
                currency_id = c.id
            FROM currencies AS c
            WHERE c.is_default
              AND v.currency IS NULL
              AND v.currency_id IS NULL
              AND (v.amount IS NOT NULL OR v.amount_from IS NOT NULL)
            """
        )
    )
    # The unresolved-currency gate errors are now fixed; drop them and refresh
    # the denormalized per-version error counters.
    bind.execute(
        sa.text(
            "DELETE FROM price_parse_issues WHERE code = 'PRICE_CURRENCY_UNRESOLVED'"
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE price_versions AS pv
            SET errors_count = COALESCE(sub.cnt, 0)
            FROM (
                SELECT price_version_id, COUNT(*) AS cnt
                FROM price_parse_issues
                WHERE severity = 'error'
                GROUP BY price_version_id
            ) AS sub
            WHERE pv.id = sub.price_version_id
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE price_versions AS pv
            SET errors_count = 0
            WHERE pv.errors_count <> 0
              AND NOT EXISTS (
                  SELECT 1 FROM price_parse_issues AS i
                  WHERE i.price_version_id = pv.id AND i.severity = 'error'
              )
            """
        )
    )


def upgrade() -> None:
    op.create_table(
        "currencies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=3), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("symbol", sa.String(length=8), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("code = upper(code)", name="ck_currencies_code_upper"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_currencies_code"),
    )

    op.add_column("supplier_price_values", sa.Column("currency_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_supplier_price_values_currency",
        "supplier_price_values",
        "currencies",
        ["currency_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_supplier_price_values_currency_id",
        "supplier_price_values",
        ["currency_id"],
        unique=False,
    )
    _seed_and_backfill()


def downgrade() -> None:
    op.drop_index(
        "ix_supplier_price_values_currency_id", table_name="supplier_price_values"
    )
    op.drop_constraint(
        "fk_supplier_price_values_currency", "supplier_price_values", type_="foreignkey"
    )
    op.drop_column("supplier_price_values", "currency_id")
    op.drop_table("currencies")
