from __future__ import annotations

from typing import Any


class RiskAcceptance:
    def verify(self, exchange_positions: Any, local_positions: Any) -> tuple[bool, str]:
        exchange = _normalize_positions(exchange_positions)
        local = _normalize_positions(local_positions)
        for symbol in sorted(set(exchange) | set(local)):
            if abs(exchange.get(symbol, 0.0) - local.get(symbol, 0.0)) > 1e-9:
                return False, "POSITION_DESYNC"
        return True, "SYNC_OK"

    def verify_reconciliation_report(self, report: dict[str, Any]) -> tuple[bool, str]:
        if not isinstance(report, dict):
            return False, "RECONCILIATION_MISSING"
        if report.get("skipped"):
            return False, "RECONCILIATION_SKIPPED"
        if not report.get("ts_ms"):
            return False, "RECONCILIATION_MISSING"
        if not bool(report.get("ok")):
            issues = report.get("issues") if isinstance(report.get("issues"), list) else []
            code = "POSITION_DESYNC"
            for issue in issues:
                if isinstance(issue, dict) and issue.get("code"):
                    code = str(issue["code"])
                    break
            return False, code
        return True, "SYNC_OK"


def _normalize_positions(positions: Any) -> dict[str, float]:
    if isinstance(positions, dict):
        return {str(symbol).upper(): _safe_float(qty) for symbol, qty in positions.items()}
    if isinstance(positions, list):
        out: dict[str, float] = {}
        for row in positions:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            qty = row.get("positionAmt", row.get("quantity", row.get("qty", 0.0)))
            out[symbol] = out.get(symbol, 0.0) + _safe_float(qty)
        return out
    return {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


risk_acceptance = RiskAcceptance()
