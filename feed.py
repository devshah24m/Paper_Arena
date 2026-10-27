"""Market data: Angel One SmartAPI (real) or a random-walk mock (for testing / off-hours).

Quote dict: {symbol, name, ltp, buy:[(price,qty)..], sell:[(price,qty)..], tick, upper, lower, close, ts}
"""
import json
import os
import random
import threading
import time

import requests

import config as C

MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
MASTER_CACHE = "scrip_master_nse_eq.json"


def _load_master():
    if os.path.exists(MASTER_CACHE) and time.time() - os.path.getmtime(MASTER_CACHE) < 86400:
        with open(MASTER_CACHE) as f:
            return json.load(f)
    raw = requests.get(MASTER_URL, timeout=90).json()
    out = []
    for r in raw:
        if r.get("exch_seg") == "NSE" and str(r.get("symbol", "")).endswith("-EQ"):
            try:
                tick = float(r.get("tick_size", 5)) / 100.0
            except Exception:
                tick = 0.05
            out.append({"symbol": r["symbol"], "name": r.get("name", ""), "token": r["token"], "tick": tick or 0.05})
    with open(MASTER_CACHE, "w") as f:
        json.dump(out, f)
    return out


class _Base:
    master: list

    def search(self, text: str, limit: int = 40):
        t = text.strip().upper()
        if not t:
            return self.master[:limit]
        starts = [m for m in self.master if m["symbol"].upper().startswith(t)]
        rest = [m for m in self.master if t in m["symbol"].upper() or t in m["name"].upper()]
        seen, out = set(), []
        for m in starts + rest:
            if m["symbol"] not in seen:
                seen.add(m["symbol"])
                out.append(m)
        return out[:limit]


class AngelFeed(_Base):
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}
        self.ttl = 2.0
        self.last_call = 0.0
        self.master = _load_master()
        self.by_symbol = {m["symbol"]: m for m in self.master}
        self._login()

    def _login(self):
        import pyotp
        from SmartApi import SmartConnect
        self.api = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
        totp = pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now()
        d = self.api.generateSession(os.environ["ANGEL_CLIENT_ID"], os.environ["ANGEL_PIN"], totp)
        if not d or not d.get("status"):
            raise RuntimeError(f"Angel One login failed: {d}")

    def _fetch(self, tokens):
        wait = 1.1 - (time.time() - self.last_call)  # API allows ~1 req/sec
        if wait > 0:
            time.sleep(wait)
        self.last_call = time.time()
        return self.api.getMarketData("FULL", {"NSE": tokens})

    def quotes(self, symbols):
        now = time.time()
        out, need = {}, []
        for s in set(symbols):
            c = self.cache.get(s)
            if c and now - c["ts"] < self.ttl:
                out[s] = c
            elif s in self.by_symbol:
                need.append(s)
        if need:
            with self.lock:
                for i in range(0, len(need), 50):
                    chunk = need[i:i + 50]
                    tokens = [self.by_symbol[s]["token"] for s in chunk]
                    resp = self._fetch(tokens)
                    if not resp or not resp.get("status"):
                        self._login()
                        resp = self._fetch(tokens)
                    if not resp or not resp.get("status"):
                        continue
                    tok2sym = {self.by_symbol[s]["token"]: s for s in chunk}
                    for d in resp["data"].get("fetched", []):
                        sym = tok2sym.get(str(d.get("symbolToken")))
                        if not sym:
                            continue
                        dep = d.get("depth") or {}
                        q = {
                            "symbol": sym, "name": self.by_symbol[sym]["name"], "ltp": float(d.get("ltp") or 0),
                            "buy": [(float(x["price"]), int(x["quantity"])) for x in dep.get("buy", [])],
                            "sell": [(float(x["price"]), int(x["quantity"])) for x in dep.get("sell", [])],
                            "tick": self.by_symbol[sym]["tick"],
                            "upper": float(d.get("upperCircuitLimit") or 0),
                            "lower": float(d.get("lowerCircuitLimit") or 0),
                            "close": float(d.get("close") or 0), "ts": time.time(),
                        }
                        self.cache[sym] = q
                        out[sym] = q
        for s in symbols:
            if s not in out and s in self.cache:
                out[s] = self.cache[s]  # stale fallback; engine checks age
        return out


_MOCK_STOCKS = {
    "RELIANCE-EQ": ("Reliance Industries", 1450.0), "TCS-EQ": ("Tata Consultancy", 3200.0),
    "HDFCBANK-EQ": ("HDFC Bank", 1000.0), "INFY-EQ": ("Infosys", 1500.0),
    "SBIN-EQ": ("State Bank of India", 850.0), "ITC-EQ": ("ITC", 420.0),
    "TATASTEEL-EQ": ("Tata Steel", 170.0), "YESBANK-EQ": ("Yes Bank", 21.0),
    "ANGELONE-EQ": ("Angel One", 2600.0), "IDEA-EQ": ("Vodafone Idea", 8.0),
}


class MockFeed(_Base):
    def __init__(self):
        self.master = [{"symbol": s, "name": n, "token": s, "tick": 0.05} for s, (n, _) in _MOCK_STOCKS.items()]
        self.px = {s: p for s, (_, p) in _MOCK_STOCKS.items()}
        self.close = dict(self.px)
        self.rng = random.Random(7)

    def quotes(self, symbols):
        out = {}
        for s in set(symbols):
            if s not in self.px:
                continue
            p = self.px[s] * (1 + self.rng.gauss(0, 0.0004))
            p = max(0.05, round(round(p / 0.05) * 0.05, 2))
            self.px[s] = p
            bid, ask, bq = [], [], []
            for i in range(5):
                bid.append((round(p - 0.05 * (i + 1), 2), self.rng.randint(200, 2500)))
                ask.append((round(p + 0.05 * (i + 1), 2), self.rng.randint(200, 2500)))
            c = self.close[s]
            out[s] = {
                "symbol": s, "name": _MOCK_STOCKS[s][0], "ltp": p, "buy": bid, "sell": ask, "tick": 0.05,
                "upper": round(c * 1.2, 2), "lower": round(c * 0.8, 2), "close": c, "ts": time.time(),
            }
        return out


def make_feed():
    return MockFeed() if C.MOCK else AngelFeed()
