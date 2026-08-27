"""HTTP endpoints. Sara business logic rebalancer package mein hi hai --
ye sirf uske upar ek patli web layer hai."""
from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import rebalancer.config as cfgmod
from rebalancer.tz import IST
import rebalancer.watchlist as wlmod
from rebalancer import instruments, report
from rebalancer.dhan import DhanClient
from rebalancer.executor import Executor
from rebalancer.models import (CircuitInfo, Plan, PlannedOrder, Reason,
                                Side, Skipped)
from rebalancer import prices as pricemod
from rebalancer.planner import build_liquidation_plan, build_plan
from rebalancer.store import Store
from web import creds as credmod
from web.paper import PaperClient

log = logging.getLogger("web")
ROOT = Path(__file__).resolve().parents[1]
import threading
import re as _re_san
STATE_LOCK = threading.RLock()

# ---- server-side state (localhost, single user) -----------------------
STATE: dict = {"watchlist": None, "wl_name": None, "wl_warnings": [],
               "plan": None, "mode": "paper", "paper": None,
               "n_override": None, "overflow_override": None,
               "demo_capital": None,
               # deploy budget -- kitni capital stocks mein jaayegi.
               # None = config.yaml jo kehti hai wahi.
               "deploy_mode": None, "deploy_pct": None, "deploy_amount": None,
               # user ne khud mode chuna hai? Nahi toh credentials dekh kar
               # app khud Live par chali jaayegi.
               "mode_chosen_by_user": False,
               # autodetect ko latch mat karo -- transient failure par
               # dobara koshish karni hai, warna app poore session Demo
               # mein atki rehti hai aur chupchaap nakli paise par chalti hai
               "autodetect_settled": False, "autodetect_next_try": 0.0,
               "autodetect_msg": "",
               # credentials hain par Dhan se baat nahi ho pa rahi.
               # Aise mein NAKLI data dena sabse khatarnaak cheez hai --
               # user ko lagega asli balance hai. Isliye mana kar dete hain.
               "creds_broken": ""}


def _cfg(apply_overrides: bool = True) -> dict:
    cfg = cfgmod.load(str(ROOT / "config.yaml"))
    if apply_overrides:
        with STATE_LOCK:
            n_ov = STATE.get("n_override")
            of_ov = STATE.get("overflow_override")
            dm = STATE.get("deploy_mode")
            dp = STATE.get("deploy_pct")
            da = STATE.get("deploy_amount")
        if n_ov is not None:
            cfg["portfolio"]["n_stocks"] = n_ov
            cfg["portfolio"]["exit_rank_threshold"] = n_ov
        if of_ov is not None:
            cfg["portfolio"]["use_overflow_slot"] = of_ov
        if dm is not None:
            cfg["portfolio"]["deploy_mode"] = dm
            if dp is not None:
                cfg["portfolio"]["deploy_pct"] = dp
            if da is not None:
                cfg["portfolio"]["deploy_amount"] = da
    return cfg


def _deploy_state(cfg: dict) -> dict:
    """Abhi ka deploy setting -- UI ke liye ek hi jagah se."""
    pf = cfg["portfolio"]
    mode = str(pf.get("deploy_mode", "all")).strip().lower()
    if mode in ("percent", "percentage", "%"):
        mode = "pct"
    if mode in ("amt", "rupees", "inr", "fixed"):
        mode = "amount"
    raw = float(pf.get("deploy_pct", 1.0) or 0.0)
    pct = raw / 100.0 if raw > 1.0 else raw
    return {"mode": mode,
            "pct": round(min(max(pct, 0.0), 1.0) * 100, 4),
            "amount": float(pf.get("deploy_amount", 0) or 0.0),
            "from_config": STATE.get("deploy_mode") is None}


def _current_creds() -> tuple[str, str]:
    """Credentials -- pehle environment se, na mile toh creds.bat se.

    START-APP.bat `call creds.bat` karti hai toh env mein aa jaate hain.
    Par app kisi aur tarike se bhi chal sakti hai -- tab file se padho,
    warna app Demo mode mein atki rahegi jabki creds saamne padi hain.
    """
    d = cfgmod.load(str(ROOT / "config.yaml"))["dhan"]
    cid = os.environ.get(d["client_id_env"], "")
    tok = os.environ.get(d["access_token_env"], "")
    if cid and tok:
        return cid, tok
    f_cid, f_tok = credmod.read_saved(ROOT, d["client_id_env"],
                                      d["access_token_env"])
    return cid or f_cid, tok or f_tok


def _creds_present() -> bool:
    cid, tok = _current_creds()
    return bool(cid and tok)


def _autodetect_mode() -> None:
    """App khulte hi: credentials hain toh LIVE mode default.

    Pehle default hamesha Demo tha. Nateeja -- credentials set hone ke
    baad bhi plan nakli Rs.1 crore par banta tha aur asli Dhan balance
    chhua tak nahi jaata tha. Ab agar creds mil jaayein aur Dhan unhe
    maan le, app khud Live par aa jaati hai.

    Live ka matlab sirf itna hai ki DATA asli aayega. Order phir bhi
    rehearsal + "haan" + confirm ke bina nahi jaata.
    """
    import time as _t
    with STATE_LOCK:
        if STATE["mode_chosen_by_user"] or STATE["autodetect_settled"]:
            return
        # use monotonic for cooldown to avoid NTP jump
        now_m = _t.monotonic()
        next_try = STATE.get("_next_try_monotonic", 0)
        if now_m < next_try:
            return                       # abhi-abhi try kiya tha, ruko
        STATE["_next_try_monotonic"] = now_m + 20
        STATE["autodetect_next_try"] = _t.time() + 20    # keep legacy wall field too

    if not _creds_present():
        with STATE_LOCK:
            STATE["autodetect_settled"] = True             # ye badalne wala nahi
            STATE["creds_broken"] = ""
            STATE["autodetect_msg"] = ("Credentials nahi mile -- Demo mode. "
                                       "Connection tab se jodo.")
        return
    try:
        cfg = cfgmod.load(str(ROOT / "config.yaml"))
    except Exception as e:
        log.warning("autodetect config load fail: %s", e)
        return
    d = cfg["dhan"]
    cid, tok = _current_creds()
    res = credmod.verify(cid, tok, d["base_url"])
    if res.get("ok"):
        os.environ[d["client_id_env"]] = cid       # DhanClient ke liye
        os.environ[d["access_token_env"]] = tok
        with STATE_LOCK:
            STATE["mode"] = "live"
            STATE["autodetect_settled"] = True
            STATE["creds_broken"] = ""
            cash = res.get("cash")
            STATE["autodetect_msg"] = (
                f"Credentials verify ho gaye -- LIVE mode khud chalu kar diya. "
                f"NAV tumhare asli Dhan account se aayega"
                + (f" (free cash Rs.{cash:,.0f})." if cash is not None else "."))
        return

    bad = next((st for st in res.get("steps", []) if st["ok"] is False), None)
    bad_name = bad["name"] if bad else ""
    # Format/Token galat = pakka; ye khud theek nahi hoga.
    # Network/5xx = shayad temporary -- 20 sec baad dobara try karenge.
    permanent = bad_name in ("Format", "Token")
    with STATE_LOCK:
        STATE["autodetect_settled"] = permanent
        STATE["creds_broken"] = (bad["msg"] if bad else "Dhan se baat nahi ho payi.")
        STATE["autodetect_msg"] = (
            "Credentials mile par Dhan ne nahi maane -- Demo mode mein hoon. "
            + (bad["msg"] if bad else "Connection tab se check karo.")
            + ("" if permanent else
               " Ye temporary bhi ho sakta hai -- app har 20 second dobara "
               "koshish karti rahegi. Page refresh karke dekho."))


def _guard_broken() -> None:
    """Mode pakka karo, phir broken hone par NAKLI data mat do.

    Autodetect pehle sirf /api/health par chalta tha. Agar koi doosra
    endpoint pehle hit ho gaya (browser refresh ka order, ya seedha API
    call), toh app Demo mode mein reh jaati thi aur chupchaap nakli
    portfolio par plan bana deti thi. Isliye ab har data endpoint pehle
    mode settle karta hai.

    Credentials hain par Dhan tak nahi pahunch pa rahe -> NAKLI data
    dena mana hai. User ko lagega ye asli balance hai aur wo us par
    trade kar dega. Saaf mana karna hi safe hai.

    User jaan-bujh kar Demo chun le toh alag baat -- tab chalne dete hain.
    """
    _autodetect_mode()
    with STATE_LOCK:
        broken = STATE.get("creds_broken")
        chosen = STATE.get("mode_chosen_by_user")
    if broken and not chosen:
        # sanitize broken msg to avoid leaking raw http
        safe_msg = _re_san.sub(r'[^\x20-\x7E\n]', '', str(broken))[:300]
        raise HTTPException(
            502,
            "Dhan se connect nahi ho pa raha, isliye ruk gaye.\n\n"
            f"{safe_msg}\n\n"
            "Yahan NAKLI portfolio dikha kar tumhe dhoka dena sabse "
            "khatarnaak hota -- tum us par asli trade kar dete. Isliye "
            "kuch nahi dikha rahe.\n\n"
            "Kya karo: (1) '1 - Connect' tab se 'Check karo' dabao, "
            "(2) Dhan ki taraf se outage ho toh do minute baad try karo, "
            "(3) sirf app dekhni hai toh upar 'Demo' khud chun lo -- "
            "tab saaf pata rahega ki paisa nakli hai.")


def _client(cfg: dict):
    """mode='live' hone par hi asli Dhan. Warna paper."""
    with STATE_LOCK:
        mode = STATE.get("mode")
        paper = STATE.get("paper")
        demo_cap = STATE.get("demo_capital")
    if mode == "live":
        if not _creds_present():
            raise HTTPException(400, "Dhan credentials set nahi hain. "
                                     "creds.bat chalao ya Demo mode use karo.")
        return DhanClient(*cfgmod.credentials(cfg), base_url=cfg["dhan"]["base_url"])
    # Demo broker ek hi rehta hai taaki fills ke baad portfolio sach mein badle
    with STATE_LOCK:
        if STATE.get("paper") is None:
            STATE["paper"] = PaperClient(STATE.get("demo_capital"))
            return STATE["paper"]
        return STATE["paper"]


def _get_prices(c, security_ids: dict, cfg: dict) -> tuple[dict, dict, dict]:
    """Prices lao: pehle Dhan, na mile toh free sources.

    Dhan ka Data API paid hai. Uske bina bhi app chalni chahiye, isliye
    Yahoo/NSE fallback hai. Fallback prices DELAYED hote hain -- caller
    ko `info` mein saaf pata chal jaata hai taaki plan mein warning aa sake.

    Returns (circuit_map, ltp_map, info)
    """
    ids = sorted(set(security_ids.values()))
    seg = cfg["dhan"]["exchange_segment"]
    symbol_of = {sid: sym for sym, sid in security_ids.items()}
    info = {"source": None, "dhan_error": "", "fallback": {},
            "fallback_errors": [], "age_min": 0.0, "no_circuit": [],
            "missing": []}

    circuit, ltp = {}, {}
    try:
        circuit = c.quotes(ids, symbol_of, segment=seg)
    except Exception as e:
        info["dhan_error"] = str(e)
        log.warning("dhan quotes fail: %s", e)
    if circuit:
        ltp = {s: (circuit[s].ltp if s in circuit else 0.0) for s in security_ids}
    else:
        try:
            by_id = c.ltp(ids, segment=seg)
            ltp = {s: by_id.get(sid, 0.0) for s, sid in security_ids.items()}
        except Exception as e:
            info["dhan_error"] = info["dhan_error"] or str(e)
            log.warning("dhan ltp fail: %s", e)

    have = {s for s, v in ltp.items() if v > 0}
    if have:
        info["source"] = "dhan"
    need = [s for s in security_ids if s not in have]

    order = list(cfg.get("prices", {}).get("fallback") or [])
    if need and order:
        log.info("Dhan se %d naam ke price nahi mile -- %s se try kar rahe hain",
                 len(need), ", ".join(order))
        res = pricemod.fetch(need, order=order)
        info["fallback"] = dict(res.sources)
        info["fallback_errors"] = list(res.errors)
        info["missing"] = list(res.missing)
        info["age_min"] = round(res.max_age_sec / 60.0, 1)
        for sym, q in res.quotes.items():
            ltp[sym] = q.ltp
            circuit[sym] = CircuitInfo(
                symbol=sym, ltp=q.ltp, upper=q.upper, lower=q.lower,
                prev_close=q.prev_close or q.ltp, volume=q.volume)
            if not q.has_circuit:
                info["no_circuit"].append(sym)
        if res.quotes:
            used = list(res.sources)          # jo source sach mein chala
            info["source"] = ("mixed" if have or len(used) > 1
                              else (used[0] if used else None))
    else:
        info["missing"] = need
    return circuit, ltp, info


def _price_warnings(info: dict, cfg: dict) -> list[str]:
    """Fallback price use hua toh user ko SAAF pata chalna chahiye."""
    w: list[str] = []
    if not info.get("fallback"):
        return w
    src = ", ".join(f"{k} ({v} naam)" for k, v in info["fallback"].items())
    w.append(
        f"!! PRICES DHAN SE NAHI AAYE -- {src} se liye gaye hain. "
        f"Ye prices DELAYED hote hain (aksar ~15 minute). LIMIT order "
        f"purane price ke aaspaas lagega; stock hil gaya toh bhar nahi "
        f"paayega. Aaj hi chalana ho toh execution mein limit_buffer_pct "
        f"bada kar lo, ya MARKET order use karo."
        + (f" Dhan ne kaha: {info['dhan_error'][:120]}"
           if info.get("dhan_error") else ""))
    age = info.get("age_min") or 0
    limit = float(cfg.get("prices", {}).get("stale_warn_min", 20))
    if age > limit:
        w.append(f"!! Fallback price {age:.0f} MINUTE purana hai (limit "
                 f"{limit:.0f}). Market band hai ya feed atka hai -- iss par "
                 f"order mat bhejo.")
    if info.get("no_circuit"):
        n = info["no_circuit"]
        w.append(
            f"{len(n)} naam ke circuit limits nahi mile (Yahoo circuit data "
            f"nahi deta): {', '.join(n[:8])}{'...' if len(n) > 8 else ''}. "
            f"Circuit-lock ka check in par nahi lagega.")
    for e in info.get("fallback_errors", [])[:3]:
        w.append(f"Price source: {e}")
    return w


def _plan_dict(plan: Plan, cfg: dict) -> dict:
    from rebalancer.planner import estimate_costs
    buy_v = sum(o.qty * o.ref_price for o in plan.orders if o.side is Side.BUY)
    sell_v = sum(o.qty * o.ref_price for o in plan.orders if o.side is Side.SELL)
    n_sell = len({o.symbol for o in plan.orders if o.side is Side.SELL})
    _e = estimate_costs(buy_v, sell_v, n_sell, cfg)
    est = asdict(_e)
    est["total"] = _e.total
    from rebalancer.planner import annualised_cost
    est["annual"] = annualised_cost(_e.total, plan.nav, cfg)
    return {
        "run_id": plan.run_id, "nav": plan.nav, "free_cash": plan.free_cash,
        "is_liquidation": getattr(plan, "is_liquidation", False),
        "age_sec": getattr(plan, "age_sec", 0.0),
        "max_age_min": float(cfg["execution"].get("max_plan_age_min", 15) or 0),
        "slice_value": plan.slice_value,
        "target_equity": getattr(plan, "target_equity", 0.0),
        "cash_after": getattr(plan, "cash_after", 0.0),
        "deploy_label": getattr(plan, "deploy_label", "poori capital"),
        "turnover_pct": getattr(plan, "turnover_pct", 0.0),
        "churn_pct": getattr(plan, "churn_pct", 0.0),
        "warnings": plan.warnings, "blockers": plan.blockers,
        "skipped": [asdict(s) for s in plan.skipped],
        "costs": est,
        "orders": [{"symbol": o.symbol, "security_id": o.security_id,
                    "side": o.side.value, "qty": o.qty,
                    "ref_price": o.ref_price, "limit_price": o.limit_price,
                    "reason": o.reason.value, "note": o.note,
                    "value": o.qty * o.ref_price} for o in plan.orders],
        "text": report.render(plan, cfg),
    }


class ModeIn(BaseModel):
    mode: str


class CredsIn(BaseModel):
    client_id: str = ""
    access_token: str = ""
    save: bool = False


class CapitalIn(BaseModel):
    capital: float


class DeployIn(BaseModel):
    mode: str = "all"            # all | pct | amount
    pct: float | None = None     # 0-100
    amount: float | None = None  # rupees


class SlotsIn(BaseModel):
    mode: str = "auto"           # auto | fixed
    n: int | None = None
    use_overflow: bool | None = None


class ExecIn(BaseModel):
    mode: str = "dry"            # dry | real
    confirm: str = ""


class FundFlowIn(BaseModel):
    amount: float
    flow_type: str = "WITHDRAW"  # DEPOSIT / WITHDRAW
    note: str = ""


def register_routes(app: FastAPI) -> None:

    # ---------------------------------------------------- health
    @app.get("/api/health")
    def health():
        _autodetect_mode()
        cfg = _cfg()
        out = {"mode": STATE["mode"], "creds": _creds_present(),
               "watchlist_loaded": STATE["watchlist"] is not None,
               "wl_name": STATE["wl_name"],
               "wl_count": len(STATE["watchlist"] or []),
               "has_plan": STATE["plan"] is not None,
               "broker_ok": None, "broker_msg": "",
               "autodetect_msg": STATE.get("autodetect_msg", ""),
               "creds_broken": STATE.get("creds_broken", ""),
               "mode_chosen_by_user": STATE["mode_chosen_by_user"]}
        if STATE["mode"] == "paper":
            out["broker_ok"] = True
            out["broker_msg"] = "Demo mode -- nakli portfolio, koi order nahi jaata"
        elif not _creds_present():
            out["broker_ok"] = False
            out["broker_msg"] = "Credentials nahi mile"
        else:
            try:
                c = DhanClient(*cfgmod.credentials(cfg),
                               base_url=cfg["dhan"]["base_url"])
                cash = c.available_cash()
                out["broker_ok"] = True
                out["broker_msg"] = f"Dhan connected -- Rs.{cash:,.0f} free cash"
            except Exception as e:
                out["broker_ok"] = False
                out["broker_msg"] = f"Connect nahi hua: {e}"
        return out

    @app.post("/api/mode")
    def set_mode(body: ModeIn):
        if body.mode not in ("paper", "live"):
            raise HTTPException(400, "mode paper ya live hona chahiye")
        if body.mode == "live" and not _creds_present():
            raise HTTPException(400, "Live mode ke liye Dhan credentials chahiye. "
                                     "creds.bat chala kar app dobara kholo.")
        with STATE_LOCK:
            STATE["mode"] = body.mode
            STATE["mode_chosen_by_user"] = True
            STATE["plan"] = None
            STATE["paper"] = None          # demo portfolio fresh
            # also reset autodetect latch so health etc reflects choice
            # keep settled true to avoid re-autodetect after explicit choice
            STATE["autodetect_settled"] = True
            STATE["creds_broken"] = ""
            mode = STATE["mode"]
        return {"mode": mode}

    # ---------------------------------------------------- config
    @app.get("/api/config")
    def get_config():
        cfg = _cfg()
        return {"portfolio": cfg["portfolio"], "risk": cfg["risk"],
                "costs": cfg["costs"], "execution": cfg["execution"]}

    # ---------------------------------------------------- watchlist upload
    @app.post("/api/watchlist")
    async def upload_watchlist(file: UploadFile = File(...)):
        # size via content-length header early + reading limit
        # Use streaming read with limit to avoid OOM
        raw = await file.read()
        if len(raw) > 5_000_000:
            raise HTTPException(400, "File bahut badi hai (5 MB se zyada).")
        # also enforce via content length if provided
        if file.size and file.size > 5_000_000:
            raise HTTPException(400, "File bahut badi hai (5 MB se zyada).")
        cfg = _cfg()
        # use unique temp file per request
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix="rebal_upload_")
        tmp = Path(tmp_path)
        try:
            os.write(tmp_fd, raw)
            os.close(tmp_fd)
            tmp_fd = -1
        finally:
            if tmp_fd != -1:
                try:
                    os.close(tmp_fd)
                except:
                    pass
        fmt, period, prev_period = "screener", None, None
        try:
            if wlmod._looks_like_backtest(tmp):
                fmt = "backtest"
                periods = wlmod.parse_backtest_periods(tmp)
                if not periods:
                    raise wlmod.WatchlistError(
                        "Backtest file lagti hai par ismein koi stock nahi mila.")
                period, last = periods[-1]
                wl = wlmod._to_targets(last)
                # Demo mode: pichhle period ko "abhi ka portfolio" bana do,
                # taaki asli rebalance dikhe (kuch rakho, kuch becho, kuch lo)
                if STATE["mode"] == "paper" and len(periods) >= 2:
                    prev_period, prev = periods[-2]
                    STATE["paper"] = PaperClient(STATE.get("demo_capital"))
                    STATE["paper"].seed_from(prev)
            else:
                wl = wlmod.read(tmp, rank_by=cfg["portfolio"].get("rank_by", "file_order"))
        except wlmod.WatchlistError as e:
            try:
                tmp.unlink(missing_ok=True)
            except:
                pass
            raise HTTPException(400, f"CSV padha nahi ja saka: {e}")
        except Exception as e:
            try:
                tmp.unlink(missing_ok=True)
            except:
                pass
            # sanitize internal error
            log.error("watchlist parse fail: %s", e, exc_info=True)
            raise HTTPException(400, "CSV mein dikkat: file format check karo (Trendlyne export?)")
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except:
                pass
        if not wl:
            raise HTTPException(400, "CSV mein koi stock nahi mila.")

        try:
            n_cfg = cfg["portfolio"]["n_stocks"]
            n_for_checks = 0 if str(n_cfg).strip().lower() == "auto" else int(float(str(n_cfg).strip()))
        except (ValueError, TypeError):
            n_for_checks = 0
        try:
            cap_cr = float(cfg["risk"].get("min_market_cap_cr", 0) or 0)
        except (TypeError, ValueError):
            cap_cr = 0
        warns = wlmod.sanity_checks(
            wl, n_for_checks,
            bool(cfg["portfolio"]["use_overflow_slot"]),
            cap_cr)

        # sanitize filename - strip path, limit length, remove html
        raw_name = file.filename or "upload.csv"
        safe_name = Path(raw_name).name[:100]
        safe_name = _re_san.sub(r'[<>&\"\'`]', '', safe_name)
        with STATE_LOCK:
            STATE.update(watchlist=wl, wl_name=safe_name,
                         wl_warnings=warns, plan=None)
            n_override_snapshot = STATE.get("n_override")
        from rebalancer.planner import resolve_n
        n = resolve_n(cfg["portfolio"], len(wl))
        use_of = bool(cfg["portfolio"]["use_overflow_slot"])
        return {"count": len(wl), "filename": safe_name, "warnings": warns,
                "n_stocks": n, "format": fmt, "period": period,
                "prev_period": prev_period,
                "auto": str(n_cfg).strip().lower() == "auto" if isinstance(n_cfg, str) else False,
                "use_overflow": use_of,
                "n_override": n_override_snapshot,
                "stocks": [{"rank": t.rank, "symbol": t.symbol,
                            "name": t.name, "ltp": t.ref_ltp,
                            "mcap_cr": t.market_cap_cr, "isin": t.isin,
                            "in_top": t.rank <= n,
                            "overflow": use_of and t.rank == n + 1} for t in wl]}

    # ---------------------------------------------------- holdings
    @app.get("/api/holdings")
    def holdings():
        _guard_broken()
        cfg = _cfg()
        c = _client(cfg)
        try:
            hs = c.holdings()
        except Exception as e:
            body = getattr(e, "body", None)
            raise HTTPException(502, f"Dhan se holdings nahi aaye.\n\n{e}"
                                + (f"\n\nDhan ka jawaab: {body}" if body else ""))
        sec_ids = [h.security_id for h in hs]
        try:
            live = c.ltp(sec_ids, segment=cfg["dhan"]["exchange_segment"])
        except Exception:
            live = {}
        rows, total = [], 0.0
        for h in hs:
            px = live.get(h.security_id) or h.avg_price
            val = h.total_qty * px
            total += val
            rows.append({"symbol": h.symbol, "qty": h.total_qty,
                         "available": h.available_qty, "avg": h.avg_price,
                         "ltp": px, "value": val,
                         "pnl": (px - h.avg_price) * h.total_qty,
                         "pnl_pct": ((px / h.avg_price - 1) * 100
                                     if h.avg_price else 0.0)})
        for r in rows:
            r["weight"] = (r["value"] / total * 100) if total else 0.0
        rows.sort(key=lambda r: -r["value"])
        paper = isinstance(c, PaperClient)
        # Zerodha-like: record NAV history (real, not fake)
        try:
            cash_val = c.available_cash()
        except:
            cash_val = 0.0
        nav_val = total + cash_val
        # Real NAV only for live; paper NAV marked as paper but still recorded for demo tracking
        try:
            db_nav = Store(str(ROOT / cfg["paths"]["db"]))
            db_nav.record_nav(nav_val, total, cash_val, source="paper" if paper else "Dhan")
        except Exception as e:
            log.debug(f"nav record fail: {e}")
        # fund flows and realized
        try:
            db2 = Store(str(ROOT / cfg["paths"]["db"]))
            flows = db2.get_fund_flows(5)
            realized = db2.get_realized_pnl()
        except:
            flows = []
            realized = 0.0
        return {"holdings": rows, "total": total,
                "cash": cash_val, "mode": STATE["mode"],
                "nav": nav_val,
                "realized_pnl": realized,
                "fund_flows": flows,
                "source": "Demo -- nakli portfolio (PAPER - not real Dhan)" if paper
                          else "Dhan API LIVE - real holdings + real cash (no fake)",
                "is_demo": paper,
                "is_real": not paper,
                "demo_capital": getattr(c, "capital", None) if paper else None}

    # ---------------------------------------------------- Zerodha-like NAV + EMA + fund flows
    @app.get("/api/portfolio/nav_history")
    def nav_history(limit: int = 90, timeframe: str = "daily"):
        _guard_broken()
        cfg = _cfg()
        db = Store(str(ROOT / cfg["paths"]["db"]))
        # timeframe: daily / weekly / monthly / yearly
        if timeframe not in ("daily","weekly","monthly","yearly"):
            timeframe = "daily"
        rows = db.get_nav_history(limit=limit, timeframe=timeframe)
        # also return latest NAV for convenience
        return {"timeframe": timeframe, "count": len(rows), "history": rows}

    @app.get("/api/portfolio/ema")
    def nav_ema(period: int = 10, timeframe: str = "daily", limit: int = 90):
        _guard_broken()
        if period < 2 or period > 200:
            raise HTTPException(400, "EMA period 2-200 hona chahiye (10,20,50 common)")
        if timeframe not in ("daily","weekly","monthly","yearly"):
            timeframe = "daily"
        cfg = _cfg()
        db = Store(str(ROOT / cfg["paths"]["db"]))
        rows = db.get_nav_history(limit=limit, timeframe=timeframe)
        if not rows:
            return {"period": period, "timeframe": timeframe, "ema": [], "history": []}
        navs = [r["nav"] for r in rows]
        emas = db.calc_ema(navs, period)
        # combine
        out = []
        for r, e in zip(rows, emas):
            out.append({"date": r["captured_at"][:10], "nav": r["nav"], "ema": round(e,2) if e else None, "holdings": r["holdings_value"], "cash": r["free_cash"]})
        return {"period": period, "timeframe": timeframe, "count": len(out), "data": out}

    @app.post("/api/portfolio/fund_flow")
    def add_fund_flow(body: FundFlowIn):
        _guard_broken()
        # In LIVE mode, withdraw is real Dhan withdraw - we only log it for NAV tracking (Dhan cash will anyway reduce)
        # In PAPER mode, we adjust PaperClient cash as well for demo fidelity
        amt = float(body.amount)
        if amt == 0:
            raise HTTPException(400, "Amount 0 nahi ho sakta")
        # normalize: WITHDRAW should be negative stored, DEPOSIT positive
        ft = body.flow_type.strip().upper()
        if ft not in ("DEPOSIT","WITHDRAW"):
            raise HTTPException(400, "flow_type DEPOSIT ya WITHDRAW hona chahiye")
        # if user says WITHDRAW 50000, store -50000
        store_amt = abs(amt) if ft=="DEPOSIT" else -abs(amt)
        cfg = _cfg()
        db = Store(str(ROOT / cfg["paths"]["db"]))
        db.add_fund_flow(store_amt, ft, note=body.note)
        # If paper, adjust demo cash to mimic real withdraw
        if STATE.get("mode")=="paper" and STATE.get("paper"):
            try:
                paper = STATE["paper"]
                # paper._cash is available cash
                if hasattr(paper, "_cash"):
                    with getattr(paper, "_lock", STATE_LOCK):
                        paper._cash = max(0, paper._cash + store_amt)  # withdraw reduces
                        paper.capital = max(0, paper.capital + store_amt)
            except Exception as e:
                log.debug(f"paper cash adjust fail: {e}")
        return {"ok": True, "amount": store_amt, "flow_type": ft, "note": body.note}

    @app.get("/api/portfolio/fund_flows")
    def list_fund_flows(limit: int = 50):
        _guard_broken()
        cfg = _cfg()
        db = Store(str(ROOT / cfg["paths"]["db"]))
        rows = db.get_fund_flows(limit=limit)
        return {"count": len(rows), "flows": rows}

    @app.get("/api/portfolio/summary")
    def portfolio_summary():
        _guard_broken()
        cfg = _cfg()
        c = _client(cfg)
        try:
            hs = c.holdings()
            cash = c.available_cash()
        except Exception as e:
            raise HTTPException(502, f"Dhan se portfolio nahi aaya: {e}")
        # live prices
        sec_ids = [h.security_id for h in hs]
        try:
            live = c.ltp(sec_ids, segment=cfg["dhan"]["exchange_segment"]) if hs else {}
        except:
            live = {}
        total = 0.0
        rows=[]
        for h in hs:
            px = live.get(h.security_id) or h.avg_price
            val = h.total_qty * px
            total += val
            rows.append({"symbol": h.symbol, "qty": h.total_qty, "available": h.available_qty, "avg": h.avg_price, "ltp": px, "value": val, "pnl": (px - h.avg_price)*h.total_qty, "pnl_pct": ((px/h.avg_price -1)*100 if h.avg_price else 0)})
        nav = total + cash
        db = Store(str(ROOT / cfg["paths"]["db"]))
        try:
            flows = db.get_fund_flows(20)
            # total deposits/withdraws
            total_deposit = sum(r["amount"] for r in flows if r["amount"]>0)
            total_withdraw = sum(-r["amount"] for r in flows if r["amount"]<0)
        except:
            flows=[]; total_deposit=0; total_withdraw=0
        # NAV history for sparkline
        try:
            hist = db.get_nav_history(limit=30, timeframe="daily")
        except:
            hist=[]
        paper = isinstance(c, PaperClient)
        return {
            "nav": nav, "holdings_value": total, "free_cash": cash,
            "holdings": sorted(rows, key=lambda x: -x["value"]),
            "holdings_count": len(rows),
            "realized_pnl": db.get_realized_pnl() if not paper else 0,
            "fund_flows": flows,
            "total_deposit": total_deposit,
            "total_withdraw": total_withdraw,
            "nav_history": hist[-30:],
            "is_real": not paper,
            "source": "Dhan LIVE - 100% real (no fake)" if not paper else "PAPER - demo fake",
            "mode": STATE.get("mode")
        }

    # ---------------------------------------------------- plan
    @app.post("/api/plan")
    def make_plan():
        if not STATE["watchlist"]:
            raise HTTPException(400, "Pehle watchlist CSV upload karo.")
        _guard_broken()
        cfg = _cfg()
        wl = STATE["watchlist"]
        c = _client(cfg)

        if isinstance(c, PaperClient):
            c.set_prices({t.symbol: t.ref_ltp for t in wl if t.ref_ltp})

        try:
            hs = c.holdings()
            cash = c.available_cash()
        except Exception as e:
            body = getattr(e, "body", None)
            raise HTTPException(502,
                f"Dhan se portfolio/cash nahi aaya.\n\n{e}"
                + (f"\n\nDhan ka jawaab: {body}" if body else "")
                + "\n\nAksar wajah: (a) token expire, (b) Dhan ki taraf se "
                  "temporary outage, (c) Data API subscription band. "
                  "Connection tab se 'Check karo' dabao -- wahan pata chal "
                  "jaayega ki kaunsa endpoint fail ho raha hai.")
        security_ids = {h.symbol: h.security_id for h in hs}
        via_isin, missing, id_clash = [], [], []

        if isinstance(c, PaperClient):
            for i, t in enumerate(wl, 500):
                security_ids.setdefault(t.symbol, f"SEC{i}")
        else:
            try:
                sym_map, isin_map = instruments.load_equity_maps(
                    str(ROOT / cfg["paths"]["instruments_cache"]))
            except Exception as e:
                log.error("scrip master load fail: %s", e)
                raise HTTPException(
                    503,
                    "NSE scrip master download nahi hua -- iske bina symbol se "
                    "securityId nahi nikal sakte. Internet check karo aur dobara "
                    f"try karo. ({type(e).__name__})")
            found, missing, via_isin = instruments.resolve(wl, sym_map, isin_map)
            # Jo scrip PEHLE SE hold hai, uska securityId Dhan ka hi rahega.
            # Dhan tumhari apni position ka ID bhej raha hai -- wahi sach hai.
            # Scrip master sirf naye naamon ke liye. (Agar dono alag nikle
            # toh galat instrument par SELL chala jaata -- wo sabse mehnga
            # bug hota.)
            id_clash = [f"{s_} (Dhan {security_ids[s_]} vs master {sid})"
                        for s_, sid in found.items()
                        if s_ in security_ids and security_ids[s_] != sid]
            for s_, sid in found.items():
                security_ids.setdefault(s_, sid)

        symbol_of = {sid: s for s, sid in security_ids.items()}
        if isinstance(c, PaperClient):
            c.register(security_ids)

        circuit, ltp, pinfo = _get_prices(c, security_ids, cfg)
        if not any(v > 0 for v in ltp.values()):
            raise HTTPException(
                502,
                "Kahin se bhi live prices nahi mile -- bina price ke plan "
                "banana khatarnaak hai, isliye ruk gaye.\n\n"
                f"Dhan: {pinfo.get('dhan_error') or 'khaali jawaab'}\n"
                + "\n".join(pinfo.get("fallback_errors") or
                             ["fallback config mein band hai"])
                + "\n\nAksar wajah: market band hai, internet nahi hai, ya "
                  "config mein prices.fallback khaali hai.")

        pre = list(STATE["wl_warnings"])
        if not isinstance(c, PaperClient) and id_clash:
            pre.append("Scrip master aur Dhan ka securityId alag hai: "
                       + ", ".join(id_clash)
                       + ". Dhan wala use ho raha hai (jo hold hai wahi sach hai).")
        if missing:
            pre.append("Ye symbols NSE master mein nahi mile: " + ", ".join(missing))
        if via_isin:
            pre.append("ISIN se match hue (symbol badla lagta hai): " + ", ".join(via_isin))
        pre += _price_warnings(pinfo, cfg)
        if pinfo.get("missing"):
            pre.append("In naam ka price kahin se nahi mila: "
                       + ", ".join(pinfo["missing"]))
        pre += wlmod.stale_check(wl, ltp,
                                 float(cfg["risk"].get("stale_ltp_tolerance", 0.05)))

        run_id = datetime.now(IST).strftime("R%Y%m%d-%H%M%S")
        plan = build_plan(run_id=run_id, holdings=hs, free_cash=cash,
                          watchlist=wl, ltp=ltp, security_ids=security_ids,
                          cfg=cfg, circuit=circuit)
        plan.warnings = pre + plan.warnings
        # ---- minimum capital check ----
        try:
            from rebalancer.planner import min_capital_for_targets
            targets_for_min = [t.symbol for t in sorted(wl, key=lambda x: x.rank)[:resolve_n(cfg["portfolio"], len(wl))]]
            min_req = min_capital_for_targets(targets_for_min, ltp, cfg)
            # compare allocated investable vs required
            # plan.target_equity is investable after deploy+reserve; for min we consider NAV
            # Warn if allocated less than min
            allocated = plan.target_equity
            need = min_req["min_investable"]
            if allocated < need - 1:
                plan.warnings.append(
                    f"CAPITAL KAM HAI: Top {len(targets_for_min)} stocks me har ek me kam se kam 1 valid order (₹{min_req['min_trade_val']:.0f}) ke liye "
                    f"kam se kam slice ₹{min_req['min_slice']:,.0f} chahiye → total ₹{need:,.0f} stocks me + reserve. "
                    f"Aapne sirf ₹{allocated:,.0f} allocate kiya (NAV ₹{plan.nav:,.0f} me se). Isiliye {len(targets_for_min) - len([o for o in plan.orders if o.side==Side.BUY])} stocks me paisa nahi laga (8/10 jaisa). "
                    f"Minimum NAV chahiye ₹{min_req['min_nav']:,.0f}. Deploy % badhao ya capital badhao. Details: " +
                    ", ".join([f"{p['symbol']} ₹{p['price']:.0f}×{p['min_qty']} = ₹{p['min_value']:.0f}" for p in min_req["per_stock"][:3]]) + ("..." if len(min_req["per_stock"])>3 else "")
                )
            # attach to plan for API
            plan._min_required = min_req  # type: ignore
        except Exception as e:
            # never fail plan due to min calc
            import logging as _lg
            _lg.getLogger("web").debug("min capital calc fail: %s", e)
            plan._min_required = None  # type: ignore
        STATE["plan"] = plan

        db = Store(str(ROOT / cfg["paths"]["db"]))
        from rebalancer.cli import plan_to_json
        db.save_run(run_id, datetime.now(IST).isoformat(timespec="seconds"),
                    "BLOCKED" if plan.blockers else "PLANNED",
                    plan.nav, plan.free_cash, plan.slice_value, plan_to_json(plan))
        plans = ROOT / cfg["paths"]["plans_dir"]
        plans.mkdir(parents=True, exist_ok=True)
        (plans / f"{run_id}.json").write_text(plan_to_json(plan))

        d = _plan_dict(plan, cfg)
        d["mode"] = STATE["mode"]
        # jo naam abhi bhi hain AUR nayi list mein bhi hain
        held = {h.symbol for h in hs}
        d["held_in_list"] = sorted(held & {t.symbol for t in wl})
        from rebalancer.planner import resolve_n
        d["slots"] = resolve_n(cfg["portfolio"], len(wl))
        d["list_len"] = len(wl)
        d["auto"] = STATE.get("n_override") is None
        d["capital_source"] = ("Demo -- nakli paisa" if isinstance(c, PaperClient)
                               else "Dhan API se live")
        d["holdings_value"] = plan.nav - plan.free_cash
        d["price_source"] = pinfo
        d["min_required"] = getattr(plan, "_min_required", None)
        # also expose for frontend easier: allocated vs required
        if d["min_required"]:
            d["min_required"]["allocated_investable"] = plan.target_equity
            d["min_required"]["allocated_nav"] = plan.nav
        return d

    @app.post("/api/plan/sell-all")
    def plan_sell_all():
        """SAB BECHO -- poora portfolio cash mein.

        Watchlist ki zarurat nahi. Ye normal rebalance se alag hai, isliye
        alag endpoint hai -- galti se normal plan ki jagah ye na chal jaaye.
        """
        _guard_broken()
        cfg = _cfg()
        c = _client(cfg)
        try:
            hs = c.holdings()
            cash = c.available_cash()
        except Exception as e:
            body = getattr(e, "body", None)
            raise HTTPException(502, f"Dhan se portfolio nahi aaya.\n\n{e}"
                                + (f"\n\nDhan ka jawaab: {body}" if body else ""))
        if not hs:
            raise HTTPException(
                400, "Portfolio khaali hai -- bechne ko kuch hai hi nahi.")

        security_ids = {h.symbol: h.security_id for h in hs}
        symbol_of = {sid: s for s, sid in security_ids.items()}
        if isinstance(c, PaperClient):
            c.register(security_ids)
        circuit, ltp, pinfo = _get_prices(c, security_ids, cfg)
        if not any(v > 0 for v in ltp.values()):
            raise HTTPException(
                502, "Kahin se bhi live prices nahi mile -- bina price ke "
                     "bechna khatarnaak hai. Market band toh nahi hai?")

        run_id = datetime.now(IST).strftime("L%Y%m%d-%H%M%S")
        plan = build_liquidation_plan(
            run_id=run_id, holdings=hs, free_cash=cash, ltp=ltp,
            security_ids=security_ids, cfg=cfg, circuit=circuit)
        STATE["plan"] = plan

        db = Store(str(ROOT / cfg["paths"]["db"]))
        from rebalancer.cli import plan_to_json
        db.save_run(run_id, datetime.now(IST).isoformat(timespec="seconds"),
                    "BLOCKED" if plan.blockers else "PLANNED",
                    plan.nav, plan.free_cash, plan.slice_value, plan_to_json(plan))
        plans = ROOT / cfg["paths"]["plans_dir"]
        plans.mkdir(parents=True, exist_ok=True)
        (plans / f"{run_id}.json").write_text(plan_to_json(plan))

        d = _plan_dict(plan, cfg)
        d["mode"] = STATE["mode"]
        d["capital_source"] = ("Demo -- nakli paisa" if isinstance(c, PaperClient)
                               else "Dhan API se live")
        d["holdings_value"] = plan.nav - plan.free_cash
        d["price_source"] = pinfo
        plan.warnings[:0] = _price_warnings(pinfo, cfg)
        d["warnings"] = plan.warnings
        d["slots"] = 0
        d["list_len"] = 0
        d["auto"] = False
        d["held_in_list"] = []
        return d

    # ---------------------------------------------------- execute
    @app.post("/api/execute")
    def execute(body: ExecIn):
        _guard_broken()
        plan = STATE["plan"]
        if plan is None:
            raise HTTPException(400, "Koi plan nahi hai. Pehle plan banao.")
        real = body.mode == "real"
        liq = getattr(plan, "is_liquidation", False)
        want = "sab bech do" if liq else "haan"
        if real and body.confirm.strip().lower() != want:
            raise HTTPException(
                400, f'Asli execution ke liye "{want}" type karna zaroori hai.'
                     + (" Ye poora portfolio bech dega, isliye alag shabd "
                        "maangte hain." if liq else ""))
        # BLOCKED plan par rehearsal bhi nahi chalta -- pehle 500 aata tha,
        # ab saaf message ke saath 400.
        if plan.blockers:
            raise HTTPException(
                400,
                ("Plan BLOCKED hai, isliye " +
                 ("asli execution" if real else "rehearsal") +
                 " nahi chalega. Pehle ye theek karo:\n\n  - " +
                 "\n  - ".join(plan.blockers)))
        cfg = _cfg()
        c = _client(cfg)
        if isinstance(c, PaperClient):
            # Demo broker turant fill karta hai -- funds-release ka 20s wait
            # aur fill polling yahan bekaar hai. Live cfg chhedte nahi.
            cfg = json.loads(json.dumps(cfg))          # deep copy
            cfg["execution"]["phase_gap_sec"] = 0
            cfg["execution"]["fill_poll_interval_sec"] = 0
            cfg["execution"]["market_fallback_after_sec"] = 0
        db = Store(str(ROOT / cfg["paths"]["db"]))
        try:
            res = Executor(c, db, cfg, dry_run=not real).run(plan)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            log.error("execute fail: %s\n%s", e, traceback.format_exc())
            raise HTTPException(500, f"Execution mein error: {e}")
        return {"dry_run": res.get("dry_run", True),
                "sells": res.get("sells") or [], "buys": res.get("buys") or [],
                "failed": res.get("failed") or [],
                "reconciliation": res.get("reconciliation") or {},
                "paper": isinstance(c, PaperClient)}

    # ---------------------------------------------------- credentials
    @app.get("/api/creds")
    def creds_status():
        cfg = _cfg()
        d = cfg["dhan"]
        cid_env = os.environ.get(d["client_id_env"], "")
        file_cid, file_tok = credmod.read_saved(ROOT, d["client_id_env"],
                                                d["access_token_env"])
        cid, tok = _current_creds()
        out = {"has_creds": bool(cid and tok),
               "client_id": cid,
               "masked": credmod.mask(tok) if tok else "",
               "source": "environment" if cid_env else ("creds.bat" if file_cid else None),
               "saved_file": (ROOT / credmod.CREDS_FILE).exists(),
               "id_env": d["client_id_env"], "token_env": d["access_token_env"]}
        if tok:
            out["token_info"] = credmod.token_expiry(tok)
        return out

    @app.post("/api/creds/verify")
    def creds_verify(body: CredsIn):
        cfg = _cfg()
        d = cfg["dhan"]
        cid = (body.client_id or "").strip()
        tok = (body.access_token or "").strip()
        if not cid or not tok:                      # saved wale se try karo
            f_cid, f_tok = credmod.read_saved(ROOT, d["client_id_env"],
                                              d["access_token_env"])
            cid = cid or os.environ.get(d["client_id_env"], "") or f_cid
            tok = tok or os.environ.get(d["access_token_env"], "") or f_tok

        res = credmod.verify(cid, tok, d["base_url"])

        if res.get("ok") and body.save:
            try:
                credmod.save(ROOT, cid, tok, d["client_id_env"], d["access_token_env"])
                os.environ[d["client_id_env"]] = cid       # is session ke liye turant
                os.environ[d["access_token_env"]] = tok
                res["saved"] = True
                with STATE_LOCK:
                    if not STATE["mode_chosen_by_user"]:
                        STATE["mode"] = "live"
                        STATE["plan"] = None
                    # reset autodetect latch on successful verify
                    STATE["autodetect_settled"] = True
                    STATE["creds_broken"] = ""
                    STATE["autodetect_msg"] = "Credentials verify ho gaye -- LIVE"
                    STATE["_next_try_monotonic"] = 0
            except OSError as e:
                res["saved"] = False
                res["save_error"] = str(e)
        elif res.get("ok"):
            os.environ[d["client_id_env"]] = cid
            os.environ[d["access_token_env"]] = tok
            with STATE_LOCK:
                STATE["autodetect_settled"] = True
                STATE["creds_broken"] = ""
                STATE["autodetect_msg"] = "Credentials verify ho gaye"
                STATE["_next_try_monotonic"] = 0
                if not STATE["mode_chosen_by_user"]:
                    STATE["mode"] = "live"
        else:
            # failure reset latch for retry if transient? keep settled logic but update broken
            with STATE_LOCK:
                # if format/token permanent, settle, else allow retry in 20s
                bad = next((st for st in res.get("steps", []) if st["ok"] is False), None)
                bn = bad["name"] if bad else ""
                permanent = bn in ("Format", "Token")
                STATE["autodetect_settled"] = permanent
                STATE["creds_broken"] = bad["msg"] if bad else "Verify fail"
                STATE["_next_try_monotonic"] = time.monotonic() + (999999 if permanent else 20)
        return res

    @app.post("/api/prices/test")
    def prices_test():
        """Free price sources tumhari machine se chalte hain ya nahi.

        Main ye khud check nahi kar sakta -- Yahoo/NSE mere paas se block
        hain. Isliye button yahan hai: ek click mein tumhe pata chal jaayega.
        """
        cfg = _cfg()
        order = list(cfg.get("prices", {}).get("fallback") or [])
        if not order:
            raise HTTPException(400, "config.yaml mein prices.fallback khaali hai.")
        wl = STATE["watchlist"] or []
        syms = [t.symbol for t in wl[:5]] or ["RELIANCE", "TCS", "HFCL"]

        out = []
        for name in order:
            fn = pricemod.PROVIDERS.get(str(name).lower())
            if not fn:
                out.append({"name": name, "ok": False,
                            "msg": "aisa koi source nahi hai"})
                continue
            t0 = time.time()
            try:
                got = fn(syms, timeout=12)
            except Exception as e:
                out.append({"name": name, "ok": False,
                            "msg": f"{type(e).__name__}: {e}"[:200]})
                continue
            took = round(time.time() - t0, 1)
            if not got:
                out.append({"name": name, "ok": False,
                            "msg": f"ek bhi price nahi mila ({took}s). "
                                   f"Ye source tumhare network se block ho "
                                   f"sakta hai."})
                continue
            ages = [q.age_sec for q in got.values() if q.age_sec is not None]
            age = round(max(ages) / 60.0, 1) if ages else None
            out.append({
                "name": name, "ok": True, "took": took,
                "msg": f"{len(got)}/{len(syms)} price mile ({took}s)",
                "age_min": age,
                "circuits": sum(1 for q in got.values() if q.has_circuit),
                "sample": [{"symbol": q.symbol, "ltp": q.ltp,
                            "volume": q.volume, "circuit": q.has_circuit}
                           for q in list(got.values())[:5]],
            })
        return {"symbols": syms, "results": out,
                "any_ok": any(r["ok"] for r in out)}

    @app.post("/api/creds/clear")
    def creds_clear():
        cfg = _cfg()
        d = cfg["dhan"]
        removed = credmod.clear(ROOT)
        os.environ.pop(d["client_id_env"], None)
        os.environ.pop(d["access_token_env"], None)
        with STATE_LOCK:
            if STATE.get("mode") == "live":
                STATE["mode"] = "paper"
                STATE["plan"] = None
            # reset autodetect so next health re-evaluates cleanly
            STATE["autodetect_settled"] = False
            STATE["creds_broken"] = ""
            STATE["autodetect_msg"] = "Credentials cleared -- Demo mode"
            STATE["_next_try_monotonic"] = 0
            mode = STATE["mode"]
        return {"removed": removed, "mode": mode}

    @app.post("/api/slots")
    def set_slots(body: SlotsIn):
        """Kitne stocks mein baantna hai -- config chhue bina."""
        with STATE_LOCK:
            wl = STATE.get("watchlist") or []
        if body.mode == "auto":
            with STATE_LOCK:
                STATE["n_override"] = None
        else:
            if body.n is None or body.n < 1:
                raise HTTPException(400, "Kam se kam 1 stock chahiye.")
            if wl and body.n > len(wl):
                raise HTTPException(
                    400, f"List mein sirf {len(wl)} naam hain, {body.n} nahi ho sakte.")
            with STATE_LOCK:
                STATE["n_override"] = int(body.n)
        if body.use_overflow is not None:
            with STATE_LOCK:
                STATE["overflow_override"] = bool(body.use_overflow)
        with STATE_LOCK:
            STATE["plan"] = None
        cfg = _cfg()
        from rebalancer.planner import resolve_n
        n = resolve_n(cfg["portfolio"], len(wl)) if wl else None
        with STATE_LOCK:
            auto = STATE.get("n_override") is None
        return {"n_stocks": n, "auto": auto,
                "use_overflow": bool(cfg["portfolio"]["use_overflow_slot"]),
                "list_len": len(wl)}

    # ---------------------------------------------------- deploy budget
    @app.get("/api/deploy")
    def get_deploy():
        """Abhi kitna deploy hone wala hai + live NAV ke saath preview."""
        cfg = _cfg()
        out = _deploy_state(cfg)
        out["nav"] = None
        out["nav_source"] = ""
        out["reserve_pct"] = float(cfg["portfolio"]["cash_reserve_pct"]) * 100
        try:
            if _creds_present() or STATE["mode"] == "paper":
                c = _client(cfg)
                hs = c.holdings()
                cash = c.available_cash()
                hv = 0.0
                stale = []
                if hs:
                    # WAHI price path jo plan use karta hai (Dhan -> Yahoo -> NSE).
                    # Warna deploy card kuch aur NAV dikhata aur plan kuch aur --
                    # aur uska pata tab chalta jab order ja chuke hote.
                    sec = {h.symbol: h.security_id for h in hs}
                    if isinstance(c, PaperClient):
                        c.register(sec)
                    _circ, ltp, pinfo = _get_prices(c, sec, cfg)
                    out["price_source"] = pinfo
                    for h in hs:
                        px = ltp.get(h.symbol, 0.0)
                        if px <= 0:
                            px = h.avg_price
                            stale.append(h.symbol)
                        hv += h.total_qty * px
                out["nav"] = hv + cash
                out["holdings_value"] = hv
                out["free_cash"] = cash
                out["stale_priced"] = stale
                out["nav_source"] = ("Demo -- nakli paisa"
                                     if isinstance(c, PaperClient)
                                     else "Dhan/free source se live")
        except Exception as e:                 # NAV na mile toh bhi UI chale
            out["nav_error"] = str(e)[:200]

        nav = out.get("nav")
        if nav and nav > 0:
            from rebalancer.planner import resolve_deploy
            cap, label = resolve_deploy(cfg["portfolio"], nav)
            reserve_cap = nav * (1 - float(cfg["portfolio"]["cash_reserve_pct"]))
            eq = max(0.0, min(cap, reserve_cap))
            out["preview"] = {"label": label, "equity": eq,
                              "cash": max(0.0, nav - eq),
                              "capped": cap > reserve_cap}
        return out

    @app.post("/api/deploy")
    def set_deploy(body: DeployIn):
        """Kitni capital stocks mein lagani hai.

        Ye sirf is session ke liye hai -- config.yaml waise ki waisi rehti
        hai. 'all' bhejne par app wapas config.yaml wali setting par chali
        jaayegi.
        """
        # Khaali mode chupchaap "all" mat maan lena -- "all" ka matlab poora
        # paisa market mein, aur wahi cheez user rokna chah raha hai.
        if body.mode is None or not str(body.mode).strip():
            raise HTTPException(400, "mode batao: all / pct / amount.")
        mode = str(body.mode).strip().lower()
        if mode in ("percent", "percentage", "%"):
            mode = "pct"
        if mode in ("amt", "rupees", "inr", "fixed"):
            mode = "amount"
        if mode not in ("all", "pct", "amount", "config"):
            raise HTTPException(400, "mode sirf all / pct / amount ho sakta hai.")

        with STATE_LOCK:
            if mode == "config":
                STATE["deploy_mode"] = STATE["deploy_pct"] = STATE["deploy_amount"] = None
            elif mode == "pct":
                if body.pct is None:
                    raise HTTPException(400, "Percent bhi bhejo (0 se 100).")
                v = float(body.pct)
                if v != v or v < 0 or v > 100:
                    raise HTTPException(
                        400, f"Percent 0 se 100 ke beech hona chahiye, {body.pct} nahi.")
                STATE["deploy_mode"] = "pct"
                STATE["deploy_pct"] = v / 100.0
                STATE["deploy_amount"] = None
            elif mode == "amount":
                if body.amount is None:
                    raise HTTPException(400, "Amount bhi bhejo (rupees mein).")
                v = float(body.amount)
                if v != v or v < 0:
                    raise HTTPException(400, "Amount negative nahi ho sakta.")
                STATE["deploy_mode"] = "amount"
                STATE["deploy_amount"] = v
                STATE["deploy_pct"] = None
            else:
                STATE["deploy_mode"] = "all"
                STATE["deploy_pct"] = None
                STATE["deploy_amount"] = None

            # Setting badli = purana plan jhootha ho gaya. Hata do, warna user
            # naye budget ke bharose purana plan execute kar dega.
            STATE["plan"] = None
        return get_deploy()

    @app.post("/api/demo/capital")
    def demo_capital(body: CapitalIn):
        """Demo ka nakli capital. Live mode par iska koi asar nahi --
        wahan paisa hamesha Dhan se aata hai."""
        if STATE["mode"] != "paper":
            raise HTTPException(400, "Ye sirf Demo mode ke liye hai. Live mein "
                                     "capital Dhan se aata hai, set nahi hota.")
        if body.capital < 10_000:
            raise HTTPException(400, "Kam se kam Rs.10,000 rakho.")
        if body.capital > 1000_00_00_000:
            raise HTTPException(400, "Itna bada demo capital theek nahi.")
        STATE["demo_capital"] = float(body.capital)
        STATE["paper"] = None                 # naye capital se dobara bano
        STATE["plan"] = None
        c = _client(_cfg())
        return {"capital": STATE["demo_capital"],
                "stocks_value": sum(v[1] * (c._ltp.get(s) or v[2])
                                    for s, v in c._pos.items()),
                "cash": c.available_cash()}

    @app.post("/api/demo/reset")
    def demo_reset():
        STATE["paper"] = None
        STATE["plan"] = None
        STATE["demo_capital"] = None
        return {"ok": True}

    # ---------------------------------------------------- history
    @app.get("/api/runs")
    def runs():
        cfg = _cfg()
        try:
            db = Store(str(ROOT / cfg["paths"]["db"]))
            return {"runs": db.recent_runs(20)}
        except Exception:
            return {"runs": []}
