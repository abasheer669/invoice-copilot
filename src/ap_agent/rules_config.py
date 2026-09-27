"""Typed access to rules_config.yaml, the rule values copied from the policies."""

from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

CENT = Decimal("0.01")
RULES_PATH = Path(__file__).with_name("rules_config.yaml")

LineKind = Literal["goods", "services", "freight"]


class Tolerance(BaseModel):
    max_abs: Decimal
    max_pct: Decimal | None = None


class Tolerances(BaseModel):
    goods: Tolerance
    services: Tolerance
    freight: Tolerance

    def limit_for(self, kind: LineKind, po_line_value: Decimal) -> Decimal:
        """The lower of the absolute cap and the percentage of the PO line value."""
        rule: Tolerance = getattr(self, kind)
        limit = rule.max_abs
        if rule.max_pct is not None:
            limit = min(limit, po_line_value * rule.max_pct)
        return limit.quantize(CENT, ROUND_HALF_UP)


class Duplicates(BaseModel):
    fuzzy_days: int
    fuzzy_amount_pct: Decimal


class Authority(BaseModel):
    limits: dict[str, Decimal | None]
    co_approval_role: str
    new_vendor_days: int
    recent_bank_change_days: int
    home_country: str

    def role_for(self, amount: Decimal) -> tuple[str, Decimal | None]:
        """The lowest role whose limit covers the amount."""
        by_limit = sorted(self.limits.items(), key=lambda item: (item[1] is None, item[1] or 0))
        for role, limit in by_limit:
            if limit is None or amount <= limit:
                return role, limit
        raise ValueError(f"no role can approve {amount}; add a role with no cap")


class Segregation(BaseModel):
    distinct_roles_above: Decimal


class Fraud(BaseModel):
    escalate_at: int
    urgency_terms: list[str]
    bypass_terms: list[str]


class RulesConfig(BaseModel):
    version: str
    sources: dict[str, str]
    home_currency: str
    tolerances: Tolerances
    duplicates: Duplicates
    authority: Authority
    segregation: Segregation
    fraud: Fraud

    def cite(self, doc_id: str, section: str) -> str:
        return f"{doc_id} v{self.sources[doc_id]} {section}"


@lru_cache
def load_rules(path: Path = RULES_PATH) -> RulesConfig:
    return RulesConfig.model_validate(yaml.safe_load(path.read_text()))
