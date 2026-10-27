import os, time, tempfile
os.environ["MOCK_FEED"] = "1"
os.environ["PAPER_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import engine, charges as ch, feed as F

engine.init_db()
f = F.make_feed()

# --- charges: 100 shares @1000 delivery buy / intraday sell
c = ch.compute("BUY", "CNC", 100000)
assert c["brokerage"] == 20 and c["stt"] == 100 and c["stamp_duty"] == 15, c
assert abs(c["gst"] - 0.18 * (20 + 2.97 + 0.1)) < 0.01, c
s = ch.compute("SELL", "MIS", 100000)
assert s["stt"] == 25 and s["stamp_duty"] == 0 and s["brokerage"] == 20, s
assert ch.compute("SELL", "CNC", 100000, dp=True)["dp"] == 23.6
print("charges ok", c["total"], s["total"])

# --- position maths
assert engine.apply_fill(0, 0, "BUY", 10, 100) == (10, 100, 0)
assert engine.apply_fill(10, 100, "SELL", 4, 110) == (6, 100, 40)
nq, na, r = engine.apply_fill(5, 100, "SELL", 8, 90)   # flip long->short
assert (nq, na, r) == (-3, 90, -50)

# --- impact: big order walks depth, then next trader sees worse price
assert engine.register("alice", "1234")[0] and engine.register("bob", "1234")[0]
assert not engine.register("alice", "1234")[0]
assert engine.login("alice", "1234") and not engine.login("alice", "9999")
q = f.quotes(["SBIN-EQ"])["SBIN-EQ"]
best_ask = q["sell"][0]
avail_l1 = best_ask[1]
r1 = engine.place_order("alice", q, "BUY", avail_l1 * 2 + 50, "MARKET", "MIS")
assert r1["ok"] and r1["status"] == "FILLED", r1
assert r1["avg"] > best_ask[0], (r1["avg"], best_ask)
print("alice fill", r1["total_filled"], "avg", round(r1["avg"], 3), "best ask was", best_ask)
q2 = f.quotes(["SBIN-EQ"])["SBIN-EQ"]
bids, asks = engine.virtual_book(q2)
assert asks[0][0] > q2["sell"][0][0] - 1e-9 and asks[0][0] >= best_ask[0] + 0.05 - 1e-6, (asks[:2], q2["sell"][:2])
pv = engine.preview("bob", q2, "BUY", 100, "MARKET", "MIS")
assert pv["avg"] >= asks[0][0] - 1e-6
print("bob sees ask", asks[0], "after alice's buy")

# --- limit order rests then fills via housekeeping
q = f.quotes(["TCS-EQ"])["TCS-EQ"]
lim = q["buy"][0][0] - 5
r = engine.place_order("bob", q, "BUY", 10, "LIMIT", "CNC", lim)
assert r["status"] == "OPEN", r
assert not engine.place_order("bob", q, "SELL", 50, "MARKET", "CNC")["ok"]  # no holdings
f.px["TCS-EQ"] = lim - 1
q = f.quotes(["TCS-EQ"])["TCS-EQ"]
engine._last_hk = 0
engine.housekeeping(f, 0)
o = engine.orders_of("bob")[0]
assert o["status"] == "FILLED", o

# --- cnc sell with DP charge once per day
q = f.quotes(["TCS-EQ"])["TCS-EQ"]
r = engine.place_order("bob", q, "SELL", 4, "MARKET", "CNC")
assert r["ok"]
t = engine.trades_of("bob", 1)[0]
assert '"dp": 23.6' in t["charge_detail"], t["charge_detail"]
engine.place_order("bob", q, "SELL", 3, "MARKET", "CNC")
assert '"dp": 0.0' in engine.trades_of("bob", 1)[0]["charge_detail"]

# --- MIS auto square-off when stale
import sqlite3, config as C
con = sqlite3.connect(C.DB_PATH); con.execute("UPDATE positions SET updated=updated-86400*2 WHERE product='MIS'"); con.commit(); con.close()
engine._last_hk = 0
engine.housekeeping(f, 0)
assert not [p for p in engine.positions_of("alice", {}) if p["product"] == "MIS"]

# --- slippage cap
q = f.quotes(["IDEA-EQ"])["IDEA-EQ"]
big = engine.place_order("alice", q, "BUY", 50_000_000 // 10, "MARKET", "MIS")
print("huge order:", big["status"], big.get("total_filled"), big.get("note"))

lb = engine.leaderboard({s: f.px[s] for s in f.px})
print([(r["Trader"], round(r["Net P&L"], 2), round(r["Return %"], 3)) for r in lb])
print("ALL OK")
