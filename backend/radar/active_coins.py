from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from typing import Any

from backend.config import settings


@dataclass
class ActiveCoin:
    symbol: str
    first_seen: float
    last_seen: float
    reason: str = ""
    status: str = "WATCHING"
    current_score: float = 0.0
    peak_score: float = 0.0
    signal_triggered: bool = False
    subscribed_streams: list[str] = field(default_factory=list)
    removed_at: float = 0.0
    cooldown_until: float = 0.0

    def asdict(self, now: float | None = None) -> dict[str, Any]:
        current = _now(now)
        row = asdict(self)
        row["age_seconds"] = round(max(0.0, current - self.first_seen), 3)
        row["idle_seconds"] = round(max(0.0, current - self.last_seen), 3)
        row["cooldown_remaining_seconds"] = round(max(0.0, self.cooldown_until - current), 3)
        return row


class ActiveCoinRegistry:
    def __init__(
        self,
        *,
        idle_seconds: float | None = None,
        cooldown_seconds: float | None = None,
        max_symbols: int | None = None,
    ) -> None:
        self.idle_seconds = float(idle_seconds if idle_seconds is not None else settings.radar_active_coin_idle_seconds)
        self.cooldown_seconds = float(cooldown_seconds if cooldown_seconds is not None else settings.radar_active_coin_cooldown_seconds)
        self.max_symbols = int(max_symbols if max_symbols is not None else settings.radar_active_coin_max_symbols)
        self._active: dict[str, ActiveCoin] = {}
        self._cooldowns: dict[str, float] = {}
        self._recent_removed: list[dict[str, Any]] = []

    def update_candidates(
        self,
        symbols: list[str] | tuple[str, ...] | set[str],
        *,
        now: float | None = None,
        reason_by_symbol: dict[str, str] | None = None,
        score_by_symbol: dict[str, float] | None = None,
    ) -> list[ActiveCoin]:
        current = _now(now)
        self.expire_idle(now=current)
        reason_by_symbol = reason_by_symbol or {}
        score_by_symbol = score_by_symbol or {}
        updated: list[ActiveCoin] = []
        for raw in symbols:
            symbol = _symbol(raw)
            if not symbol:
                continue
            if self._cooldowns.get(symbol, 0.0) > current and symbol not in self._active:
                continue
            coin = self._active.get(symbol)
            score = float(score_by_symbol.get(symbol, 0.0) or 0.0)
            reason = reason_by_symbol.get(symbol, "")
            if coin is None:
                if len(self._active) >= max(1, self.max_symbols):
                    if not self._replace_lowest_priority_if_better(score, now=current):
                        continue
                coin = ActiveCoin(
                    symbol=symbol,
                    first_seen=current,
                    last_seen=current,
                    reason=reason,
                    current_score=score,
                    peak_score=score,
                )
                self._active[symbol] = coin
            else:
                coin.last_seen = current
                coin.status = "WATCHING" if coin.status in {"EXPIRED", "INVALID"} else coin.status
                if reason:
                    coin.reason = reason
                coin.current_score = score
                coin.peak_score = max(float(coin.peak_score or 0.0), score)
            updated.append(coin)
        return updated

    def _replace_lowest_priority_if_better(self, score: float, *, now: float) -> bool:
        if not self._active:
            return True
        lowest_symbol = min(
            self._active,
            key=lambda symbol: (
                float(self._active[symbol].current_score or 0.0),
                float(self._active[symbol].last_seen or 0.0),
                symbol,
            ),
        )
        lowest = self._active[lowest_symbol]
        if float(score or 0.0) <= float(lowest.current_score or 0.0):
            return False
        removed = self._active.pop(lowest_symbol)
        removed.status = "INVALID"
        removed.reason = "capacity_replace"
        removed.removed_at = now
        removed.cooldown_until = 0.0
        self._recent_removed.insert(0, removed.asdict(now))
        self._recent_removed = self._recent_removed[:20]
        return True

    def expire_idle(self, *, now: float | None = None) -> list[ActiveCoin]:
        current = _now(now)
        expired: list[ActiveCoin] = []
        for symbol, coin in list(self._active.items()):
            if current - float(coin.last_seen or 0.0) <= self.idle_seconds:
                continue
            expired.append(self.remove(symbol, now=current, reason="idle_timeout"))
        return expired

    def remove(self, symbol: str, *, now: float | None = None, reason: str = "removed") -> ActiveCoin:
        current = _now(now)
        key = _symbol(symbol)
        coin = self._active.pop(key, None) or ActiveCoin(symbol=key, first_seen=current, last_seen=current)
        coin.status = "EXPIRED" if reason == "idle_timeout" else "INVALID"
        coin.reason = reason
        coin.removed_at = current
        coin.cooldown_until = current + self.cooldown_seconds
        self._cooldowns[key] = coin.cooldown_until
        self._recent_removed.insert(0, coin.asdict(current))
        self._recent_removed = self._recent_removed[:20]
        return coin

    def mark_signal_triggered(self, symbol: str, *, now: float | None = None) -> None:
        key = _symbol(symbol)
        coin = self._active.get(key)
        if not coin:
            return
        coin.status = "ACTIONABLE"
        coin.signal_triggered = True
        coin.last_seen = _now(now)

    def active_symbols(self) -> list[str]:
        return sorted(
            self._active.keys(),
            key=lambda symbol: (
                -float(self._active[symbol].current_score or 0.0),
                -float(self._active[symbol].last_seen or 0.0),
                symbol,
            ),
        )

    def diagnostics(self, *, now: float | None = None) -> dict[str, Any]:
        current = _now(now)
        active = [self._active[symbol].asdict(current) for symbol in self.active_symbols()]
        cooldowns = {
            symbol: round(until - current, 3)
            for symbol, until in self._cooldowns.items()
            if until > current and symbol not in self._active
        }
        return {
            "active_count": len(active),
            "active_symbols": [row["symbol"] for row in active],
            "active": active[:50],
            "cooldown_count": len(cooldowns),
            "cooldowns": cooldowns,
            "recent_removed": self._recent_removed[:10],
            "policy": {
                "idle_seconds": self.idle_seconds,
                "cooldown_seconds": self.cooldown_seconds,
                "max_symbols": self.max_symbols,
                "ordering": "current_score_desc",
                "capacity": "replace_lowest_score",
            },
        }

    def reset(self) -> None:
        self._active.clear()
        self._cooldowns.clear()
        self._recent_removed.clear()


def _now(value: float | None = None) -> float:
    return float(time.monotonic() if value is None else value)


def _symbol(value: Any) -> str:
    return str(value or "").upper().strip()


active_coin_registry = ActiveCoinRegistry()
