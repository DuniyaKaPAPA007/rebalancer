"""
Symbol -> securityId mapping.

Dhan orders SYMBOL nahi lete, `securityId` lete hain. Ye mapping scrip master
CSV se aati hai jo Dhan roz publish karta hai.

NOTE: Dhan ne is CSV ke column names beech-beech mein badle hain. Isliye
column detection tolerant rakhi hai -- naam badle toh code nahi tootega.
"""
from __future__ import annotations

import csv
import io
import time
from pathlib import Path

import requests

COMPACT_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
DETAILED_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

# Column naam ke possible variants -- pehla jo mile wahi use hoga
_SECURITY_ID_COLS = ["SECURITY_ID", "SEM_SMST_SECURITY_ID", "SECURITYID"]
_SYMBOL_COLS = ["UNDERLYING_SYMBOL", "SYMBOL_NAME", "SEM_TRADING_SYMBOL",
                "SM_SYMBOL_NAME", "TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL"]
_EXCH_COLS = ["EXCH_ID", "SEM_EXM_EXCH_ID", "EXCHANGE"]
_SEGMENT_COLS = ["SEGMENT", "SEM_SEGMENT"]
_INSTRUMENT_COLS = ["INSTRUMENT", "SEM_INSTRUMENT_NAME", "INSTRUMENT_TYPE"]
_SERIES_COLS = ["SERIES", "SEM_SERIES"]
_ISIN_COLS = ["ISIN", "SEM_ISIN", "ISIN_CODE", "ISIN_NO"]


class ScripMasterError(RuntimeError):
    pass


def _pick(header: list[str], candidates: list[str]) -> str | None:
    upper = {h.strip().upper(): h for h in header}
    for c in candidates:
        if c in upper:
            return upper[c]
    return None


def _download(url: str, dest: Path, max_age_hours: int = 12) -> str:
    """Roz-ka CSV cache karke rakho -- har run pe 10MB download bekaar hai."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (time.time() - dest.stat().st_mtime) < max_age_hours * 3600:
        return dest.read_text(encoding="utf-8", errors="replace")

    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    text = resp.content.decode("utf-8", errors="replace")
    dest.write_text(text, encoding="utf-8")
    return text


def load_equity_maps(cache_path: str | Path,
                     exchange: str = "NSE",
                     url: str = DETAILED_URL) -> tuple[dict[str, str], dict[str, str]]:
    """
    Returns ({SYMBOL: securityId}, {ISIN: securityId}) -- sirf NSE cash equity.

    ISIN map isliye chahiye kyunki trading symbol badalta rehta hai
    (merger, naam change, group change) par ISIN kabhi nahi badalta.
    Screener CSV mein ISIN ho toh usse match karna zyada bharosemand hai.

    Derivatives, indices etc. jaan-bujh ke filter kiye hain -- warna
    'NIFTY' jaise naam galat instrument pe match kar jaate hain.
    """
    text = _download(url, Path(cache_path))
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    if not header:
        raise ScripMasterError("Scrip master khaali aaya.")

    c_sec = _pick(header, _SECURITY_ID_COLS)
    c_sym = _pick(header, _SYMBOL_COLS)
    c_exch = _pick(header, _EXCH_COLS)
    c_seg = _pick(header, _SEGMENT_COLS)
    c_inst = _pick(header, _INSTRUMENT_COLS)
    c_series = _pick(header, _SERIES_COLS)
    c_isin = _pick(header, _ISIN_COLS)

    if not c_sec or not c_sym:
        raise ScripMasterError(
            f"Scrip master ke columns pehchaan nahi paaye. Mile: {header[:15]}\n"
            f"instruments.py mein _SECURITY_ID_COLS / _SYMBOL_COLS update karo.")

    out: dict[str, str] = {}
    by_isin: dict[str, str] = {}
    for row in reader:
        if c_exch and (row.get(c_exch) or "").strip().upper() != exchange:
            continue
        if c_seg:
            seg = (row.get(c_seg) or "").strip().upper()
            if seg not in ("E", "EQUITY", "NSE_EQ"):
                continue
        if c_inst:
            inst = (row.get(c_inst) or "").strip().upper()
            if inst and inst not in ("EQUITY", "ES", "EQ"):
                continue
        if c_series:
            series = (row.get(c_series) or "").strip().upper()
            # EQ = normal rolling settlement. BE/BZ = trade-to-trade,
            # in par intraday nahi chalta aur ye aksar surveillance mein hote
            # hain -- rebalancer se door hi rakho.
            if series and series != "EQ":
                continue

        sym = (row.get(c_sym) or "").strip().upper()
        sec = (row.get(c_sec) or "").strip()
        if sym and sec and sym not in out:
            out[sym] = sec
        if c_isin and sec:
            isin = (row.get(c_isin) or "").strip().upper()
            if isin and isin not in by_isin:
                by_isin[isin] = sec

    if not out:
        raise ScripMasterError(
            "Scrip master parse ho gaya par ek bhi NSE equity nahi mila -- "
            "filters ya column mapping check karo.")
    return out, by_isin


def load_equity_map(cache_path: str | Path, exchange: str = "NSE",
                    url: str = DETAILED_URL) -> dict[str, str]:
    """Sirf symbol map chahiye toh."""
    return load_equity_maps(cache_path, exchange, url)[0]


def resolve(targets, by_symbol: dict[str, str],
            by_isin: dict[str, str] | None = None
            ) -> tuple[dict[str, str], list[str], list[str]]:
    """
    targets: TargetName ki list (ya plain symbol strings).

    Pehle ISIN se match karta hai (symbol rename-proof), phir symbol se.
    Returns: ({symbol: securityId}, na-mile symbols, ISIN-se-mile symbols)
    """
    by_isin = by_isin or {}
    found: dict[str, str] = {}
    missing: list[str] = []
    via_isin: list[str] = []

    for t in targets:
        sym = (getattr(t, "symbol", t) or "").strip().upper()
        isin = (getattr(t, "isin", None) or "").strip().upper()

        if isin and isin in by_isin:
            found[sym] = by_isin[isin]
            if by_symbol.get(sym) != by_isin[isin]:
                via_isin.append(sym)     # symbol match nahi hua, ISIN ne bachaya
        elif sym in by_symbol:
            found[sym] = by_symbol[sym]
        else:
            missing.append(sym)
    return found, missing, via_isin
