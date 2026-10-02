"""ISO 4217 validation and currency-справочник access."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Currency


# Active alphabetic ISO 4217 codes. Currency evidence extracted from a file
# remains authoritative; this list only prevents a malformed client hint from
# being promoted to a valid-looking price currency.
ISO_4217_CODES = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND
    BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU
    CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS
    GIP GMD GNF GTQ GYD HKD HNL HRK HTG HUF IDR ILS INR IQD IRR ISK JMD JOD
    JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL
    MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR
    NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG
    SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY
    TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG
    XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU XTS XUA XXX YER ZAR
    ZMW ZWG
    """.split()
)


def normalise_currency_hint(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    code = value.strip().upper()
    if code not in ISO_4217_CODES:
        raise ValueError("currency_hint должен быть действующим ISO 4217 кодом")
    return code


async def currency_directory_map(session: AsyncSession) -> dict[str, Currency]:
    """Load the currency справочник keyed by upper-case ISO code."""

    from sqlalchemy import select

    from app.models import Currency

    rows = (await session.execute(select(Currency))).scalars().all()
    return {currency.code: currency for currency in rows}


async def default_currency(session: AsyncSession) -> Currency | None:
    """The project-wide fallback currency (RUB), if the справочник has one."""

    from sqlalchemy import select

    from app.models import Currency

    return await session.scalar(select(Currency).where(Currency.is_default.is_(True)))
