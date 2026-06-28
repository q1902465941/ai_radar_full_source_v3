from backend.trading.trade_economics import calc_roi, gross_pnl


def calc_pnl(side, entry, current, qty):
    return gross_pnl(side, entry, current, qty)
