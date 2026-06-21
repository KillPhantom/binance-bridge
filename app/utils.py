from __future__ import annotations

from decimal import Decimal, ROUND_DOWN


def decimal_string(value: Decimal) -> str:
    return format(value, "f")


def round_step_size(quantity: Decimal, step_size: Decimal) -> Decimal:
    if quantity <= 0 or step_size <= 0:
        raise ValueError("quantity and step size must be positive")
    return (quantity / step_size).to_integral_value(rounding=ROUND_DOWN) * step_size
