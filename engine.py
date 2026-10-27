"""Matching engine, positions, charges and leaderboard on SQLite."""
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import charges as ch
import config as C

IST = ZoneInfo("Asia/Kolkata")
_hk_lock = threading.Lock()
_last_hk = 0.0


# ------------------------------------------------------------------ db
@contextmanager
def db():
    con = sqlite3.connect(C.DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
    finally:
        con.close()


@contextmanager
def tx():
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, salt TEXT, pin_hash TEXT,
            created REAL, peak_exposure REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, symbol TEXT,
            side TEXT, qty INTEGER, otype TEXT, product TEXT, limit_price REAL, status TEXT,
            filled_qty INTEGER DEFAULT 0, avg_price REAL DEFAULT 0, charges REAL DEFAULT 0, note TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, user TEXT,
            symbol TEXT, side TEXT, qty INTEGER, price REAL, product TEXT, charges REAL, charge_detail TEXT,
            realized REAL, fills TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS positions(user TEXT, symbol TEXT, product TEXT, qty INTEGER, avg REAL,
            updated REAL, PRIMARY KEY(user, symbol, product));
        CREATE TABLE IF NOT EXISTS consumed(symbol TEXT, side TEXT, price REAL, qty REAL, ts REAL);
        CREATE INDEX IF NOT EXISTS ix_cons ON consumed(symbol, side);
        CREATE INDEX IF NOT EXISTS ix_tr_user ON trades(user);
        """)


# ------------------------------------------------------------------ auth
def _hash(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 100_000).hex()


def register(user, pin):
    user = user.strip()
    if not (3 <= len(user) <= 20) or not user.replace("_", "").replace(".", "").isalnum():
        return False, "Username: 3-20 chars, letters/numbers/_/."
    if len(pin) < 4:
        return False, "PIN must be at least 4 characters"
    salt = secrets.token_hex(8)
    try:
        with tx() as con:
            con.execute("INSERT INTO users(username,salt,pin_hash,created) VALUES(?,?,?,?)",
                        (user, salt, _hash(pin, salt), time.time()))
    except sqlite3.IntegrityError:
        return False, "Username taken"
    return True, "Registered"


def login(user, pin):
    with db() as con:
        r = con.execute("SELECT * FROM users WHERE username=?", (user.strip(),)).fetchone()
    if r and secrets.compare_digest(r["pin_hash"], _hash(pin, r["salt"])):
        return True
    return False


# ------------------------------------------------------------------ time
def now_ist():
    return datetime.now(IST)


def market_open(dt=None):
    dt = dt or now_ist()
    return dt.weekday() < 5 and dtime(9, 15) <= dt.time() < dtime(15, 30)


def _day_start(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ------------------------------------------------------------------ order book model
def _rt(x, tick):
    return round(round(x / tick) * tick, 2)


def _consumed(con, symbol, side, now):
    m = {}
    for r in con.execute("SELECT price, qty, ts FROM consumed WHERE symbol=? AND side=?", (symbol, side)):
        m[r["price"]] = m.get(r["price"], 0.0) + r["qty"] * 0.5 ** ((now - r["ts"]) / C.HALF_LIFE)
    return m


def _ladder(con, quote, side, now):
    """Liquidity a `side` order can take, best price first, after shared paper-trader impact."""
    buy = side == "BUY"
    real = quote["sell"] if buy else quote["buy"]
    lv = [[round(p, 2), int(q)] for p, q in real if p > 0 and q > 0]
    tick = quote.get("tick") or 0.05
    ltp = quote["ltp"]
    avg = max(C.SYNTH_MIN_QTY, int(sum(q for _, q in lv) / len(lv))) if lv else C.SYNTH_MIN_QTY * 5
    last = lv[-1][0] if lv else ltp
    step = max(tick, _rt(last * C.SYNTH_STEP_PCT, tick))
    cap = quote.get("upper") if buy else quote.get("lower")
    for _ in range(C.SYNTH_LEVELS):
        last = _rt(last + step if buy else last - step, tick)
        if last <= 0 or (cap and ((buy and last > cap + 1e-9) or (not buy and last < cap - 1e-9))):
            break
        lv.append([last, avg])
    cons = _consumed(con, quote["symbol"], "A" if buy else "B", now)
    for l in lv:
        l[1] = max(0, int(l[1] - cons.get(l[0], 0)))
    return [l for l in lv if l[1] > 0]


def _cap_slippage(ladder, quote, side):
    pct = C.MAX_SLIPPAGE_PCT / 100
    if side == "BUY":
        lim = quote["ltp"] * (1 + pct)
        return [l for l in ladder if l[0] <= lim]
    lim = quote["ltp"] * (1 - pct)
    return [l for l in ladder if l[0] >= lim]


def _walk(ladder, qty, limit, side):
    fills, rem = [], qty
    for p, q in ladder:
        if rem <= 0:
            break
        if limit is not None and ((side == "BUY" and p > limit + 1e-9) or (side == "SELL" and p < limit - 1e-9)):
            break
        t = min(q, rem)
        fills.append((p, t))
        rem -= t
    return fills, rem


def virtual_book(quote, depth=8):
    """What the arena book looks like right now (real depth minus paper impact)."""
    now = time.time()
    with db() as con:
        asks = _ladder(con, quote, "BUY", now)[:depth]
        bids = _ladder(con, quote, "SELL", now)[:depth]
    return bids, asks


# ------------------------------------------------------------------ positions
def apply_fill(pq, pavg, side, qty, price):
    signed = qty if side == "BUY" else -qty
    realized = 0.0
    if pq == 0 or (pq > 0) == (signed > 0):
        nq = pq + signed
        navg = (abs(pq) * pavg + qty * price) / abs(nq)
    else:
        closing = min(abs(pq), qty)
        realized = closing * (price - pavg) if pq > 0 else closing * (pavg - price)
        nq = pq + signed
        navg = 0.0 if nq == 0 else (pavg if (nq > 0) == (pq > 0) else price)
    return nq, navg, realized


def _pos(con, user, symbol, product):
    r = con.execute("SELECT qty, avg FROM positions WHERE user=? AND symbol=? AND product=?",
                    (user, symbol, product)).fetchone()
    return (r["qty"], r["avg"]) if r else (0, 0.0)


def _update_peak(con, user):
    r = con.execute("SELECT COALESCE(SUM(ABS(qty)*avg),0) e FROM positions WHERE user=?", (user,)).fetchone()
    con.execute("UPDATE users SET peak_exposure=MAX(peak_exposure, ?) WHERE username=?", (r["e"], user))


# ------------------------------------------------------------------ execution
def _execute(con, o, quote, now):
    """Match as much of order `o` (dict) as possible against the book. Returns summary dict."""
    side = o["side"]
    rem = o["qty"] - o["filled_qty"]
    ladder = _ladder(con, quote, side, now)
    limit = o["limit_price"] if o["otype"] == "LIMIT" else None
    if o["otype"] == "MARKET":
        ladder = _cap_slippage(ladder, quote, side)
    fills, left = _walk(ladder, rem, limit, side)
    filled = rem - left
    total_filled = o["filled_qty"] + filled
    charges_total = o["charges"]
    avg_run = o["avg_price"]
    if filled > 0:
        turnover = sum(p * q for p, q in fills)
        avg = turnover / filled
        pq, pavg = _pos(con, o["user"], o["symbol"], o["product"])
        nq, navg, realized = apply_fill(pq, pavg, side, filled, avg)
        dp = False
        if o["product"] == "CNC" and side == "SELL":
            dp = con.execute(
                "SELECT 1 FROM trades WHERE user=? AND symbol=? AND product='CNC' AND side='SELL' AND ts>=? LIMIT 1",
                (o["user"], o["symbol"], _day_start(now_ist()))).fetchone() is None
        c = ch.compute(side, o["product"], turnover, "NSE", dp)
        con.execute("INSERT INTO trades(order_id,user,symbol,side,qty,price,product,charges,charge_detail,realized,fills,ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (o["id"], o["user"], o["symbol"], side, filled, avg, o["product"], c["total"],
                     json.dumps(c), realized, json.dumps(fills), now))
        con.execute("INSERT OR REPLACE INTO positions(user,symbol,product,qty,avg,updated) VALUES(?,?,?,?,?,?)",
                    (o["user"], o["symbol"], o["product"], nq, navg, now))
        cside = "A" if side == "BUY" else "B"
        con.executemany("INSERT INTO consumed(symbol,side,price,qty,ts) VALUES(?,?,?,?,?)",
                        [(o["symbol"], cside, round(p, 2), q, now) for p, q in fills])
        con.execute("DELETE FROM consumed WHERE ts<?", (now - 12 * C.HALF_LIFE,))
        avg_run = (o["avg_price"] * o["filled_qty"] + turnover) / total_filled
        charges_total += c["total"]
        _update_peak(con, o["user"])
    if total_filled >= o["qty"]:
        status, note = "FILLED", o.get("note")
    elif o["otype"] == "LIMIT":
        status, note = "OPEN", o.get("note")
    else:
        status = "PARTIAL" if total_filled else "REJECTED"
        note = "Liquidity exhausted within slippage cap; remainder cancelled"
    con.execute("UPDATE orders SET filled_qty=?, avg_price=?, charges=?, status=?, note=? WHERE id=?",
                (total_filled, avg_run, charges_total, status, note, o["id"]))
    return {"status": status, "filled": filled, "total_filled": total_filled, "avg": avg_run,
            "charges": charges_total, "note": note, "order_id": o["id"]}


def _validate(con, user, side, qty, otype, product, limit, quote):
    if not quote or quote["ltp"] <= 0:
        return "No live quote for this scrip right now"
    if not isinstance(qty, int) or qty <= 0 or qty > C.MAX_QTY:
        return f"Quantity must be between 1 and {C.MAX_QTY:,}"
    if side not in ("BUY", "SELL") or otype not in ("MARKET", "LIMIT") or product not in ("MIS", "CNC"):
        return "Invalid order parameters"
    if otype == "LIMIT" and (limit is None or limit <= 0):
        return "Limit price required"
    if time.time() - quote["ts"] > 30:
        return "Quote is stale (feed issue) - try again"
    if C.ENFORCE_HOURS and not market_open():
        return "Market is closed (9:15-15:30 IST, Mon-Fri)"
    if product == "MIS" and C.ENFORCE_HOURS:
        n = now_ist()
        if (n.hour, n.minute) >= C.SQUAREOFF_HM:
            return "MIS orders are blocked after the 15:20 auto square-off"
    if product == "CNC" and side == "SELL":
        pq, _ = _pos(con, user, quote["symbol"], "CNC")
        pend = con.execute("SELECT COALESCE(SUM(qty-filled_qty),0) s FROM orders WHERE user=? AND symbol=? AND "
                           "product='CNC' AND side='SELL' AND status='OPEN'", (user, quote["symbol"])).fetchone()["s"]
        if qty > pq - pend:
            return f"Delivery sell exceeds holdings ({pq} held, {pend} already in open sell orders)"
    return None


def place_order(user, quote, side, qty, otype, product, limit_price=None):
    with tx() as con:
        err = _validate(con, user, side, qty, otype, product, limit_price, quote)
        if err:
            return {"ok": False, "msg": err}
        if otype == "LIMIT":
            limit_price = _rt(limit_price, quote.get("tick") or 0.05)
        now = time.time()
        cur = con.execute("INSERT INTO orders(user,symbol,side,qty,otype,product,limit_price,status,ts)"
                          " VALUES(?,?,?,?,?,?,?,?,?)",
                          (user, quote["symbol"], side, qty, otype, product, limit_price, "OPEN", now))
        o = {"id": cur.lastrowid, "user": user, "symbol": quote["symbol"], "side": side, "qty": qty,
             "otype": otype, "product": product, "limit_price": limit_price, "filled_qty": 0,
             "avg_price": 0.0, "charges": 0.0, "note": None}
        res = _execute(con, o, quote, now)
    res["ok"] = res["status"] != "REJECTED"
    res["msg"] = {"FILLED": "Order filled", "PARTIAL": "Partially filled", "OPEN": "Order resting in book",
                  "REJECTED": res.get("note") or "Rejected"}[res["status"]]
    return res


def preview(user, quote, side, qty, otype, product, limit_price=None):
    """Dry-run: expected fills and charges without touching state."""
    if not quote or qty <= 0:
        return None
    now = time.time()
    with db() as con:
        err = _validate(con, user, side, qty, otype, product, limit_price, quote) if user else None
        ladder = _ladder(con, quote, side, now)
        if otype == "MARKET":
            ladder = _cap_slippage(ladder, quote, side)
        lim = limit_price if otype == "LIMIT" else None
        fills, left = _walk(ladder, qty, lim, side)
        filled = qty - left
        turnover = sum(p * q for p, q in fills)
        dp = False
        if user and product == "CNC" and side == "SELL":
            dp = con.execute("SELECT 1 FROM trades WHERE user=? AND symbol=? AND product='CNC' AND side='SELL' AND ts>=?",
                             (user, quote["symbol"], _day_start(now_ist()))).fetchone() is None
    avg = turnover / filled if filled else 0
    ref = quote["ltp"]
    slip = ((avg - ref) / ref * 100 * (1 if side == "BUY" else -1)) if filled else 0
    return {"error": err, "filled": filled, "left": left, "avg": avg, "slippage_pct": slip,
            "levels": len(fills), "charges": ch.compute(side, product, turnover, "NSE", dp), "turnover": turnover,
            "last_price": fills[-1][0] if fills else None}


def cancel_order(user, oid):
    with tx() as con:
        n = con.execute("UPDATE orders SET status='CANCELLED' WHERE id=? AND user=? AND status='OPEN'",
                        (oid, user)).rowcount
    return n > 0


# ------------------------------------------------------------------ housekeeping
def housekeeping(feed, min_gap=5.0):
    """Fill resting limit orders, expire day orders, auto square-off MIS. Safe to call often."""
    global _last_hk
    if time.time() - _last_hk < min_gap or not _hk_lock.acquire(blocking=False):
        return
    try:
        _last_hk = time.time()
        n = now_ist()
        today0 = _day_start(n)
        with db() as con:
            open_orders = [dict(r) for r in con.execute("SELECT * FROM orders WHERE status='OPEN' ORDER BY id")]
            mis = [dict(r) for r in con.execute("SELECT * FROM positions WHERE product='MIS' AND qty!=0")]
        syms = {o["symbol"] for o in open_orders} | {p["symbol"] for p in mis}
        if not syms:
            return
        quotes = feed.quotes(list(syms))
        live = (not C.ENFORCE_HOURS) or market_open(n)
        due_time = (n.hour, n.minute) >= C.SQUAREOFF_HM and (n.weekday() < 5 or not C.ENFORCE_HOURS)
        for o in open_orders:
            q = quotes.get(o["symbol"])
            stale_day = o["ts"] < today0
            mis_due = o["product"] == "MIS" and (due_time or stale_day)
            with tx() as con:
                cur = con.execute("SELECT status FROM orders WHERE id=?", (o["id"],)).fetchone()
                if not cur or cur["status"] != "OPEN":
                    continue
                if stale_day or mis_due:
                    con.execute("UPDATE orders SET status='CANCELLED', note='Day order expired' WHERE id=?", (o["id"],))
                    continue
                if q and live and time.time() - q["ts"] < 30:
                    o2 = dict(con.execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone())
                    _execute(con, o2, q, time.time())
        for p in mis:
            if not (due_time or p["updated"] < today0):
                continue
            q = quotes.get(p["symbol"])
            if not q or time.time() - q["ts"] > 30:
                continue
            with tx() as con:
                pq, _ = _pos(con, p["user"], p["symbol"], "MIS")
                if pq == 0:
                    continue
                side = "SELL" if pq > 0 else "BUY"
                cur = con.execute("INSERT INTO orders(user,symbol,side,qty,otype,product,status,note,ts)"
                                  " VALUES(?,?,?,?,?,?,?,?,?)",
                                  (p["user"], p["symbol"], side, abs(pq), "MARKET", "MIS", "OPEN",
                                   "AUTO SQUARE-OFF", time.time()))
                o = {"id": cur.lastrowid, "user": p["user"], "symbol": p["symbol"], "side": side, "qty": abs(pq),
                     "otype": "MARKET", "product": "MIS", "limit_price": None, "filled_qty": 0, "avg_price": 0.0,
                     "charges": 0.0, "note": "AUTO SQUARE-OFF"}
                _execute(con, o, q, time.time())
    finally:
        _hk_lock.release()


# ------------------------------------------------------------------ public views
def open_symbols():
    with db() as con:
        return [r["symbol"] for r in con.execute("SELECT DISTINCT symbol FROM positions WHERE qty!=0")]


def positions_of(user, ltp):
    with db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM positions WHERE user=? AND qty!=0", (user,))]
    for r in rows:
        r["ltp"] = ltp.get(r["symbol"], r["avg"])
        r["unrealized"] = (r["ltp"] - r["avg"]) * r["qty"]
        r["pct"] = (r["ltp"] / r["avg"] - 1) * 100 * (1 if r["qty"] > 0 else -1) if r["avg"] else 0
    return rows


def leaderboard(ltp):
    with db() as con:
        users = [dict(r) for r in con.execute("SELECT username, peak_exposure FROM users")]
        agg = {r["user"]: dict(r) for r in con.execute(
            "SELECT user, COUNT(*) n, SUM(realized) realized, SUM(charges) charges, "
            "SUM(CASE WHEN realized>0 THEN 1 ELSE 0 END) wins, "
            "SUM(CASE WHEN realized!=0 THEN 1 ELSE 0 END) closes FROM trades GROUP BY user")}
        pos = [dict(r) for r in con.execute("SELECT * FROM positions WHERE qty!=0")]
    unreal, npos = {}, {}
    for p in pos:
        l = ltp.get(p["symbol"], p["avg"])
        unreal[p["user"]] = unreal.get(p["user"], 0.0) + (l - p["avg"]) * p["qty"]
        npos[p["user"]] = npos.get(p["user"], 0) + 1
    rows = []
    for u in users:
        a = agg.get(u["username"], {})
        real, chg = a.get("realized") or 0.0, a.get("charges") or 0.0
        un = unreal.get(u["username"], 0.0)
        net = real + un - chg
        pk = u["peak_exposure"] or 0.0
        rows.append({"Trader": u["username"], "Net P&L": net, "Return %": (net / pk * 100) if pk else 0.0,
                     "Realized": real, "Unrealized": un, "Charges": chg, "Trades": a.get("n", 0),
                     "Win %": (a["wins"] / a["closes"] * 100) if a.get("closes") else 0.0,
                     "Open pos": npos.get(u["username"], 0), "Peak capital used": pk})
    rows.sort(key=lambda r: r["Net P&L"], reverse=True)
    return rows


def trades_of(user, limit=100):
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM trades WHERE user=? ORDER BY id DESC LIMIT ?", (user, limit))]


def orders_of(user, limit=50):
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM orders WHERE user=? ORDER BY id DESC LIMIT ?", (user, limit))]


def recent_trades(limit=40):
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))]
