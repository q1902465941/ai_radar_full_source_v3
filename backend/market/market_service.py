from __future__ import annotations
from dataclasses import asdict, dataclass
import time
from typing import Any

from backend.config import settings
from backend.market.mock_market import market as mock_market
from backend.market.binance_factor_source import binance_factor_source
from backend.market.binance_rest import binance_rest
from backend.models import MarketSnapshot, now_ms


@dataclass
class PriceQuote:
    symbol: str
    price: float
    source: str
    ts_ms: int
    age_seconds: float
    stale: bool
    error: str = ""
    bid: float = 0.0
    ask: float = 0.0
    last_price: float = 0.0
    mark_price: float = 0.0

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


class MarketService:
    def __init__(self):
        self.last_snapshots: dict[str, MarketSnapshot] = {}
        self.last_prices: dict[str, float] = {}
        self.last_price_ts: dict[str, float] = {}
        self.last_price_wall_ts: dict[str, int] = {}
        self.last_price_source: dict[str, str] = {}
        self.last_price_error: dict[str, str] = {}
        self.last_price_bid: dict[str, float] = {}
        self.last_price_ask: dict[str, float] = {}

    async def get_snapshots(self, force_refresh: bool = False) -> list[MarketSnapshot]:
        if settings.market_data_mode.lower() != "binance":
            snaps = await mock_market.get_snapshots()
            self.last_snapshots = {s.symbol:s for s in snaps}
            for snap in snaps:
                self._remember_price(snap.symbol, snap.price, "mock_snapshot", snap.ts_ms)
            return snaps
        snaps = await binance_factor_source.get_snapshots(force_refresh=force_refresh)
        self.last_snapshots = {s.symbol:s for s in snaps}
        source = f"snapshot:{binance_factor_source.last_refresh_source or 'unknown'}"
        if binance_factor_source.last_refresh_degraded:
            source = f"{source}:degraded"
        for snap in snaps:
            self._remember_price(snap.symbol, snap.price, source, snap.ts_ms)
        return snaps

    def price(self, symbol: str) -> float:
        s=self.last_snapshots.get(symbol)
        if s and s.price > 0:
            return s.price
        return self.last_prices.get(symbol, 0.0)

    async def price_for(self, symbol: str) -> float:
        return (await self.price_quote(symbol)).price

    async def price_quote(self, symbol: str, side: str | None = None) -> PriceQuote:
        symbol = str(symbol or "").upper()
        if not symbol:
            return self._quote(symbol, 0.0, "invalid_symbol", 0, "empty_symbol")

        if settings.market_data_mode.lower() == "binance":
            quote = await self._binance_quote(symbol, side)
            if quote.price > 0:
                return quote

        return self.cached_price_quote(symbol)

    def cached_price_quote(self, symbol: str) -> PriceQuote:
        symbol = str(symbol or "").upper()
        snapshot = self.last_snapshots.get(symbol)
        if snapshot and snapshot.price > 0:
            return self._quote(
                symbol,
                snapshot.price,
                self.last_price_source.get(symbol) or "snapshot_cache",
                snapshot.ts_ms,
                self.last_price_error.get(symbol, ""),
                self.last_price_bid.get(symbol, 0.0),
                self.last_price_ask.get(symbol, 0.0),
            )
        price = self.last_prices.get(symbol, 0.0)
        ts_ms = self.last_price_wall_ts.get(symbol, 0)
        return self._quote(
            symbol,
            price,
            self.last_price_source.get(symbol) or "cache",
            ts_ms,
            self.last_price_error.get(symbol, ""),
            self.last_price_bid.get(symbol, 0.0),
            self.last_price_ask.get(symbol, 0.0),
        )

    async def _binance_quote(self, symbol: str, side: str | None) -> PriceQuote:
        errors: list[str] = []
        try:
            row = await binance_rest.book_ticker(symbol)
            received_ts = now_ms()
            bid = _first_positive(_field(row, "bidPrice"))
            ask = _first_positive(_field(row, "askPrice"))
            if bid > 0 and ask > 0:
                if side == "LONG":
                    price = bid
                    source = "book_ticker_bid_close_long"
                elif side == "SHORT":
                    price = ask
                    source = "book_ticker_ask_close_short"
                else:
                    price = (bid + ask) / 2.0
                    source = "book_ticker_mid"
                quote = self._quote(symbol, price, source, received_ts, "", bid, ask)
                self._remember_price(symbol, price, source, quote.ts_ms, bid, ask)
                return quote
        except Exception as exc:
            errors.append(f"book_ticker:{type(exc).__name__}")

        for source, fetch, parser in (
            ("ticker_price", lambda: binance_rest.ticker_price(symbol), _ticker_price),
            ("premium_mark_price", lambda: binance_rest.public_get("/fapi/v1/premiumIndex", {"symbol": symbol}), _mark_price),
            ("ticker_24hr_last_price", lambda: binance_rest.ticker_24hr(symbol), _ticker_price),
        ):
            try:
                row = await fetch()
                received_ts = now_ms()
                price = parser(row)
                if price > 0:
                    quote = self._quote(symbol, price, source, received_ts, ";".join(errors))
                    self._remember_price(symbol, price, source, quote.ts_ms)
                    return quote
            except Exception as exc:
                errors.append(f"{source}:{type(exc).__name__}")

        self.last_price_error[symbol] = ";".join(errors[:8])
        return self._quote(symbol, 0.0, "binance_unavailable", 0, self.last_price_error[symbol])

    def _remember_price(
        self,
        symbol: str,
        price: float,
        source: str,
        ts_ms: int | None = None,
        bid: float = 0.0,
        ask: float = 0.0,
    ) -> None:
        if price <= 0:
            return
        symbol = str(symbol or "").upper()
        self.last_prices[symbol] = float(price)
        self.last_price_ts[symbol] = time.monotonic()
        self.last_price_wall_ts[symbol] = int(ts_ms or now_ms())
        self.last_price_source[symbol] = source
        self.last_price_error[symbol] = ""
        if bid > 0:
            self.last_price_bid[symbol] = bid
        if ask > 0:
            self.last_price_ask[symbol] = ask

    def _quote(
        self,
        symbol: str,
        price: float,
        source: str,
        ts_ms: int | None,
        error: str = "",
        bid: float = 0.0,
        ask: float = 0.0,
    ) -> PriceQuote:
        ts = int(ts_ms or 0)
        age = max(0.0, (now_ms() - ts) / 1000.0) if ts > 0 else 999999.0
        stale_after = max(10.0, float(settings.position_manage_interval_seconds or 2) * 3.0, float(settings.binance_ws_stale_seconds or 10))
        stale = price <= 0 or ts <= 0 or age > stale_after
        return PriceQuote(
            symbol=symbol,
            price=float(price or 0.0),
            source=source,
            ts_ms=ts,
            age_seconds=round(age, 3),
            stale=stale,
            error=error,
            bid=float(bid or 0.0),
            ask=float(ask or 0.0),
            last_price=float(price or 0.0),
            mark_price=float(price or 0.0) if "mark" in source else 0.0,
        )


def _ticker_price(row: Any) -> float:
    if isinstance(row, list):
        row = row[0] if row else {}
    if not isinstance(row, dict):
        return 0.0
    # Do not use weightedAvgPrice here; it is a 24h statistic, not a current mark.
    for key in ("lastPrice", "price", "markPrice"):
        try:
            value = float(row.get(key) or 0.0)
        except Exception:
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _mark_price(row: Any) -> float:
    if isinstance(row, list):
        row = row[0] if row else {}
    if not isinstance(row, dict):
        return 0.0
    return _first_positive(row.get("markPrice"), row.get("indexPrice"))


def _field(row: Any, key: str) -> Any:
    if isinstance(row, list):
        row = row[0] if row else {}
    return row.get(key) if isinstance(row, dict) else None


def _event_ts(row: Any) -> int:
    if isinstance(row, list):
        row = row[0] if row else {}
    if not isinstance(row, dict):
        return now_ms()
    for key in ("time", "E", "T", "closeTime"):
        try:
            value = int(float(row.get(key) or 0))
        except Exception:
            value = 0
        if value > 0:
            return value
    return now_ms()


def _first_positive(*values: Any) -> float:
    for value in values:
        try:
            parsed = float(value or 0.0)
        except Exception:
            parsed = 0.0
        if parsed > 0:
            return parsed
    return 0.0

market_service = MarketService()
