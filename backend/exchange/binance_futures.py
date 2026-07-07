from __future__ import annotations

import hashlib
import hmac
import asyncio
import logging
import re
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from backend.config import settings


class BinanceAPIError(RuntimeError):
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"BINANCE_API_ERROR status={status_code} payload={redact_sensitive_url(payload)}")


class BinanceFuturesClient:
    """Minimal Binance USD-M Futures REST client.

    Signed endpoints use HMAC SHA256 over the exact query string, then send
    X-MBX-APIKEY. This client is intentionally explicit; no third-party Binance
    SDK is required.
    """

    def __init__(self) -> None:
        self.base_url = "https://testnet.binancefuture.com" if settings.binance_testnet else "https://fapi.binance.com"
        self.api_key = settings.binance_api_key
        self.api_secret = settings.binance_api_secret
        self.recv_window = settings.binance_recv_window
        self._exchange_info_cache: dict[str, Any] | None = None
        self._symbol_filters: dict[str, dict[str, Any]] = {}
        self._public_client: httpx.AsyncClient | None = None
        self._public_client_loop_id: int | None = None
        self._signed_client: httpx.AsyncClient | None = None
        self._signed_client_loop_id: int | None = None
        self._server_time_offset_ms = 0
        self._time_synced_at = 0.0

    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def reload_from_settings(self) -> None:
        self.base_url = "https://testnet.binancefuture.com" if settings.binance_testnet else "https://fapi.binance.com"
        self.api_key = settings.binance_api_key
        self.api_secret = settings.binance_api_secret
        self.recv_window = settings.binance_recv_window
        self._exchange_info_cache = None
        self._symbol_filters = {}
        self._public_client = None
        self._public_client_loop_id = None
        self._signed_client = None
        self._signed_client_loop_id = None

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RuntimeError("BINANCE_API_KEY_MISSING")
        return {"X-MBX-APIKEY": self.api_key}

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_secret:
            raise RuntimeError("BINANCE_API_SECRET_MISSING")
        signed = {k: v for k, v in (params or {}).items() if v is not None}
        signed.setdefault("recvWindow", self.recv_window)
        signed["timestamp"] = int(time.time() * 1000) + self._server_time_offset_ms
        query_string = urlencode(signed, doseq=True)
        signature = hmac.new(self.api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
        signed["signature"] = signature
        return signed

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        if signed:
            await self.sync_server_time()
        request_params = self._sign(params or {}) if signed else (params or {})
        headers = self._headers() if signed else None
        client = await self._client(signed=signed)
        url = self.base_url + path
        try:
            response = await client.request(method, url, params=request_params, headers=headers)
        except httpx.HTTPError as exc:
            raise RuntimeError(_http_error_detail(exc, url)) from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except Exception:
                payload = response.text
            if signed and self._is_timestamp_error(payload):
                await self.sync_server_time(force=True)
                request_params = self._sign(params or {})
                try:
                    response = await client.request(method, url, params=request_params, headers=headers)
                except httpx.HTTPError as exc:
                    raise RuntimeError(_http_error_detail(exc, url)) from exc
                if response.status_code < 400:
                    if not response.text.strip():
                        return None
                    try:
                        return response.json()
                    except ValueError:
                        return response.text.strip() or None
                try:
                    payload = response.json()
                except Exception:
                    payload = response.text
            raise BinanceAPIError(response.status_code, payload)
        if not response.text.strip():
            return None
        try:
            return response.json()
        except ValueError:
            return response.text.strip() or None

    async def _client(self, *, signed: bool = False) -> httpx.AsyncClient:
        loop_id = id(asyncio.get_running_loop())
        if signed:
            if self._signed_client is not None and self._signed_client_loop_id == loop_id:
                return self._signed_client
            if self._signed_client is not None:
                try:
                    await self._signed_client.aclose()
                except Exception:
                    pass
            self._signed_client = httpx.AsyncClient(
                timeout=binance_signed_http_timeout(),
                limits=binance_signed_http_limits(),
                trust_env=False,
            )
            self._signed_client_loop_id = loop_id
            return self._signed_client

        if self._public_client is not None and self._public_client_loop_id == loop_id:
            return self._public_client
        if self._public_client is not None:
            try:
                await self._public_client.aclose()
            except Exception:
                pass
        self._public_client = httpx.AsyncClient(
            timeout=binance_http_timeout(),
            limits=binance_http_limits(),
            trust_env=False,
        )
        self._public_client_loop_id = loop_id
        return self._public_client

    async def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params, signed=False)

    async def sync_server_time(self, force: bool = False) -> int:
        now = time.monotonic()
        if not force and now - self._time_synced_at < 60:
            return self._server_time_offset_ms
        client = await self._client(signed=True)
        before = int(time.time() * 1000)
        response = await client.get(self.base_url + "/fapi/v1/time")
        response.raise_for_status()
        after = int(time.time() * 1000)
        server_time = int((response.json() or {}).get("serverTime", after))
        local_midpoint = (before + after) // 2
        self._server_time_offset_ms = server_time - local_midpoint
        self._time_synced_at = now
        return self._server_time_offset_ms

    def _is_timestamp_error(self, payload: Any) -> bool:
        return isinstance(payload, dict) and payload.get("code") == -1021

    async def signed_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params, signed=True)

    async def signed_post(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("POST", path, params, signed=True)

    async def signed_delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("DELETE", path, params, signed=True)

    # ---------- public market endpoints ----------
    async def exchange_info(self) -> Any:
        if self._exchange_info_cache is None:
            self._exchange_info_cache = await self.public_get("/fapi/v1/exchangeInfo")
            self._index_symbol_filters(self._exchange_info_cache)
        return self._exchange_info_cache

    async def ticker_prices(self) -> Any:
        return await self.public_get("/fapi/v2/ticker/price")

    async def ticker_24hr(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else {}
        return await self.public_get("/fapi/v1/ticker/24hr", params)

    async def premium_index(self) -> Any:
        return await self.public_get("/fapi/v1/premiumIndex")

    async def klines(self, symbol: str, interval: str = "5m", limit: int = 30) -> Any:
        return await self.public_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    async def depth(self, symbol: str, limit: int = 50) -> Any:
        return await self.public_get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    async def open_interest(self, symbol: str) -> Any:
        return await self.public_get("/fapi/v1/openInterest", {"symbol": symbol})

    async def open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 30) -> Any:
        return await self.public_get("/futures/data/openInterestHist", {"symbol": symbol, "period": period, "limit": limit})

    async def taker_long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 30) -> Any:
        return await self.public_get("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": period, "limit": limit})

    # ---------- signed account endpoints ----------
    async def account_balance(self) -> list[dict[str, Any]]:
        return await self.signed_get("/fapi/v2/balance")

    async def account_info(self) -> dict[str, Any]:
        return await self.signed_get("/fapi/v2/account")

    async def position_side_dual(self) -> bool:
        data = await self.signed_get("/fapi/v1/positionSide/dual")
        value = (data or {}).get("dualSidePosition") if isinstance(data, dict) else data
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() == "true"

    async def position_risk(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else {}
        return await self.signed_get("/fapi/v2/positionRisk", params)

    async def open_orders(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else {}
        return await self.signed_get("/fapi/v1/openOrders", params)

    async def income_history(self, symbol: str | None = None, income_type: str | None = None, limit: int = 100) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if income_type:
            params["incomeType"] = income_type
        return await self.signed_get("/fapi/v1/income", params)

    async def leverage_bracket(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else {}
        return await self.signed_get("/fapi/v1/leverageBracket", params)

    # ---------- signed trading endpoints ----------
    async def change_leverage(self, symbol: str, leverage: int) -> Any:
        return await self.signed_post("/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})

    async def change_margin_type(self, symbol: str, margin_type: Literal["ISOLATED", "CROSSED"]) -> Any:
        try:
            return await self.signed_post("/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
        except BinanceAPIError as exc:
            # Binance returns -4046 if margin type is already set. Treat as idempotent.
            if isinstance(exc.payload, dict) and exc.payload.get("code") == -4046:
                return {"symbol": symbol, "marginType": margin_type, "already_set": True}
            raise

    async def new_order(self, **params: Any) -> Any:
        return await self.signed_post("/fapi/v1/order", params)

    async def test_order(self, **params: Any) -> Any:
        return await self.signed_post("/fapi/v1/order/test", params)

    async def cancel_order(self, symbol: str, order_id: int | None = None, orig_client_order_id: str | None = None) -> Any:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        return await self.signed_delete("/fapi/v1/order", params)

    async def cancel_all_open_orders(self, symbol: str) -> Any:
        return await self.signed_delete("/fapi/v1/allOpenOrders", {"symbol": symbol})

    async def market_open(self, symbol: str, side: Literal["BUY", "SELL"], quantity: float, position_side: str | None = None, client_id: str | None = None) -> Any:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": self.format_market_quantity(symbol, quantity),
            "newOrderRespType": "RESULT",
        }
        if position_side:
            params["positionSide"] = position_side
        if client_id:
            params["newClientOrderId"] = client_id[:36]
        return await self.new_order(**params)

    async def market_close(self, symbol: str, side: Literal["BUY", "SELL"], quantity: float, position_side: str | None = None, client_id: str | None = None) -> Any:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": self.format_market_quantity(symbol, quantity),
            "reduceOnly": "true" if not position_side else None,
            "newOrderRespType": "RESULT",
        }
        if position_side:
            params["positionSide"] = position_side
        if client_id:
            params["newClientOrderId"] = client_id[:36]
        return await self.new_order(**params)

    async def stop_market(self, symbol: str, close_side: Literal["BUY", "SELL"], stop_price: float, position_side: str | None = None, client_id: str | None = None) -> Any:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": close_side,
            "type": "STOP_MARKET",
            "stopPrice": self.format_price(symbol, stop_price),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
        }
        if position_side:
            params["positionSide"] = position_side
        if client_id:
            params["newClientOrderId"] = client_id[:36]
        return await self.new_order(**params)

    async def take_profit_market(self, symbol: str, close_side: Literal["BUY", "SELL"], stop_price: float, position_side: str | None = None, client_id: str | None = None) -> Any:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": close_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": self.format_price(symbol, stop_price),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
        }
        if position_side:
            params["positionSide"] = position_side
        if client_id:
            params["newClientOrderId"] = client_id[:36]
        return await self.new_order(**params)

    # ---------- precision helpers ----------
    def _index_symbol_filters(self, info: dict[str, Any]) -> None:
        self._symbol_filters = {}
        for sym in info.get("symbols", []):
            filters = {f.get("filterType"): f for f in sym.get("filters", [])}
            self._symbol_filters[sym.get("symbol")] = {"raw": sym, "filters": filters}

    async def ensure_symbol_filters(self, symbol: str) -> None:
        if symbol not in self._symbol_filters:
            await self.exchange_info()

    def _floor_step(self, value: float, step: str) -> str:
        d_value = Decimal(str(value))
        d_step = Decimal(str(step))
        if d_step == 0:
            return str(value)
        floored = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
        return format(floored.normalize(), "f")

    def format_quantity(self, symbol: str, quantity: float) -> str:
        info = self._symbol_filters.get(symbol)
        if not info:
            # Safe fallback. exchangeInfo is usually loaded before trading.
            return f"{quantity:.6f}".rstrip("0").rstrip(".")
        lot = info["filters"].get("LOT_SIZE") or {}
        return self._floor_step(quantity, lot.get("stepSize", "0.001"))

    def format_market_quantity(self, symbol: str, quantity: float) -> str:
        info = self._symbol_filters.get(symbol)
        if not info:
            return f"{quantity:.6f}".rstrip("0").rstrip(".")
        filters = info.get("filters") or {}
        lot = filters.get("LOT_SIZE") or {}
        market_lot = filters.get("MARKET_LOT_SIZE") or {}
        step_size = market_lot.get("stepSize") or lot.get("stepSize") or "0.001"
        return self._floor_step(quantity, step_size)

    def format_price(self, symbol: str, price: float) -> str:
        info = self._symbol_filters.get(symbol)
        if not info:
            return f"{price:.8f}".rstrip("0").rstrip(".")
        price_filter = info["filters"].get("PRICE_FILTER") or {}
        return self._floor_step(price, price_filter.get("tickSize", "0.00000001"))

    def market_order_constraints(self, symbol: str, quantity: float, reference_price: float) -> dict[str, Any]:
        info = self._symbol_filters.get(symbol)
        if not info:
            return {"ok": False, "reasons": ["symbol_filters_missing"], "symbol": symbol}

        filters = info.get("filters") or {}
        lot = filters.get("LOT_SIZE") or {}
        market_lot = filters.get("MARKET_LOT_SIZE") or {}
        step_size = market_lot.get("stepSize") or lot.get("stepSize") or "0.001"
        formatted_qty = self.format_market_quantity(symbol, quantity)
        qty = _safe_decimal(formatted_qty)
        price = _safe_decimal(reference_price)
        reasons: list[str] = []

        min_qty = max(_safe_decimal(lot.get("minQty")), _safe_decimal(market_lot.get("minQty")))
        max_qty_values = [
            value
            for value in (_safe_decimal(lot.get("maxQty")), _safe_decimal(market_lot.get("maxQty")))
            if value > 0
        ]
        max_qty = min(max_qty_values) if max_qty_values else Decimal("0")
        min_notional = _safe_decimal((filters.get("MIN_NOTIONAL") or {}).get("notional"))
        if min_notional <= 0:
            min_notional = _safe_decimal((filters.get("NOTIONAL") or {}).get("minNotional"))
        notional = qty * price

        if qty <= 0:
            reasons.append("quantity_floors_to_zero")
        if min_qty > 0 and qty < min_qty:
            reasons.append("quantity_below_min_qty")
        if max_qty > 0 and qty > max_qty:
            reasons.append("quantity_above_max_qty")
        if price <= 0:
            reasons.append("reference_price_invalid")
        if min_notional > 0 and notional < min_notional:
            reasons.append("notional_below_min_notional")

        return {
            "ok": not reasons,
            "reasons": reasons,
            "symbol": symbol,
            "formatted_quantity": formatted_qty,
            "quantity": float(qty),
            "reference_price": float(price),
            "notional": float(notional),
            "min_qty": float(min_qty),
            "max_qty": float(max_qty),
            "min_notional": float(min_notional),
            "step_size": str(step_size),
        }


def _http_error_detail(exc: httpx.HTTPError, url: str) -> str:
    message = redact_sensitive_url(str(exc)).strip()
    if message:
        return f"{type(exc).__name__}:{message}"
    return f"{type(exc).__name__}:{redact_sensitive_url(url)}"


binance_futures = BinanceFuturesClient()


def redact_sensitive_url(value: Any) -> str:
    text = str(value)
    for key in ("signature", "apiSecret", "api_secret", "secret", "X-MBX-APIKEY"):
        text = re.sub(rf"({re.escape(key)}=)[^&\s\"']+", rf"\1<redacted>", text, flags=re.IGNORECASE)
    return text


class RedactingLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_url(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {key: _redact_log_arg(value) for key, value in record.args.items()}
            else:
                record.args = tuple(_redact_log_arg(value) for value in record.args)
        return True


def _redact_log_arg(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_url(value)
    return value


def _install_redacting_logging_filter() -> None:
    redactor = RedactingLogFilter()
    for logger_name in ("httpx", "httpcore", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(item, RedactingLogFilter) for item in logger.filters):
            logger.addFilter(redactor)
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


_install_redacting_logging_filter()


def binance_http_timeout() -> httpx.Timeout:
    base = max(1.0, float(settings.binance_http_timeout or 5.0))
    market_read = max(10.0, base)
    return httpx.Timeout(
        connect=max(3.0, min(base, 10.0)),
        read=market_read,
        write=base,
        pool=max(market_read, min(30.0, market_read * 2.5)),
    )


def binance_http_limits() -> httpx.Limits:
    request_fanout = 3
    if settings.binance_use_open_interest_hist:
        request_fanout += 1
    if settings.binance_use_taker_ratio_endpoint:
        request_fanout += 1
    expected_parallel = max(1, int(settings.binance_factor_concurrency or 1)) * request_fanout + 4
    max_connections = min(200, max(50, expected_parallel * 3, int(settings.binance_symbol_limit or 0)))
    keepalive = max(20, min(max_connections, expected_parallel * 2))
    return httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=keepalive,
        keepalive_expiry=20.0,
    )


def binance_signed_http_timeout() -> httpx.Timeout:
    base = max(1.0, float(settings.binance_http_timeout or 5.0))
    return httpx.Timeout(
        connect=max(2.0, min(base, 8.0)),
        read=max(2.0, min(base, 8.0)),
        write=max(2.0, min(base, 8.0)),
        pool=max(1.0, min(3.0, base / 2.0)),
    )


def binance_signed_http_limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=20,
        max_keepalive_connections=10,
        keepalive_expiry=20.0,
    )


def _safe_decimal(value: Any) -> Decimal:
    try:
        if value is None or value == "":
            return Decimal("0")
        return Decimal(str(value))
    except Exception:
        return Decimal("0")
