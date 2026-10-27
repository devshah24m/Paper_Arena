"""Angel One equity charges (NSE/BSE cash segment).

Rates are collected here so they are easy to re-verify against
https://www.angelone.in/list-of-charges when they change.
"""

RATES = {
    # Brokerage: lower of Rs20 or 0.1% per executed order, minimum Rs5.
    # Angel One's pages disagree on delivery (some say zero); set to True for zero.
    "delivery_brokerage_zero": False,
    "brk_pct": 0.001,
    "brk_cap": 20.0,
    "brk_min": 5.0,
    "stt_cnc_both": 0.001,       # delivery: 0.1% on buy and sell
    "stt_mis_sell": 0.00025,     # intraday: 0.025% on sell
    "txn": {"NSE": 0.0000297, "BSE": 0.0000375},
    "sebi": 0.000001,            # Rs10 per crore
    "ipft": 0.000001,            # NSE equity IPFT, Rs10 per crore (not GST-able)
    "stamp_cnc_buy": 0.00015,
    "stamp_mis_buy": 0.00003,
    "gst": 0.18,                 # on brokerage + txn + SEBI
    "dp": 20.0,                  # per scrip per day on delivery sell, + GST
}


def compute(side: str, product: str, turnover: float, exchange: str = "NSE", dp: bool = False) -> dict:
    r = RATES
    buy = side == "BUY"
    cnc = product == "CNC"
    if turnover <= 0:
        return {"total": 0.0}
    if cnc and r["delivery_brokerage_zero"]:
        brokerage = 0.0
    else:
        brokerage = min(max(r["brk_min"], min(r["brk_cap"], turnover * r["brk_pct"])), turnover)
    if cnc:
        stt = turnover * r["stt_cnc_both"]
    else:
        stt = 0.0 if buy else turnover * r["stt_mis_sell"]
    txn = turnover * r["txn"].get(exchange, r["txn"]["NSE"])
    sebi = turnover * r["sebi"]
    ipft = turnover * r["ipft"] if exchange == "NSE" else 0.0
    stamp = turnover * (r["stamp_cnc_buy"] if cnc else r["stamp_mis_buy"]) if buy else 0.0
    gst = r["gst"] * (brokerage + txn + sebi)
    dpc = r["dp"] * (1 + r["gst"]) if dp else 0.0
    d = {
        "brokerage": brokerage, "stt": stt, "exchange_txn": txn, "sebi": sebi,
        "ipft": ipft, "stamp_duty": stamp, "gst": gst, "dp": dpc,
    }
    d = {k: round(v, 2) for k, v in d.items()}
    d["total"] = round(sum(d.values()), 2)
    return d
