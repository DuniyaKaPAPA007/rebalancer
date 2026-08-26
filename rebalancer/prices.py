"""Live prices -- Dhan ke alawa free sources se bhi.

Dhan ka Data API paid hai. Uske bina bhi app chalni chahiye, isliye ye
module do free fallback deta hai:

  yahoo -- Yahoo Finance chart API. Koi key nahi chahiye, har jagah se
           chal jaata hai. Volume bhi deta hai. Circuit limits NAHI deta.
  nse   -- NSE India ki apni site. Circuit limits BHI deti hai (yahi
           sabse kaam ki cheez hai), par cookie handshake chahiye aur
           NSE aksar automated calls block karti hai.

EK ZAROORI BAAT, SAAF-SAAF:
    Ye dono source ke prices **delayed** hote hain -- Yahoo ke liye NSE
    par aksar ~15 minute. LIMIT order us purane price ke aaspaas lagega,
    aur agar tab tak stock hil gaya toh order bhar hi nahi paayega (ya
    bura bhar jaayega).

    Isliye fallback price use hone par plan mein saaf warning aati hai
    aur limit buffer bada karne ko bola jaata hai. Ye Dhan ke live feed
    ka poora replacement NAHI hai -- iska matlab sirf itna hai ki bina
    paid subscription ke app kaam karti rahe.
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable

import requests

log = logging.getLogger(__name__)

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS"
NSE_HOME = "https://www.nseindia.com"
NSE_QUOTE = "https://www.nseindia.com/api/quote-equity?symbol={sym}"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class PriceError(RuntimeError):
    pass


@dataclass
class Quote:
    symbol: str
    ltp: float
    source: str
    prev_close: float = 0.0
    volume: int = 0
    upper: float = 0.0          # circuit limits -- sirf NSE deta hai
    lower: float = 0.0
    age_sec: float | None = None    # price kitna purana hai (pata ho toh)

    @property
    def has_circuit(self) -> bool:
        return self.upper > 0 and self.lower > 0


# ----------------------------------------------------------------------
def _num(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default          # NaN guard
    except (TypeError, ValueError):
        return default


def _yahoo_one(sess: requests.Session, sym: str, timeout: int) -> Quote | None:
    """Ek symbol ka Yahoo se price. Fail hone par None -- crash nahi."""
    try:
        r = sess.get(YAHOO_URL.format(sym=sym),
                     params={"interval": "1d", "range": "1d"},
                     timeout=timeout)
        if r.status_code != 200:
            log.debug("yahoo %s -> HTTP %s", sym, r.status_code)
            return None
        meta = ((r.json().get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        log.debug("yahoo %s fail: %s", sym, e)
        return None

    ltp = _num(meta.get("regularMarketPrice"))
    if ltp <= 0:
        return None
    ts = meta.get("regularMarketTime")
    age = (time.time() - float(ts)) if ts else None
    return Quote(
        symbol=sym, ltp=ltp, source="yahoo",
        prev_close=_num(meta.get("chartPreviousClose")
                        or meta.get("previousClose")),
        volume=int(_num(meta.get("regularMarketVolume"))),
        age_sec=age,
    )


def fetch_yahoo(symbols: Iterable[str], timeout: int = 10,
                workers: int = 8) -> dict[str, Quote]:
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    if not syms:
        return {}
    sess = requests.Session()
    sess.headers.update({"User-Agent": _UA, "Accept": "application/json"})
    out: dict[str, Quote] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(syms))) as ex:
        for q in ex.map(lambda s: _yahoo_one(sess, s, timeout), syms):
            if q:
                out[q.symbol] = q
    return out


# ----------------------------------------------------------------------
def _nse_session(timeout: int) -> requests.Session:
    """NSE bina cookie ke API call reject kar deti hai -- pehle homepage."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_HOME + "/get-quotes/equity",
    })
    s.get(NSE_HOME, timeout=timeout)
    return s


def _nse_one(sess: requests.Session, sym: str, timeout: int) -> Quote | None:
    try:
        r = sess.get(NSE_QUOTE.format(sym=requests.utils.quote(sym)),
                     timeout=timeout)
        if r.status_code != 200:
            return None
        d = r.json() or {}
    except (requests.RequestException, ValueError) as e:
        log.debug("nse %s fail: %s", sym, e)
        return None

    pi = d.get("priceInfo") or {}
    ltp = _num(pi.get("lastPrice"))
    if ltp <= 0:
        return None
    band = pi.get("pPriceBand")
    upper = _num(pi.get("upperCP")) or _num((pi.get("intraDayHighLow") or {}).get("max"))
    lower = _num(pi.get("lowerCP")) or _num((pi.get("intraDayHighLow") or {}).get("min"))
    if isinstance(band, str) and band.lower() in ("no band", "nan", ""):
        upper = lower = 0.0
    vol = _num(((d.get("securityWiseDP") or {}).get("quantityTraded")))
    return Quote(symbol=sym, ltp=ltp, source="nse",
                 prev_close=_num(pi.get("previousClose")),
                 volume=int(vol), upper=upper, lower=lower)


def fetch_nse(symbols: Iterable[str], timeout: int = 10) -> dict[str, Quote]:
    """NSE ek-ek karke, dheere. Wo parallel calls block kar deti hai."""
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    if not syms:
        return {}
    try:
        sess = _nse_session(timeout)
    except requests.RequestException as e:
        log.warning("NSE session nahi bana: %s", e)
        return {}
    out: dict[str, Quote] = {}
    for s in syms:
        q = _nse_one(sess, s, timeout)
        if q:
            out[s] = q
        time.sleep(0.35)                 # NSE ko gussa mat dilao
    return out


# ----------------------------------------------------------------------
PROVIDERS = {"yahoo": fetch_yahoo, "nse": fetch_nse}


@dataclass
class FetchResult:
    quotes: dict[str, Quote] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)   # {source: kitne}
    errors: list[str] = field(default_factory=list)
    max_age_sec: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.quotes)


def fetch(symbols: Iterable[str], order: Iterable[str] = ("yahoo", "nse"),
          timeout: int = 10) -> FetchResult:
    """Ek ke baad ek provider try karo. Jo naam mil gaye, unhe dobara
    nahi maangte -- sirf bache hue naam agle provider se."""
    want = [s.strip().upper() for s in symbols if s and s.strip()]
    res = FetchResult(missing=list(want))
    for name in order:
        if not res.missing:
            break
        fn = PROVIDERS.get(str(name).strip().lower())
        if not fn:
            res.errors.append(f"'{name}' naam ka koi price source nahi hai.")
            continue
        try:
            got = fn(res.missing, timeout=timeout)
        except Exception as e:                       # provider kabhi crash na kare
            res.errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if not got:
            res.errors.append(f"{name}: ek bhi price nahi mila.")
            continue
        res.quotes.update(got)
        res.sources[name] = res.sources.get(name, 0) + len(got)
        res.missing = [s for s in res.missing if s not in got]
    ages = [q.age_sec for q in res.quotes.values() if q.age_sec is not None]
    res.max_age_sec = max(ages) if ages else 0.0
    return res
