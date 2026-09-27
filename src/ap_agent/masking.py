"""Masks long digit runs, such as bank account numbers, before text reaches logs or the
model. Only the last four digits stay visible (FIN-POL-004 §2, FIN-POL-010 §2)."""

import re
from typing import Any

LONG_DIGITS = re.compile(r"\d{6,}")


def mask(text: str) -> str:
    return LONG_DIGITS.sub(lambda m: "*" * (len(m[0]) - 4) + m[0][-4:], text)


def mask_values(value: Any) -> Any:
    """Mask every string inside a JSON-like value."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {key: mask_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_values(item) for item in value]
    return value
