# Binance USD-M Futures 账户与交易 API 接入说明

本版本已经补齐交易所账户与交易链路，不再只靠 mock / paper 数据。

## 已接入的账户接口

- `GET /fapi/v2/balance`：查询 USDT-M Futures 账户余额
- `GET /fapi/v2/account`：查询账户信息、可用余额、保证金、未实现盈亏
- `GET /fapi/v2/positionRisk`：查询交易所真实持仓风险
- `GET /fapi/v1/openOrders`：查询交易所未成交订单
- `GET /fapi/v1/income`：查询资金流水 / 收益历史

## 已接入的交易接口

- `POST /fapi/v1/leverage`：设置杠杆
- `POST /fapi/v1/marginType`：设置逐仓 / 全仓
- `POST /fapi/v1/order/test`：测试下单，不进入撮合
- `POST /fapi/v1/order`：真实下单
- `DELETE /fapi/v1/order`：撤单
- `DELETE /fapi/v1/allOpenOrders`：撤销某个 symbol 的全部挂单

## 自动交易执行链

```text
AutoTrader
  ↓
AccountService 读取真实账户余额
  ↓
DynamicTradeModel 计算动态仓位 / 杠杆
  ↓
LiveExecutor
  ├─ change_margin_type
  ├─ change_leverage
  ├─ MARKET 开仓
  ├─ STOP_MARKET 保护止损
  └─ TAKE_PROFIT_MARKET 保护止盈
```

## 默认安全状态

`.env.example` 默认：

```env
TRADE_MODE=paper
LIVE_TRADING_ENABLED=false
LIVE_USE_TEST_ORDER=true
BINANCE_TESTNET=true
```

也就是：默认不会真实下单。

要测试 Testnet 真实 API 链路：

```env
BINANCE_TESTNET=true
TRADE_MODE=live
LIVE_TRADING_ENABLED=true
LIVE_USE_TEST_ORDER=true
BINANCE_API_KEY=你的testnet key
BINANCE_API_SECRET=你的testnet secret
```

要真实提交 Binance Futures 主网订单，必须同时改：

```env
BINANCE_TESTNET=false
TRADE_MODE=live
LIVE_TRADING_ENABLED=true
LIVE_USE_TEST_ORDER=false
```

## 注意

真实交易前必须先在 Testnet 验证：账户余额、下单、保护单、手动平仓、交易所持仓同步。
