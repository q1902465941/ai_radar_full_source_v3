# 猎妖人 AI Radar Full Source v3

这是一个完整独立源代码包：包含扫描监控网站、雷达扫描算法、本地 Codex 策略生成接口、动态自动交易算法、自动持仓管理算法、持仓页面、SQLite 本地持久化。

## 页面

- `/radar` 雷达中心：Top4候选卡片、Top50表格、8轮变化、升温斜率、资金确认、假突破风险、庄家雷达、SM占仓/动向。
- `/positions` 持仓管理：未平仓、已平仓、汇总卡片、手动平仓、Stage1/Stage2、TP/SL管理。
- `/settings` 控制页：扫描一次、AI自动交易一次、启动/停止自动交易循环。

## 启动

```bash
cd ai_radar_full_source_v2
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env  # Windows
# cp .env.example .env  # macOS/Linux
python run.py
```

打开：

```text
http://127.0.0.1:8000/radar
```

## 模式

默认可直接运行 mock；当前部署已切到 Binance Testnet 真实公开行情：

```env
MARKET_DATA_MODE=binance
BINANCE_TESTNET=true
TRADE_MODE=paper
AUTO_TRADING_ENABLED=false
LIVE_TRADING_ENABLED=false
```

切回本地模拟行情：

```env
MARKET_DATA_MODE=mock
```

Binance 模式会拉取真实 USD-M Futures 因子：K线涨跌、量能放大、盘口深度不平衡、当前 OI、资金费率、K线 taker buy/sell、ATR、插针比例。主网在当前机器网络下返回 451，Testnet 可用；主网网络/API环境打通后把 `BINANCE_TESTNET=false` 即可切换。

实盘下单默认关闭。即使开启 live，系统仍保留系统级保护校验：策略几何校验、精度校验、保护单失败处理、并发锁、重复 strategy_id 防护。

## 重点算法文件

```text
backend/radar/radar_engine.py          雷达主流程
backend/radar/score_engine.py          综合评分算法
backend/radar/heat_tracker.py          8轮变化和升温斜率
backend/radar/fund_confirm.py          资金确认0/3-3/3
backend/radar/fake_breakout.py         假突破风险
backend/radar/smart_money.py           SM占仓/动向估算
backend/radar/dealer_radar.py          庄家雷达标签
backend/trading/autotrader.py          自动交易主流程
backend/ai_strategy/context_compressor.py  AI上下文压缩
backend/ai_strategy/wait_manager.py    避免无限WAIT
backend/ai_strategy/dynamic_trade_model.py 动态自动交易算法
backend/positions/position_manager.py  自动持仓管理算法
```

## Codex 策略生成

运行时走 `backend/ai_strategy/openai_strategy_client.py`，当前配置接入本地 Codex CLI，不消耗 OpenAI API 余额。

```env
AI_ENABLED=true
AI_STRATEGY_PROVIDER=codex_cli
CODEX_MODEL=gpt-5.5
CODEX_REASONING_EFFORT=medium
CODEX_SERVICE_TIER=priority
```
