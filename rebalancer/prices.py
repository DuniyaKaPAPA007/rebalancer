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


def _yahoo_one(sym: str, timeout: int) -> Quote | None:
    """Ek symbol ka Yahoo se price. Fail hone par None -- crash nahi.
    FIX: per-thread Session to avoid thread-safety issue."""
    sess = requests.Session()
    sess.headers.update({"User-Agent": _UA, "Accept": "application/json"})
    try:
        r = sess.get(YAHOO_URL.format(sym=sym),
                     params={"interval": "1d", "range": "1d"},
                     timeout=timeout)
        if r.status_code != 200:
            log.debug("yahoo %s -> HTTP %s", sym, r.status_code)
            return None
        try:
            meta = ((r.json().get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
        except (ValueError, KeyError, IndexError, AttributeError):
            return None
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        log.debug("yahoo %s fail: %s", sym, e)
        return None
    finally:
        sess.close()

    ltp = _num(meta.get("regularMarketPrice"))
    if ltp <= 0:
        return None
    ts = meta.get("regularMarketTime")
    try:
        age = (time.time() - float(ts)) if ts else None
        if age is not None and age < 0:
            age = 0  # clock skew
        # stale check: if age > 3600 (1hr) mark as stale but still return - caller decides
        # but we enforce max_age later
        if age is not None and age > 7200:  # >2hr very stale, likely market closed
            # still return but log
            log.debug("yahoo %s age %.0f sec stale", sym, age)
    except (TypeError, ValueError, OverflowError):
        age = None
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
    # fix: validate workers
    try:
        workers = int(workers)
        if workers <= 0:
            workers = 1
        if workers > 16:
            workers = 16
    except (TypeError, ValueError):
        workers = 4
    out: dict[str, Quote] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(syms))) as ex:
        for q in ex.map(lambda s: _yahoo_one(s, timeout), syms):
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
    try:
        r = s.get(NSE_HOME, timeout=timeout)
        if r.status_code not in (200, 301, 302):
            log.debug("NSE home HTTP %s", r.status_code)
    except requests.RequestException as e:
        log.warning("NSE home fetch fail: %s", e)
        # still return session - maybe quote works without cookie sometimes
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
    # FIX: don't fallback to intraday high/low as circuit - they are not limits
    upper = _num(pi.get("upperCP"))
    lower = _num(pi.get("lowerCP"))
    # only if both missing and band says no band, keep 0
    if isinstance(band, str) and band.lower() in ("no band", "nan", ""):
        upper = lower = 0.0
    # do NOT fallback to max/min - that causes false circuit triggers
    vol = _num(((d.get("securityWiseDP") or {}).get("quantityTraded")))
    # also try alternative field names for volume
    if vol == 0:
        vol = _num((d.get("securityWiseDP") or {}).get("tradedVolume") or d.get("tradedVolume"))
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
    for idx, s in enumerate(syms):
        q = _nse_one(sess, s, timeout)
        if q:
            out[s] = q
        # adaptive sleep with backoff on failure
        if q is None and idx < len(syms) - 1:
            time.sleep(0.6)
        else:
            time.sleep(0.5)                 # NSE ko gussa mat dilao - increased to 0.5
        # if we get 429, extra cooldown
        # note: _nse_one doesn't expose status, but we could detect rate limit via log
    sess.close()
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
          timeout: int = 10, max_age_sec: float = 900) -> FetchResult:
    """Ek ke baad ek provider try karo. Jo naam mil gaye, unhe dobara
    nahi maangte -- sirf bache hue naam agle provider se.
    FIX: stale quotes >max_age_sec (default 15min) drop karo.
    FIX: always try to enrich Yahoo quotes with NSE circuit even if Yahoo succeeded.
    """
    want = [s.strip().upper() for s in symbols if s and s.strip()]
    res = FetchResult(missing=list(want))
    # validate max_age
    try:
        max_age_sec = float(max_age_sec)
        if max_age_sec != max_age_sec or max_age_sec <= 0 or max_age_sec > 86400:
            max_age_sec = 900
    except (TypeError, ValueError):
        max_age_sec = 900
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
        # stale filter
        filtered = {}
        for sym, q in got.items():
            if q.age_sec is not None and q.age_sec > max_age_sec:
                res.errors.append(f"{sym} {name} price {q.age_sec/60:.0f}m purana - stale, ignore")
                res.missing.append(sym) if sym not in res.missing else None
                continue
            filtered[sym] = q
        if not filtered:
            res.errors.append(f"{name}: saare prices stale the (> {max_age_sec/60:.0f}m)")
            continue
        res.quotes.update(filtered)
        res.sources[name] = res.sources.get(name, 0) + len(filtered)
        res.missing = [s for s in res.missing if s not in filtered]
    # FIX: if Yahoo was first and succeeded, still try NSE for circuit enrichment for those symbols
    if "yahoo" in [str(x).lower() for x in order] and "nse" in [str(x).lower() for x in order]:
        yahoo_syms = [s for s, q in res.quotes.items() if q.source == "yahoo" and not q.has_circuit]
        if yahoo_syms:
            try:
                nse_extra = fetch_nse(yahoo_syms, timeout=min(timeout, 8))
                for sym, nq in nse_extra.items():
                    if sym in res.quotes and nq.has_circuit:
                        # merge circuit into existing yahoo quote
                        old = res.quotes[sym]
                        res.quotes[sym] = Quote(symbol=old.symbol, ltp=old.ltp, source=old.source,
                                                prev_close=old.prev_close or nq.prev_close,
                                                volume=old.volume or nq.volume,
                                                upper=nq.upper, lower=nq.lower,
                                                age_sec=old.age_sec)
                        log.debug("Enriched %s with NSE circuit", sym)
            except Exception as e:
                log.debug("NSE enrich fail: %s", e)
    ages = [q.age_sec for q in res.quotes.values() if q.age_sec is not None]
    res.max_age_sec = max(ages) if ages else 0.0
    return res
