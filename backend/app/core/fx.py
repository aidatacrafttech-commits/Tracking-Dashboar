"""Currency conversion, strictly opt-in.

Only rates the owner configured (EXCHANGE_RATES) with a documented origin
(EXCHANGE_RATE_SOURCE) are used. With no configured rate, conversion returns None and
callers must refuse the comparison instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Conversion:
    amount: Decimal
    rate: Decimal
    pair: str  # the configured pair used, e.g. "USD/INR"
    inverted: bool
    source: str


class ExchangeRates:
    def __init__(self, rates: dict[str, Decimal], source: str | None) -> None:
        self.rates = rates
        self.source = source

    @property
    def enabled(self) -> bool:
        return bool(self.rates and self.source)

    def convert(self, amount: Decimal, from_ccy: str, to_ccy: str) -> Conversion | None:
        if from_ccy == to_ccy:
            return Conversion(amount, Decimal(1), f"{from_ccy}/{to_ccy}", False, "identity")
        if not self.enabled:
            return None
        direct = f"{from_ccy}/{to_ccy}"
        if direct in self.rates:
            rate = self.rates[direct]
            return Conversion(amount * rate, rate, direct, False, self.source or "")
        inverse = f"{to_ccy}/{from_ccy}"
        if inverse in self.rates:
            rate = self.rates[inverse]
            return Conversion(amount / rate, rate, inverse, True, self.source or "")
        return None
