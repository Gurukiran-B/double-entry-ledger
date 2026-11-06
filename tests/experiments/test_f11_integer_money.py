"""F11: Validate transfer amounts as positive integer minor currency units."""

from decimal import Decimal
from typing import cast

import pytest

from ledger.transfer import _validate_amount_minor


@pytest.mark.parametrize("amount_minor", [1, 25, 10_000])
def test_accepts_positive_integer_minor_units(amount_minor: int) -> None:
    _validate_amount_minor(amount_minor)


@pytest.mark.parametrize("amount_minor", [0, -1])
def test_rejects_non_positive_minor_units(amount_minor: int) -> None:
    with pytest.raises(ValueError, match="Transfer amount must be positive"):
        _validate_amount_minor(amount_minor)


@pytest.mark.parametrize(
    "amount_minor",
    [True, 1.0, Decimal("1.00"), "1"],
)
def test_rejects_non_integer_minor_units(amount_minor: object) -> None:
    with pytest.raises(
        TypeError,
        match="integer number of minor currency units",
    ):
        _validate_amount_minor(cast(int, amount_minor))
