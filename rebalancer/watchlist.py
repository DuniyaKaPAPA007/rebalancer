"""
Watchlist reader.

Teen format apne aap pehchaanta hai:

  1. SIMPLE      -- rank,symbol
  2. SCREENER    -- Trendlyne/Screener export jaisa, jismein 'NSE Code',
                    'ISIN', 'LTP', 'Market Cap' columns hote hain
  3. BACKTEST    -- Trendlyne ka "Backtest Execution Detail" export.
                    Ye poori history hoti hai (har period ke holdings).
                    Hum uska SABSE AAKHRI period uthaate hain -- wahi
                    strategy ki abhi ki list hai.

Koi bhi seedha daal do, haath se kuch banane ki zarurat nahi.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from .models import TargetName


class WatchlistError(RuntimeError):
    pass


# column naam ke variants (case/space-insensitive match hota hai)
_SYMBOL_COLS = ["nse code", "nsecode", "nse_code", "symbol", "ticker",
                "nse symbol", "trading symbol", "tradingsymbol"]
_RANK_COLS = ["rank", "sl no", "slno", "sr no", "s no", "#"]
_ISIN_COLS = ["isin", "isin code", "isin_code"]
_LTP_COLS = ["ltp", "last price", "close", "cmp", "current price"]
_NAME_COLS = ["stock", "company", "company name", "name", "stock name"]
_MCAP_COLS = ["market cap", "market cap (cr)", "mcap", "marketcap"]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").strip().lower())


def _pick(header: list[str], candidates: list[str]) -> str | None:
    lookup = {_norm(h): h for h in header}
    for c in candidates:
        if c in lookup:
            return lookup[c]
    # substring fallback: "Market Cap" vs "Market Cap (Rs Cr)"
    for c in candidates:
        for k, orig in lookup.items():
            if k.startswith(c):
                return orig
    return None


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in ("", "-", "--", "n/a", "na", "null", "none", "#n/a"):
        return None
    s = re.sub(r"[^0-9.\-]", "", s)
    try:
        return float(s) if s not in ("", "-", ".", "-.", ".-") else None
    except ValueError:
        return None


_PERIOD_RE = re.compile(r"^\d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}$")
# "HFCL (INE548A01028 / 500183/ HFCL)"  ->  name, isin, bse, nse
_STOCK_RE = re.compile(r"^(?P<name>.+?)\s*\(\s*(?P<isin>INE[0-9A-Z]+)\s*/"
                       r"\s*(?P<bse>[^/]*?)\s*/\s*(?P<nse>[^)]+?)\s*\)\s*$")


def _looks_like_backtest(path: Path) -> bool:
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            # read first few lines, not just first
            head = ""
            for _ in range(3):
                try:
                    head += f.readline().lower() + " "
                except:
                    break
        return "as on date" in head and "nav" in head and "period" in head or ("as on date" in head and "nav" in head and "holdings" in head)
    except Exception:
        return False


def parse_backtest_periods(path: str | Path) -> list[tuple[str, list[dict]]]:
    """Backtest export ke SAARE periods -- [(period, [stock dicts]), ...]."""
    p = Path(path)
    periods: list[tuple[str, list[dict]]] = []
    cur: list[dict] | None = None

    with p.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if not row or not any(c.strip() for c in row):
                continue
            c0 = (row[0] or "").strip()
            if _PERIOD_RE.match(c0):
                cur = []
                periods.append((c0, cur))
                continue
            if c0 or cur is None or len(row) < 2:
                continue                      # "Total Change" / header
            m = _STOCK_RE.match((row[1] or "").strip())
            if not m:
                continue
            try:
                end_px = float(row[3])
            except (IndexError, ValueError):
                end_px = None
            try:
                start_px = float(row[2])
            except (IndexError, ValueError):
                start_px = None
            cur.append({"nse": m.group("nse").strip().upper(),
                        "isin": m.group("isin").strip().upper(),
                        "name": m.group("name").strip(),
                        "ltp": end_px, "start": start_px})

    return [(d, st) for d, st in periods if st]


def _to_targets(stocks: list[dict]) -> list[TargetName]:
    seen: set[str] = set()
    out: list[TargetName] = []
    for st in stocks:
        if st["nse"] in seen:
            continue
        seen.add(st["nse"])
        out.append(TargetName(rank=len(out) + 1, symbol=st["nse"],
                              isin=st["isin"], name=st["name"],
                              ref_ltp=st["ltp"], market_cap_cr=None))
    return out


def read_backtest(path: str | Path) -> tuple[list[TargetName], str]:
    """Backtest export ka SABSE AAKHRI period = strategy ki abhi ki list.

    Returns (targets, period_string). Purani history rebalance ke liye
    bekaar hai -- paisa aaj ki list par lagta hai.
    """
    periods = parse_backtest_periods(path)
    if not periods:
        raise WatchlistError(
            f"{Path(path).name} backtest file lagti hai par ismein koi "
            f"stock nahi mila.")
    period, stocks = periods[-1]
    return _to_targets(stocks), period


def read(path: str | Path, rank_by: str = "file_order") -> list[TargetName]:
    """
    rank_by:
      "file_order" -- CSV mein jis order mein hain (default, sabse safe)
      "<column>"   -- us column ke hisaab se descending sort
                      (jaise "Momentum Score")
    """
    p = Path(path)
    if not p.exists():
        raise WatchlistError(f"Watchlist nahi mili: {p}")

    if _looks_like_backtest(p):               # format 3
        targets, _period = read_backtest(p)
        return targets

    with p.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise WatchlistError(f"{p} khaali hai.")

    header = list(rows[0].keys())
    c_sym = _pick(header, _SYMBOL_COLS)
    if not c_sym:
        raise WatchlistError(
            f"{p.name} mein symbol column nahi mila.\n"
            f"Mile columns: {header}\n"
            f"Chahiye: 'NSE Code' ya 'symbol'.")

    c_rank = _pick(header, _RANK_COLS)
    c_isin = _pick(header, _ISIN_COLS)
    c_ltp = _pick(header, _LTP_COLS)
    c_name = _pick(header, _NAME_COLS)
    c_mcap = _pick(header, _MCAP_COLS)

    out: list[TargetName] = []
    for i, r in enumerate(rows):
        sym = (r.get(c_sym) or "").strip().upper()
        if not sym:
            continue
        rank = int(_num(r.get(c_rank)) or (i + 1)) if c_rank else i + 1
        isin = (r.get(c_isin) or "").strip().upper() or None if c_isin else None
        out.append(TargetName(
            rank=rank,
            symbol=sym,
            isin=isin,
            name=(r.get(c_name) or "").strip() or None if c_name else None,
            ref_ltp=_num(r.get(c_ltp)) if c_ltp else None,
            market_cap_cr=_num(r.get(c_mcap)) if c_mcap else None,
        ))

    if not out:
        raise WatchlistError(f"{p.name} mein ek bhi symbol nahi mila.")

    # O(n) duplicate detection + ISIN duplicate check
    seen = {}
    isin_seen = {}
    dupes_set = set()
    for t in out:
        if t.symbol in seen:
            dupes_set.add(t.symbol)
        else:
            seen[t.symbol] = 1
        if t.isin:
            if t.isin in isin_seen and isin_seen[t.isin] != t.symbol:
                # same ISIN different symbol -> dual listing warning, not blocker but collect
                pass
            isin_seen[t.isin] = t.symbol
    if dupes_set:
        raise WatchlistError(f"Watchlist mein duplicate symbol: {sorted(dupes_set)}")

    # ---- re-rank ------------------------------------------------------
    if rank_by != "file_order":
        col = _pick(header, [_norm(rank_by)])
        if not col:
            raise WatchlistError(
                f"rank_by='{rank_by}' column CSV mein nahi hai. Columns: {header}")
        scores = {(r.get(c_sym) or "").strip().upper(): _num(r.get(col))
                  for r in rows}
        missing = [t.symbol for t in out if scores.get(t.symbol) is None]
        if missing:
            raise WatchlistError(
                f"'{rank_by}' column in symbols ke liye khaali hai: {missing}")
        out.sort(key=lambda t: -scores[t.symbol])
    else:
        out.sort(key=lambda t: t.rank)

    # rank ko 1..N par normalise kar do (Sl No mein gap ho sakte hain)
    return [TargetName(rank=i + 1, symbol=t.symbol, isin=t.isin, name=t.name,
                       ref_ltp=t.ref_ltp, market_cap_cr=t.market_cap_cr)
            for i, t in enumerate(out)]


# ----------------------------------------------------------------------
def sanity_checks(watchlist: list[TargetName], n_stocks: int,
                  use_overflow: bool, min_mcap_cr: float = 0.0) -> list[str]:
    """Plan banne se PEHLE ke warnings. Blocker nahi -- soch-samajh ke
    aage badhne ke liye."""
    w: list[str] = []

    auto = n_stocks <= 0            # auto mode -> list hi size decide karti hai

    if not auto and use_overflow and len(watchlist) == n_stocks:
        w.append(
            f"Watchlist mein theek {n_stocks} naam hain, {n_stocks + 1} nahi. "
            f"n+1 (overflow) slot khaali rahega -- bacha hua paisa cash mein "
            f"pada rahega. Screener se {n_stocks + 1} rows export karo.")

    if not auto and len(watchlist) > n_stocks + 1:
        extra = [t.symbol for t in watchlist[n_stocks + 1:]]
        w.append(f"{len(watchlist)} naam hain par sirf top {n_stocks + 1} use "
                 f"honge. Ignore: {', '.join(extra)}")

    if min_mcap_cr > 0:
        scope = watchlist if auto else watchlist[:n_stocks + 1]
        small = [f"{t.symbol} (Rs.{t.market_cap_cr:,.0f}cr)"
                 for t in scope
                 if t.market_cap_cr is not None and t.market_cap_cr < min_mcap_cr]
        if small:
            w.append(f"Chhote market cap: {', '.join(small)}. Inmein spread "
                     f"chauda hota hai, circuit aur ASM/GSM ka risk bhi -- "
                     f"slippage plan se zyada aa sakta hai.")
    return w


def stale_check(watchlist: list[TargetName], live_ltp: dict[str, float],
                tolerance: float = 0.05) -> list[str]:
    """CSV ka LTP vs live LTP. Bahut farak = CSV purani hai.

    Ye guard sasta hai aur mehnga bachaata hai: purani list par rebalance
    karna matlab kal ke momentum par aaj paisa lagana.
    """
    out = []
    for t in watchlist:
        live = live_ltp.get(t.symbol)
        if not t.ref_ltp or not live:
            continue
        d = abs(live - t.ref_ltp) / t.ref_ltp
        if d > tolerance:
            out.append(f"{t.symbol}: CSV mein Rs.{t.ref_ltp:,.2f}, live "
                       f"Rs.{live:,.2f} ({d * 100:+.1f}%)")
    if out:
        return [f"CSV ka LTP live se {tolerance * 100:.0f}%+ alag hai -- list "
                f"purani lag rahi hai. Fresh export karo.\n       "
                + "\n       ".join(out)]
    return []
