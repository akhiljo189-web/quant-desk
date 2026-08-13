"""
qd.providers.base — the interfaces every data source and broker implements.

Protocols rather than base classes, so an adapter is anything with the right
methods and the replay provider is not a subclass of anything live. The engine
never imports a vendor module; it receives providers and cannot tell whether it
is trading or being simulated. That property is what makes the backtest a test
OF THE SYSTEM rather than a test of a reimplementation of the system that
happens to share some functions.

Every method takes explicit time bounds. None of them may read the wall clock —
a provider that decides for itself what "now" means cannot be replayed, because
replay works by lying about the time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Protocol, Sequence, runtime_checkable

from qd.types import (
    Bar, EarningsEvent, Fill, NewsItem, OptionTrade, Order, Position, Quote, Side,
)


@dataclass(frozen=True)
class AccountInfo:
    equity: float
    cash: float
    buying_power: float
    currency: str = "USD"
    pattern_day_trader: bool = False
    trading_blocked: bool = False
    account_id: str = ""

    def masked_id(self) -> str:
        """Never log a full account identifier."""
        return f"…{self.account_id[-4:]}" if len(self.account_id) >= 4 else "…"


@dataclass(frozen=True)
class BrokerOrder:
    """An order as the broker sees it."""
    id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: float
    status: str
    filled_quantity: float = 0.0
    filled_avg_price: Optional[float] = None
    submitted_at: Optional[datetime] = None
    order_type: str = "market"
    limit_price: Optional[float] = None

    @property
    def is_open(self) -> bool:
        return self.status in ("new", "accepted", "pending_new", "partially_filled")

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"


class ProviderError(RuntimeError):
    """Any failure reaching or parsing an upstream source."""


class RateLimited(ProviderError):
    """Upstream asked us to slow down."""


@runtime_checkable
class MarketData(Protocol):
    def bars(
        self, symbol: str, start: datetime, end: datetime, minutes: int = 5
    ) -> list[Bar]:
        """Intraday bars with `end` exclusive. Only fully-closed bars."""
        ...

    def daily_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        ...

    def quote(self, symbol: str, at: datetime) -> Optional[Quote]:
        ...


@runtime_checkable
class NewsFeed(Protocol):
    def news(
        self, symbols: Sequence[str], since: datetime, until: datetime
    ) -> list[NewsItem]:
        ...


@runtime_checkable
class EarningsSource(Protocol):
    def earnings(
        self, symbols: Sequence[str], start: datetime, end: datetime
    ) -> list[EarningsEvent]:
        ...


@runtime_checkable
class OptionsTape(Protocol):
    def option_trades(
        self, underlying: str, start: datetime, end: datetime
    ) -> list[OptionTrade]:
        ...


@runtime_checkable
class Broker(Protocol):
    def account(self) -> AccountInfo: ...

    def positions(self) -> list[Position]: ...

    def open_orders(self) -> list[BrokerOrder]: ...

    def submit(self, order: Order) -> BrokerOrder:
        """Submit with a broker-side protective stop attached.

        Implementations MUST attach the stop atomically with the entry. A stop
        held in this process disappears the moment the process does, and the
        position it was protecting does not.
        """
        ...

    def cancel(self, order_id: str) -> bool: ...

    def close_position(self, symbol: str, quantity: Optional[float] = None) -> Optional[BrokerOrder]:
        ...

    def replace_stop(self, symbol: str, new_stop: float) -> bool: ...


@dataclass
class Providers:
    """The bundle the engine is handed."""
    market: MarketData
    broker: Broker
    news: Optional[NewsFeed] = None
    earnings: Optional[EarningsSource] = None
    options: Optional[OptionsTape] = None

    def describe(self) -> str:
        def name(x) -> str:
            return type(x).__name__ if x is not None else "none"
        return (
            f"market={name(self.market)} broker={name(self.broker)} "
            f"news={name(self.news)} earnings={name(self.earnings)} "
            f"options={name(self.options)}"
        )


__all__ = [
    "AccountInfo", "BrokerOrder", "ProviderError", "RateLimited",
    "MarketData", "NewsFeed", "EarningsSource", "OptionsTape", "Broker", "Providers",
]
