"""Extract comparable factual values from Chinese policy statements."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re


@dataclass(frozen=True, slots=True, order=True)
class SemanticFact:
    kind: str
    value: str
    unit: str


_DURATION = re.compile(
    r"(?P<number>\d+(?:\.\d+)?)\s*(?:个)?(?P<unit>自然日|工作日|天|日|个月|月|年)"
)
_PERCENT = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*%")
_AMOUNT = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<scale>万)?元")

_DURATION_UNITS = {
    "自然日": "calendar_day",
    "天": "calendar_day",
    "日": "calendar_day",
    "工作日": "business_day",
    "个月": "month",
    "月": "month",
    "年": "year",
}


def extract_semantic_facts(text: str) -> set[SemanticFact]:
    """Return normalized facts whose unit semantics must be preserved."""

    facts: set[SemanticFact] = set()
    for match in _DURATION.finditer(text):
        facts.add(
            SemanticFact(
                kind="duration",
                value=_normalize_number(match.group("number")),
                unit=_DURATION_UNITS[match.group("unit")],
            )
        )
    for match in _PERCENT.finditer(text):
        facts.add(
            SemanticFact(
                kind="percentage",
                value=_normalize_number(match.group("number")),
                unit="percent",
            )
        )
    for match in _AMOUNT.finditer(text):
        value = Decimal(match.group("number"))
        if match.group("scale"):
            value *= Decimal("10000")
        facts.add(
            SemanticFact(
                kind="amount",
                value=_normalize_decimal(value),
                unit="cny",
            )
        )
    return facts


def describe_fact(fact: SemanticFact) -> str:
    unit_names = {
        "calendar_day": "自然日",
        "business_day": "工作日",
        "month": "个月",
        "year": "年",
        "percent": "%",
        "cny": "元",
    }
    return f"{fact.value}{unit_names.get(fact.unit, fact.unit)}"


def canonicalize_semantic_facts(text: str) -> str:
    """Replace supported quantitative facts with deterministic proof tokens."""

    def duration_token(match: re.Match[str]) -> str:
        value = _normalize_number(match.group("number"))
        unit = _DURATION_UNITS[match.group("unit")]
        return f"<duration:{value}:{unit}>"

    def percent_token(match: re.Match[str]) -> str:
        return f"<percentage:{_normalize_number(match.group('number'))}:percent>"

    def amount_token(match: re.Match[str]) -> str:
        value = Decimal(match.group("number"))
        if match.group("scale"):
            value *= Decimal("10000")
        return f"<amount:{_normalize_decimal(value)}:cny>"

    normalized = _DURATION.sub(duration_token, text)
    normalized = _PERCENT.sub(percent_token, normalized)
    return _AMOUNT.sub(amount_token, normalized)


def _normalize_number(value: str) -> str:
    try:
        return _normalize_decimal(Decimal(value))
    except InvalidOperation:
        return value


def _normalize_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")
