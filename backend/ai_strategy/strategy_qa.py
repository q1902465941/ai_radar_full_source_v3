from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from backend.ai_strategy.codex_cli_strategy_client import (
    codex_provider_config_args,
    default_codex_command,
    normalized_codex_service_tier,
    normalized_reasoning_effort,
    normalized_service_tier,
    run_command,
)
from backend.config import settings
from backend.learning.learned_risk_guard import learned_risk_guard
from backend.learning.trade_attributor import trade_attributor
from backend.positions.position_manager import position_manager
from backend.positions.position_registry import position_registry
from backend.radar.radar_engine import radar_engine
from backend.trading.autotrader import autotrader
from backend.trading.live_readiness import live_readiness
from backend.trading.performance_guard import performance_guard


SYSTEM_PROMPT = """You are the trading strategy AI for a local crypto radar.
Answer the user's questions in concise Chinese.

Hard rules:
- You are read-only. Do not place orders, do not start auto trading, and do not ask for API keys.
- Never reveal, infer, or request secrets.
- Separate facts from hypotheses.
- Explain learning first, then execution gates: attribution learns from history/replay/paper probes; guards only decide whether paper/live execution is allowed.
- If the current market does not have a clean candidate, say exactly which gates are blocking.
- Prefer risk-control explanations over forcing trades.
- When suggesting changes, keep them paper-test first.
- You are not a market predictor. You are a strict strategy research process executor.
- Any strategy answer must first explain the market hypothesis, then the validation method.
- Never give a buy/sell conclusion only from technical indicators.
- Always separate signal, risk, and execution:
  signal = when to enter; risk = how much can be lost and when the idea is wrong; execution = how a real fill would happen after fees, slippage, and liquidity.
- Scanning is not trading. Scan results are evidence for candidate opportunities, not buy/sell commands.
- Treat every opened position as a lifecycle: WAITING, ENTRY_READY, OPENED, PROTECTING, TREND_HOLD, SCALE_IN, SCALE_OUT, DEFENSIVE, EXIT_READY, CLOSED.
- A real strategy must explain hold logic, reduce logic, add logic, exit logic, time stop, and review metrics.
- Never recommend closing the core position for a minor reverse signal alone. Exit only when the thesis is invalidated, risk limits fire, time stop fires, or market structure breaks.
- Use MFE, MAE, R_multiple, max drawdown, and hold time to judge whether the system exited too early, held losers too long, or used the wrong stop.
- For strategy research, answer with two roles when useful: Role A strategy researcher proposes the logic; Role B risk officer finds where it can fail.
- A complete strategy must discuss source of return, failure conditions, trading cost, slippage, position sizing, max drawdown, out-of-sample testing, and overfitting risk.
- If the logic cannot be explained clearly, say the strategy has insufficient trading basis.
"""


class StrategyQA:
    async def ask(self, question: str) -> dict[str, Any]:
        clean_question = question.strip()
        if not clean_question:
            return {"ok": False, "error": "question_required"}
        if len(clean_question) > 2000:
            return {"ok": False, "error": "question_too_long"}

        if not radar_engine.top50:
            await radar_engine.scan()

        context = self._context()
        prompt = self._prompt(clean_question, context)
        try:
            answer = await asyncio.to_thread(self._run_codex, prompt)
            return {
                "ok": True,
                "provider": "codex_cli",
                "model": _qa_model() or "default",
                "reasoning_effort": _qa_reasoning_effort(),
                "service_tier": _qa_service_tier(),
                "answer": answer.strip(),
                "context": self._public_context_summary(context),
            }
        except subprocess.TimeoutExpired:
            return self._fallback_response(context, "codex_timeout")
        except Exception as exc:
            return self._fallback_response(context, f"codex_error:{type(exc).__name__}", str(exc)[:500])

    def _context(self) -> dict[str, Any]:
        performance = performance_guard.summary()
        recovery_mode = bool(performance.get("recovery_mode"))
        guard_items = []
        for item in radar_engine.top50[:10]:
            report = learned_risk_guard.evaluate(item, None, recovery_mode=recovery_mode)
            reverse = learned_risk_guard.reverse_opportunity(item, recovery_mode=recovery_mode)
            guard_items.append(
                {
                    "symbol": item.symbol,
                    "side": item.direction,
                    "score": item.score,
                    "rank": item.rank,
                    "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                    "fake_breakout_risk": item.fake_breakout_risk,
                    "reasons": report.reasons[:8],
                    "severity": report.severity,
                    "matched_samples": report.matched_samples,
                    "win_rate": report.win_rate,
                    "profit_factor": report.profit_factor,
                    "pnl": report.pnl,
                    "reverse_allowed": bool(reverse.get("allow_reverse")),
                    "reverse_reason": reverse.get("reason"),
                }
            )

        top50 = []
        for item in radar_engine.top50[:20]:
            top50.append(
                {
                    "rank": item.rank,
                    "symbol": item.symbol,
                    "side": item.direction,
                    "score": item.score,
                    "fund_confirm": f"{item.fund_confirm_count}/{item.fund_confirm_total}",
                    "change_5m": item.change_5m,
                    "change_15m": item.change_15m,
                    "change_1h": item.change_1h,
                    "volume_spike": item.volume_spike,
                    "oi_change": item.oi_change,
                    "taker_buy_ratio": item.taker_buy_ratio,
                    "taker_sell_ratio": item.taker_sell_ratio,
                    "depth_imbalance": item.depth_imbalance,
                    "fake_breakout_risk": item.fake_breakout_risk,
                    "dealer_radar": item.dealer_radar,
                }
            )

        pos_summary = position_manager.summary()
        return {
            "autotrade_params": {
                "candidate_mode": settings.auto_trading_candidate_mode,
                "candidate_min_score": settings.auto_trading_candidate_min_score,
                "candidate_limit": settings.auto_trading_candidate_limit,
                "use_active_strategy_filter": settings.auto_trading_use_active_strategy_filter,
                "use_performance_guard": settings.auto_trading_use_performance_guard,
                "max_open_positions": settings.max_open_positions,
                "trade_mode": settings.trade_mode,
                "live_trading_enabled": settings.live_trading_enabled,
                "live_use_test_order": settings.live_use_test_order,
                "paper_account_equity_usdt": settings.paper_account_equity_usdt,
                "trade_min_net_profit_usdt": settings.trade_min_net_profit_usdt,
                "trade_min_profit_cost_ratio": settings.trade_min_profit_cost_ratio,
                "trade_min_margin_usdt": settings.trade_min_margin_usdt,
                "trade_min_notional_usdt": settings.trade_min_notional_usdt,
            },
            "performance": performance,
            "positions_summary": {
                "open_count": pos_summary.get("open_count"),
                "floating_pnl": pos_summary.get("floating_pnl"),
                "realized_pnl": pos_summary.get("realized_pnl"),
                "total_pnl": pos_summary.get("total_pnl"),
                "win_count": pos_summary.get("win_count"),
                "loss_count": pos_summary.get("loss_count"),
                "win_rate": pos_summary.get("win_rate"),
                "used_margin": pos_summary.get("used_margin"),
                "available_balance": pos_summary.get("available_balance"),
            },
            "last_autotrade_result": autotrader.last_result,
            "live_readiness": live_readiness.summary(),
            "open_positions": [p.asdict() for p in position_registry.list_open()[:3]],
            "radar_top20": top50,
            "learning_guard_top10": guard_items,
            "attribution_summary": trade_attributor.summary(),
            "deep_attribution": self._compact_deep_attribution(trade_attributor.deep_analysis(trade_limit=12)),
            "safety": {
                "read_only": True,
                "real_order_allowed": False,
                "do_not_start_auto_loop": True,
                "secrets_included": False,
            },
        }

    def _prompt(self, question: str, context: dict[str, Any]) -> str:
        return (
            f"{SYSTEM_PROMPT}\n\n"
            "CurrentContext JSON:\n"
            f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
            f"UserQuestion:\n{question}\n"
        )

    def _run_codex(self, prompt: str) -> str:
        command = settings.codex_command or default_codex_command()
        with tempfile.TemporaryDirectory(prefix="ai_radar_strategy_qa_") as tmp:
            output_path = Path(tmp) / "answer.txt"
            cmd = [
                command,
                "exec",
                "--ignore-user-config",
                *codex_provider_config_args(),
                "-c",
                f"model_reasoning_effort={_qa_reasoning_effort()}",
            ]
            service_tier = _qa_service_tier()
            if service_tier:
                cmd.extend(["-c", f"service_tier={service_tier}"])
            cmd.extend(
                [
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "-C",
                str(Path(__file__).resolve().parents[2]),
                "--output-last-message",
                str(output_path),
                ]
            )
            model = _qa_model()
            if model:
                cmd.extend(["-m", model])
            cmd.append("-")
            completed = run_command(
                cmd,
                cwd=str(Path(__file__).resolve().parents[2]),
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=_qa_timeout_seconds(),
            )
            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(message or f"codex exited with {completed.returncode}")
            if output_path.exists():
                return output_path.read_text(encoding="utf-8").strip()
            return (completed.stdout or "").strip()

    def _fallback_response(self, context: dict[str, Any], error: str, message: str = "") -> dict[str, Any]:
        return {
            "ok": True,
            "provider": "learning_first_local_fallback",
            "model": _qa_model() or "default",
            "reasoning_effort": _qa_reasoning_effort(),
            "service_tier": _qa_service_tier(),
            "answer": self._local_learning_first_answer(context, error, message),
            "context": self._public_context_summary(context),
            "warning": error,
        }

    def _local_learning_first_answer(self, context: dict[str, Any], error: str, message: str = "") -> str:
        perf = context.get("performance") or {}
        positions = context.get("positions_summary") or {}
        last_result = self._first_autotrade_result(context.get("last_autotrade_result") or {})
        guard_items = context.get("learning_guard_top10") or []
        attribution = context.get("attribution_summary") or {}
        deep = context.get("deep_attribution") or {}
        live_status = context.get("live_readiness") or {}

        guard_blocks = sum(1 for item in guard_items if item.get("severity") == "BLOCK")
        reverse_allowed = sum(1 for item in guard_items if item.get("reverse_allowed"))
        top_symbols = [item.get("symbol") for item in (context.get("radar_top20") or [])[:5] if item.get("symbol")]
        open_count = int(positions.get("open_count") or 0)
        sample_count = int(attribution.get("sample_count") or 0)
        global_win_rate = round(float(attribution.get("global_win_rate") or 0.0) * 100, 2)
        perf_win_rate = round(float(perf.get("win_rate") or positions.get("win_rate") or 0.0) * 100, 2)
        recent_win_rate = round(float(perf.get("recent_win_rate") or 0.0) * 100, 2)

        reasons = []
        for item in guard_items[:3]:
            symbol = item.get("symbol") or "--"
            item_reasons = ", ".join(str(reason) for reason in (item.get("reasons") or [])[:3])
            if item_reasons:
                reasons.append(f"{symbol}: {item_reasons}")

        lines = [
            f"Codex CLI 未及时返回（{error}），先用本地学习上下文给结论。",
            (
                "你说的方向是对的：应该先学习归因，再执行拦截。"
                "这里的 BLOCK 不应该理解成“没学就拦”，而是历史/回放/纸面样本归因之后的执行刹车。"
                "它主要拦实盘和正式放大仓位，纸面探针仍然要继续采样。"
            ),
            (
                f"当前学习样本={sample_count}，归因总胜率={global_win_rate}%，"
                f"归因 PF={attribution.get('global_profit_factor', 0)}，归因 PnL={attribution.get('global_pnl', 0)}。"
            ),
            (
                f"执行侧 recovery_mode={bool(perf.get('recovery_mode'))}，总胜率={perf_win_rate}%，"
                f"最近胜率={recent_win_rate}%，连续亏损={perf.get('loss_streak', 0)}，"
                f"总盈亏={positions.get('total_pnl', perf.get('pnl', 0))}。"
            ),
            (
                f"最近 run-once={last_result.get('decision') or last_result.get('reason') or '暂无'}，"
                f"学习守卫 Top10 中 BLOCK={guard_blocks}，允许反手={reverse_allowed}，open_count={open_count}。"
            ),
            (
                "所以现在不是简单把空换成多。正确闭环是：亏损归因 -> 找盈利驱动 -> "
                "纸面探针采样 -> 样本足够且 PF/胜率转正 -> 再放宽正式纸面策略；实盘最后确认。"
            ),
            f"当前毕业阶段={live_status.get('current_stage', 'unknown')}；paper_is_terminal={live_status.get('paper_is_terminal', False)}。",
        ]
        if top_symbols:
            lines.append(f"当前前排观察标的：{', '.join(top_symbols)}。")
        if reasons:
            lines.append("主要学习归因/执行拦截原因：" + "；".join(reasons))
        root_causes = deep.get("root_causes") or []
        profit_drivers = deep.get("profit_drivers") or []
        if root_causes:
            lines.append("优先避免的亏损结构：" + "；".join(self._format_matrix_item(item) for item in root_causes[:3]))
        if profit_drivers:
            lines.append("优先寻找的盈利结构：" + "；".join(self._format_matrix_item(item) for item in profit_drivers[:3]))
        if message:
            lines.append(f"底层调用信息：{message}")
        return "\n".join(lines)

    def _local_fallback_answer(self, context: dict[str, Any], error: str, message: str = "") -> str:
        perf = context.get("performance") or {}
        positions = context.get("positions_summary") or {}
        last = context.get("last_autotrade_result") or {}
        guard_items = context.get("learning_guard_top10") or []
        guard_blocks = sum(1 for x in guard_items if x.get("severity") == "BLOCK")
        reverse_allowed = sum(1 for x in guard_items if x.get("reverse_allowed"))
        top_symbols = [x.get("symbol") for x in (context.get("radar_top20") or [])[:5]]
        reasons = []
        for item in guard_items[:3]:
            symbol = item.get("symbol") or "--"
            item_reasons = ", ".join(str(x) for x in (item.get("reasons") or [])[:3])
            if item_reasons:
                reasons.append(f"{symbol}: {item_reasons}")

        lines = [
            f"Codex CLI 未及时返回（{error}），先用本地风控上下文给结论。",
            (
                f"当前不开仓的主因是恢复模式={bool(perf.get('recovery_mode'))}、"
                f"总胜率={positions.get('win_rate', 0)}%、"
                f"最近胜率={round(float(perf.get('recent_win_rate') or 0) * 100, 2)}%、"
                f"连续亏损={perf.get('loss_streak', 0)}、"
                f"总盈亏={positions.get('total_pnl', perf.get('pnl', 0))}。"
            ),
            (
                f"候选侧目前也不干净：最近 run-once={last.get('decision') or last.get('reason') or '暂无'}，"
                f"学习守卫 Top10 中 BLOCK={guard_blocks}，允许反手={reverse_allowed}，"
                f"open_count={positions.get('open_count', 0)}。"
            ),
            (
                "所以现在不是单纯把空换成多就能解决，而是历史样本、三因子确认、收益/手续费结构和恢复模式共同把开仓挡住。"
            ),
        ]
        if top_symbols:
            lines.append(f"当前前排观察标的：{', '.join(str(x) for x in top_symbols if x)}。")
        if reasons:
            lines.append("主要拦截原因：" + "；".join(reasons))
        if message:
            lines.append(f"底层调用信息：{message}")
        return "\n".join(lines)

    def _public_context_summary(self, context: dict[str, Any]) -> dict[str, Any]:
        guard = context.get("learning_guard_top10") or []
        attribution = context.get("attribution_summary") or {}
        live_status = context.get("live_readiness") or {}
        return {
            "live_trading_enabled": context.get("autotrade_params", {}).get("live_trading_enabled"),
            "trade_mode": context.get("autotrade_params", {}).get("trade_mode"),
            "candidate_mode": context.get("autotrade_params", {}).get("candidate_mode"),
            "performance": context.get("performance"),
            "positions_summary": context.get("positions_summary"),
            "last_autotrade_result": context.get("last_autotrade_result"),
            "live_readiness": {
                "current_stage": live_status.get("current_stage"),
                "paper_is_terminal": live_status.get("paper_is_terminal"),
                "blockers": (live_status.get("blockers") or [])[:8],
                "next_actions": live_status.get("next_actions"),
            },
            "guard_counts": {
                "block": sum(1 for x in guard if x.get("severity") == "BLOCK"),
                "reverse_allowed": sum(1 for x in guard if x.get("reverse_allowed")),
            },
            "learning": {
                "sample_count": attribution.get("sample_count"),
                "global_win_rate": attribution.get("global_win_rate"),
                "global_profit_factor": attribution.get("global_profit_factor"),
                "global_pnl": attribution.get("global_pnl"),
                "pipeline": "learn_attribution_first_then_gate_execution",
            },
            "top_symbols": [x.get("symbol") for x in (context.get("radar_top20") or [])[:5]],
        }

    def _compact_deep_attribution(self, deep: dict[str, Any]) -> dict[str, Any]:
        return {
            "enabled": deep.get("enabled"),
            "sample_count": deep.get("sample_count"),
            "win_rate": deep.get("win_rate"),
            "profit_factor": deep.get("profit_factor"),
            "pnl": deep.get("pnl"),
            "loss_count": deep.get("loss_count"),
            "win_count": deep.get("win_count"),
            "root_causes": (deep.get("root_causes") or [])[:6],
            "profit_drivers": (deep.get("profit_drivers") or [])[:6],
            "close_reasons": (deep.get("close_reasons") or [])[:6],
            "action_rules": (deep.get("action_rules") or [])[:6],
            "instruction": deep.get("instruction"),
        }

    def _first_autotrade_result(self, last: dict[str, Any]) -> dict[str, Any]:
        results = last.get("results") if isinstance(last, dict) else None
        if isinstance(results, list) and results:
            first = results[0]
            return first if isinstance(first, dict) else {}
        return last if isinstance(last, dict) else {}

    def _format_matrix_item(self, item: dict[str, Any]) -> str:
        label = item.get("label") or item.get("code") or item.get("factor") or "--"
        samples = item.get("samples", 0)
        win_rate = round(float(item.get("win_rate") or 0.0) * 100, 2)
        profit_factor = item.get("profit_factor", 0)
        pnl = item.get("pnl", 0)
        return f"{label}(样本={samples}, 胜率={win_rate}%, PF={profit_factor}, PnL={pnl})"


def _qa_model() -> str:
    return str(settings.codex_qa_model or settings.codex_fast_model or settings.codex_model or "").strip()


def _qa_reasoning_effort() -> str:
    return normalized_reasoning_effort(settings.codex_qa_reasoning_effort, "low")


def _qa_service_tier() -> str:
    fast_model = str(settings.codex_fast_model or "").strip()
    if fast_model and _qa_model() == fast_model:
        return normalized_service_tier(settings.codex_fast_service_tier, "", allow_empty=True)
    return normalized_codex_service_tier()


def _qa_timeout_seconds() -> float:
    configured = settings.codex_qa_timeout_seconds or settings.codex_fast_timeout_seconds or settings.codex_timeout_seconds
    return min(max(float(configured or 60.0), 10.0), 120.0)


strategy_qa = StrategyQA()
