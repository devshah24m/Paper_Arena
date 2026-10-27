import time
from datetime import datetime

import pandas as pd
import streamlit as st

import charges as ch
import config as C
import engine
import feed as feedmod

st.set_page_config(page_title="Paper Arena", page_icon="📈", layout="wide")
engine.init_db()


@st.cache_resource
def get_feed():
    return feedmod.make_feed()


feed = get_feed()
user = st.session_state.get("user")


def money(x):
    return f"₹{x:,.2f}"


def color_pn(v):
    if isinstance(v, (int, float)):
        return "color: #16a34a" if v > 0 else ("color: #dc2626" if v < 0 else "")
    return ""


def style(df, pn_cols, fmt):
    s = df.style.format(fmt)
    f = getattr(s, "map", None) or s.applymap
    return f(color_pn, subset=pn_cols)


def ltp_map():
    syms = engine.open_symbols()
    return {s: q["ltp"] for s, q in feed.quotes(syms).items()} if syms else {}


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("📈 Paper Arena")
    if C.MOCK:
        st.warning("MOCK feed (no Angel One credentials set)")
    st.caption("Market OPEN" if engine.market_open() else "Market CLOSED"
               + ("" if C.ENFORCE_HOURS else " (hours not enforced)"))
    if user:
        st.success(f"Logged in as **{user}**")
        if st.button("Log out"):
            st.session_state.pop("user")
            st.rerun()
    else:
        mode = st.radio("Account", ["Login", "Register"], horizontal=True)
        u = st.text_input("Username")
        p = st.text_input("PIN / password", type="password")
        if st.button(mode):
            if mode == "Register":
                ok, msg = engine.register(u, p)
                if ok:
                    st.session_state["user"] = u.strip()
                    st.rerun()
                st.error(msg)
            elif engine.login(u, p):
                st.session_state["user"] = u.strip()
                st.rerun()
            else:
                st.error("Wrong username or PIN")
    st.markdown("---")
    st.caption("No capital limit. Fills walk the live Angel One depth; your orders eat liquidity that "
               "everyone sees, and it recovers over time. Angel One charges applied on every fill.")


# ------------------------------------------------------------------ shared views
def trader_view(name, key):
    ltp = ltp_map()
    pos = engine.positions_of(name, ltp)
    st.subheader(f"{name}'s open positions")
    if pos:
        df = pd.DataFrame([{"Scrip": p["symbol"], "Product": p["product"], "Qty": p["qty"], "Avg": p["avg"],
                            "LTP": p["ltp"], "Unrealized ₹": p["unrealized"], "Return %": p["pct"]} for p in pos])
        st.dataframe(style(df, ["Unrealized ₹", "Return %"],
                           {"Avg": "{:.2f}", "LTP": "{:.2f}", "Unrealized ₹": "{:,.2f}", "Return %": "{:+.2f}"}),
                     hide_index=True, use_container_width=True)
    else:
        st.caption("No open positions")
    tr = engine.trades_of(name, 100)
    st.subheader("Trade history")
    if tr:
        df = pd.DataFrame([{"Time": datetime.fromtimestamp(t["ts"], engine.IST).strftime("%d %b %H:%M:%S"),
                            "Scrip": t["symbol"], "Side": t["side"], "Product": t["product"], "Qty": t["qty"],
                            "Avg price": t["price"], "Charges ₹": t["charges"], "Realized ₹": t["realized"]}
                           for t in tr])
        st.dataframe(style(df, ["Realized ₹"], {"Avg price": "{:.2f}", "Charges ₹": "{:.2f}", "Realized ₹": "{:,.2f}"}),
                     hide_index=True, use_container_width=True)
    else:
        st.caption("No trades yet")


@st.fragment(run_every=5)
def leaderboard_tab():
    engine.housekeeping(feed)
    rows = engine.leaderboard(ltp_map())
    if not rows:
        st.info("No traders yet - register and place the first trade!")
        return
    df = pd.DataFrame(rows)
    df.insert(0, "Rank", range(1, len(df) + 1))
    st.dataframe(style(df, ["Net P&L", "Return %", "Realized", "Unrealized"],
                       {"Net P&L": "{:,.2f}", "Return %": "{:+.2f}", "Realized": "{:,.2f}", "Unrealized": "{:,.2f}",
                        "Charges": "{:,.2f}", "Win %": "{:.0f}", "Peak capital used": "{:,.0f}"}),
                 hide_index=True, use_container_width=True)
    st.caption("Net P&L = realized + unrealized − Angel One charges. Return % = Net P&L ÷ peak capital deployed "
               "(highest cost value of open positions at any time).")
    names = [r["Trader"] for r in rows]
    pick = st.selectbox("Inspect a trader", names, index=names.index(user) if user in names else 0, key="inspect")
    trader_view(pick, "lb")


@st.fragment(run_every=3)
def depth_view(sym):
    q = feed.quotes([sym]).get(sym)
    if not q:
        st.error("No quote available")
        return
    bids, asks = engine.virtual_book(q)
    c1, c2, c3 = st.columns(3)
    c1.metric("LTP", f"{q['ltp']:.2f}", f"{(q['ltp'] / q['close'] - 1) * 100:+.2f}%" if q["close"] else None)
    c2.metric("Best bid (arena)", f"{bids[0][0]:.2f}" if bids else "-")
    c3.metric("Best ask (arena)", f"{asks[0][0]:.2f}" if asks else "-")
    b = pd.DataFrame(bids, columns=["Bid price", "Bid qty"])
    a = pd.DataFrame(asks, columns=["Ask price", "Ask qty"])
    cb, ca = st.columns(2)
    cb.dataframe(b, hide_index=True, use_container_width=True)
    ca.dataframe(a, hide_index=True, use_container_width=True)
    st.caption("Arena book = live exchange depth minus liquidity already consumed by paper traders (recovers over time). "
               "Beyond 5 real levels, depth is extrapolated up to the circuit limit.")


def trade_tab():
    q_text = st.text_input("Search scrip (symbol or name)", "RELIANCE")
    matches = feed.search(q_text)
    if not matches:
        st.warning("No match")
        return
    sym = st.selectbox("Scrip", [m["symbol"] for m in matches],
                       format_func=lambda s: f"{s} — {next(m['name'] for m in matches if m['symbol'] == s)}")
    depth_view(sym)
    if not user:
        st.info("Log in from the sidebar to place orders.")
        return
    st.markdown("#### Order")
    c = st.columns(5)
    side = c[0].radio("Side", ["BUY", "SELL"], horizontal=True)
    qty = c[1].number_input("Quantity", min_value=1, value=100, step=1)
    otype = c[2].radio("Type", ["MARKET", "LIMIT"], horizontal=True)
    product = c[3].radio("Product", ["MIS", "CNC"], horizontal=True, help="MIS = intraday (auto square-off 15:20), CNC = delivery")
    limit = c[4].number_input("Limit price", min_value=0.0, value=0.0, step=0.05, disabled=(otype == "MARKET"))
    quote = feed.quotes([sym]).get(sym)
    pv = engine.preview(user, quote, side, int(qty), otype, product, limit if otype == "LIMIT" else None)
    if pv:
        if pv["error"]:
            st.warning(pv["error"])
        if pv["filled"]:
            m = st.columns(4)
            m[0].metric("Expected fill", f"{pv['filled']:,} @ {pv['avg']:.2f}")
            m[1].metric("Slippage vs LTP", f"{pv['slippage_pct']:+.2f}%")
            m[2].metric("Levels consumed", pv["levels"])
            m[3].metric("Charges", money(pv["charges"]["total"]))
            with st.expander("Charge breakup"):
                st.json({k: v for k, v in pv["charges"].items() if v})
        if pv["left"]:
            st.caption(f"{pv['left']:,} would remain unfilled" + (" (rests as open limit order)" if otype == "LIMIT" else " (cancelled)"))
    if st.button(f"Place {side} order", type="primary"):
        r = engine.place_order(user, quote, side, int(qty), otype, product, limit if otype == "LIMIT" else None)
        (st.success if r["ok"] else st.error)(
            r["msg"] + (f" — {r['total_filled']:,} @ {r['avg']:.2f}, charges {money(r['charges'])}" if r.get("total_filled") else ""))


@st.fragment(run_every=5)
def portfolio_tab():
    if not user:
        st.info("Log in to see your portfolio.")
        return
    engine.housekeeping(feed)
    trader_view(user, "me")
    st.subheader("Orders")
    orders = engine.orders_of(user)
    for o in orders[:15]:
        c = st.columns([5, 1])
        lp = f" @ {o['limit_price']}" if o["otype"] == "LIMIT" else ""
        c[0].write(f"#{o['id']} {o['side']} {o['qty']} {o['symbol']} {o['otype']}{lp} {o['product']} — "
                   f"**{o['status']}** ({o['filled_qty']}/{o['qty']}) {o['note'] or ''}")
        if o["status"] == "OPEN" and c[1].button("Cancel", key=f"c{o['id']}"):
            engine.cancel_order(user, o["id"])
            st.rerun()


@st.fragment(run_every=4)
def feed_tab():
    rows = engine.recent_trades(50)
    if not rows:
        st.info("No trades yet")
        return
    df = pd.DataFrame([{"Time": datetime.fromtimestamp(t["ts"], engine.IST).strftime("%H:%M:%S"), "Trader": t["user"],
                        "Side": t["side"], "Scrip": t["symbol"], "Qty": t["qty"], "Avg price": t["price"],
                        "Product": t["product"]} for t in rows])
    st.dataframe(df.style.format({"Avg price": "{:.2f}"}), hide_index=True, use_container_width=True)


t1, t2, t3, t4 = st.tabs(["🏆 Leaderboard", "💹 Trade", "👤 My Portfolio", "📡 Live Trades"])
with t1:
    leaderboard_tab()
with t2:
    trade_tab()
with t3:
    portfolio_tab()
with t4:
    feed_tab()
