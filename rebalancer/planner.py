"""
Diff engine -- is poore system ka dimaag.

Ye module JAAN-BUJH KE pure hai: koi network call nahi, koi file read nahi,
koi clock nahi. Sirf (holdings, prices, watchlist, config) andar -> Plan bahar.

Isi wajah se:
  * saal bhar ka backtest theek isi code par chal sakta hai jo live jaata hai
  * har edge case ka unit test 2 line mein likha jaa sakta hai
  * paise wala logic kabhi API failure ke saath uljhega nahi
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping

from .models import (CircuitInfo, CostEstimate, Plan, PlannedOrder, Position,
                     Reason, Side, Skipped, TargetName)


# ----------------------------------------------------------------------
#  helpers
# ----------------------------------------------------------------------
def _floor_qty(value: float, price: float) -> int:
    try:
        if price is None or value is None:
            return 0
        if price != price or value != value:  # NaN
            return 0
        if price <= 0 or value <= 0 or math.isinf(price) or math.isinf(value):
            return 0
        return max(0, math.floor(value / price))
    except (ValueError, OverflowError, TypeError):
        return 0


def _limit_price(ltp: float, side: Side, buffer_pct: float, circuit: CircuitInfo | None = None) -> float:
    """BUY thoda upar, SELL thoda neeche -- fill ki guarantee badhti hai
    bina market order ke slippage ke. FIX: circuit ke andar clamp karo warna Dhan 400 deta hai."""
    try:
        if ltp is None or ltp != ltp or ltp <= 0 or math.isinf(ltp):
            return 0.0
        if buffer_pct != buffer_pct or math.isinf(buffer_pct) or buffer_pct < 0:
            buffer_pct = 0.0
        mult = (1 + buffer_pct) if side is Side.BUY else (1 - buffer_pct)
        px = round(ltp * mult, 2)          # NSE tick size = 0.05 for most,
                                           # 0.01 allowed; 2 decimals safe hai
        # FIX: clamp to circuit limits if available - prevents 400 Price outside circuit
        if circuit and circuit.upper > 0 and circuit.lower > 0:
            # keep 0.05 buffer from edge to avoid reject on exactly circuit
            low = circuit.lower * 1.001
            high = circuit.upper * 0.999
            if px < low:
                px = round(low, 2)
            elif px > high:
                px = round(high, 2)
            # never cross ltp if at circuit - BUY at upper should be at upper, SELL at lower
            # if at_upper, BUY must be exactly upper (no fill but valid), same for SELL lower
            if circuit.at_upper and side is Side.BUY:
                px = round(circuit.upper * 0.999, 2)
            if circuit.at_lower and side is Side.SELL:
                px = round(circuit.lower * 1.001, 2)
        # tick size 0.05 rounding - Dhan expects multiples of 0.05 for most EQ
        # round to nearest 0.05 to avoid 400 Tick size error
        px = round(round(px / 0.05) * 0.05, 2)
        return px
    except (ValueError, OverflowError, TypeError, AttributeError):
        return 0.0


# ----------------------------------------------------------------------
#  cost model (planning ke liye estimate -- exact bill broker deta hai)
# ----------------------------------------------------------------------
def resolve_n(p_cfg: Mapping, list_len: int) -> int:
    """Kitne stocks mein barabar baantna hai.

    n_stocks: <number>  -> wahi
    n_stocks: auto      -> jitne naam list mein hain (overflow slot chhod kar)

    Auto matlab app list ke saath badal jaati hai -- 5 naam ho ya 50,
    kuch badalna nahi padta.
    """
    raw = p_cfg.get("n_stocks", "auto")
    if raw is None or (isinstance(raw, str) and str(raw).strip().lower() in ("", "auto", "none")):
        use_of = bool(p_cfg.get("use_overflow_slot"))
        n = list_len - 1 if (use_of and list_len > 1) else list_len
        return max(1, n)
    if isinstance(raw, str) and str(raw).strip().lower() == "auto":
        use_of = bool(p_cfg.get("use_overflow_slot"))
        n = list_len - 1 if (use_of and list_len > 1) else list_len
        return max(1, n)
    try:
        # handle "10.0" string -> 10
        if isinstance(raw, str):
            raw = raw.strip()
            if "." in raw:
                return max(1, int(float(raw)))
        return max(1, int(float(raw) if isinstance(raw, float) else int(raw)))
    except (ValueError, TypeError, OverflowError):
        use_of = bool(p_cfg.get("use_overflow_slot"))
        n = list_len - 1 if (use_of and list_len > 1) else list_len
        return max(1, n)


def resolve_exit_rank(p_cfg: Mapping, list_len: int, n: int) -> int:
    """Rank isse neeche gaya toh naam bahar. auto = n (strict top-n)."""
    raw = p_cfg.get("exit_rank_threshold", "auto")
    if raw is None or (isinstance(raw, str) and str(raw).strip().lower() in ("", "auto", "none")):
        return n
    if isinstance(raw, str) and str(raw).strip().lower() == "auto":
        return n
    try:
        if isinstance(raw, str) and "." in raw.strip():
            return max(n, int(float(raw.strip())))
        return max(n, int(float(raw) if isinstance(raw, float) else int(raw)))
    except (ValueError, TypeError, OverflowError):
        return n


def _inr_label(x: float) -> str:
    """1,74,800 -- Indian grouping, kyunki 174,800 padhne mein dikkat hoti hai."""
    neg = x < 0
    d = f"{abs(x):.0f}"
    if len(d) > 3:
        head, tail = d[:-3], d[-3:]
        g = []
        while len(head) > 2:
            g.insert(0, head[-2:]); head = head[:-2]
        if head:
            g.insert(0, head)
        d = ",".join(g) + "," + tail
    return ("-" if neg else "") + d


def resolve_deploy(p_cfg: Mapping, nav: float) -> tuple[float, str]:
    """Poori capital mein se KITNA stocks mein lagana hai.

    Ye cash_reserve_pct se alag cheez hai. cash_reserve ek chhota buffer
    hai (charges, rounding). Deploy budget ek soch-samajh kar liya gaya
    faisla hai -- "abhi 60% hi market mein rakhna hai".

      deploy_mode: all     -> poori NAV (purana behaviour, default)
      deploy_mode: pct     -> deploy_pct hissa. 0.60 ya 60 dono chalega.
      deploy_mode: amount  -> deploy_amount rupees, chahe NAV kuch bhi ho.

    Return: (rupees, label). Rupees hamesha >= 0. NAV se upar ka cap
    caller lagata hai (kyunki cash_reserve bhi wahin lagta hai).
    """
    raw_mode = p_cfg.get("deploy_mode", "all")
    mode = str(raw_mode).strip().lower() if raw_mode is not None else "all"
    # explicit typo guard - unknown mode should not silently become "all" (over-invest)
    # but for backward compat we treat unknown as all with warning handled by caller
    if mode in ("pct", "percent", "percentage", "%"):
        try:
            raw = float(p_cfg.get("deploy_pct", 1.0) or 0.0)
        except (TypeError, ValueError):
            raw = 1.0
        if raw != raw or raw < 0:              # NaN / negative
            raw = 0.0
        # FIX cliff at 1.0: treat raw as percent if raw >1, but raw==1.0 is now 1% not 100%
        # Heuristic: if raw <=1.0, treat as fraction (0.6=60%); if 1<raw<=100 treat as percent (60=60%)
        # raw ==1.0 historically 100%, but we now treat as 100%? Keep 1.0 as 100% for compat?
        # To avoid 99% drop, treat raw==1.0 as 100% (legacy). raw>1 => percent.
        if raw == 1.0:
            pct = 1.0
        else:
            pct = raw / 100.0 if raw > 1.0 else raw
        pct = min(max(pct, 0.0), 1.0)
        if pct < 0.01 and raw > 0:
            # warn caller? pct 0.01 => 1% suspiciously low, but allow - don't silently 0
            pass
        return nav * pct, f"NAV ka {pct * 100:.4g}%"

    if mode in ("amount", "amt", "rupees", "inr", "fixed"):
        try:
            amt = float(p_cfg.get("deploy_amount", 0) or 0.0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt != amt or amt < 0 or math.isinf(amt):
            amt = 0.0
        return amt, f"Rs.{_inr_label(amt)} (fixed)"

    # unknown mode -> treat as all but caller can warn if needed
    return nav, "poori capital"


# Statutory rates -- ye sarkar/exchange badalte rehte hain. Config mein
# override kar sakte ho (costs.rates), warna ye default use hote hain.
# Aakhri baar verify: Aug 2026, equity DELIVERY ke liye.
_DEFAULT_RATES = {
    "brokerage_pct": 0.0,        # Dhan delivery = zero
    "stt_pct": 0.001,            # 0.1%, buy aur sell dono par
    "txn_charges_pct": 0.0000307,  # NSE 0.00307%
    "stamp_duty_pct": 0.00015,   # 0.015%, SIRF buy par
    "sebi_fees_pct": 0.000001,   # Rs.10 per crore
    "gst_pct": 0.18,             # 18% on (brokerage + txn + SEBI)
}


def annualised_cost(one_time: float, nav: float, cfg: Mapping) -> dict:
    """Ek rebalance ka kharcha saal bhar mein kitna banega.

    Ye sabse zaroori number hai jo aksar nahi dikhaya jaata. Ek baar ka
    Rs.15,000 chhota lagta hai; wahi hafte-hafte 52 baar = Rs.7.9 lakh,
    yaani capital ka 8%. Strategy ko itna extra kamana padega sirf
    barabar par aane ke liye.
    """
    per_year = int(cfg["portfolio"].get("rebalances_per_year", 52) or 52)
    yearly = one_time * per_year
    return {"per_year": per_year, "yearly": yearly,
            "pct_of_nav": (yearly / nav * 100) if nav > 0 else 0.0}


def cost_rates(cfg: Mapping) -> dict:
    r = dict(_DEFAULT_RATES)
    r.update(cfg["costs"].get("rates") or {})
    return r


def estimate_costs(buy_value: float, sell_value: float,
                   n_sell_scrips: int, cfg: Mapping) -> CostEstimate:
    c = cfg["costs"]
    r = cost_rates(cfg)
    both = buy_value + sell_value
    e = CostEstimate()
    e.brokerage = float(r["brokerage_pct"]) * both
    e.stt = float(r["stt_pct"]) * both
    e.txn_charges = float(r["txn_charges_pct"]) * both
    e.stamp_duty = float(r["stamp_duty_pct"]) * buy_value
    e.sebi_fees = float(r["sebi_fees_pct"]) * both
    e.dp_charges = float(c["dp_charge_per_scrip_inr"]) * n_sell_scrips
    e.gst = float(r["gst_pct"]) * (e.brokerage + e.txn_charges + e.sebi_fees)
    return e


# ----------------------------------------------------------------------
#  main entry point
# ----------------------------------------------------------------------
def build_plan(
    *,
    run_id: str,
    holdings: Iterable[Position],
    free_cash: float,
    watchlist: list[TargetName],
    ltp: Mapping[str, float],
    security_ids: Mapping[str, str],
    cfg: Mapping,
    circuit: Mapping[str, CircuitInfo] | None = None,
) -> Plan:
    try:
        p_cfg, c_cfg, r_cfg, x_cfg = cfg["portfolio"], cfg["costs"], cfg["risk"], cfg["execution"]
        band = float(p_cfg.get("drift_band_pct", 0.0) or 0.0)
        buf = float(x_cfg.get("limit_buffer_pct", 0.003) or 0.0)
        if band != band or band < 0 or math.isinf(band):
            band = 0.0
        if buf != buf or buf < 0 or math.isinf(buf):
            buf = 0.003
    except (KeyError, TypeError, ValueError) as e:
        # config missing keys -> blocker rather than crash
        plan = Plan(run_id=run_id, nav=0.0, free_cash=free_cash, slice_value=0.0)
        plan.blockers.append(f"Config galat hai: {e}")
        return plan
    # aggregate duplicate holdings by symbol (two demat entries etc)
    held: dict[str, Position] = {}
    for h in holdings:
        if h.symbol in held:
            prev = held[h.symbol]
            # sum quantities, weighted avg price
            tot = prev.total_qty + h.total_qty
            avail = prev.available_qty + h.available_qty
            avg = (prev.avg_price * prev.total_qty + h.avg_price * h.total_qty) / tot if tot else 0
            held[h.symbol] = Position(symbol=h.symbol, security_id=prev.security_id,
                                      total_qty=tot, available_qty=min(avail, tot), avg_price=avg)
            # will warn later about data issue via has_data_issue
        else:
            held[h.symbol] = h
    plan = Plan(run_id=run_id, nav=0.0, free_cash=free_cash, slice_value=0.0)
    # surface data-integrity warnings
    for sym, pos in held.items():
        if pos.has_data_issue:
            plan.warnings.append(f"{sym}: data issue - available {pos.available_qty} > total {pos.total_qty} (capped)")

    # deploy mode typo guard
    valid_modes = ("all", "pct", "percent", "percentage", "%", "amount", "amt", "rupees", "inr", "fixed")
    dm_raw = str(p_cfg.get("deploy_mode", "all")).strip().lower()
    if dm_raw not in valid_modes:
        plan.warnings.append(f"deploy_mode '{p_cfg.get('deploy_mode')}' samajh nahi aaya, 'all' maan rahe hain")

    # ---- 0. watchlist sanity ------------------------------------------
    wl = sorted(watchlist, key=lambda t: t.rank)

    # n_stocks / exit_rank_threshold "auto" ho sakte hain -- tab list ka
    # size hi decide karta hai. Isse 5 naam ho ya 50, app waise hi chalti hai.
    n = resolve_n(p_cfg, len(wl))
    exit_rank = resolve_exit_rank(p_cfg, len(wl), n)
    symbols = [t.symbol for t in wl]
    if len(set(symbols)) != len(symbols):
        plan.blockers.append("Watchlist mein duplicate symbol hai -- duplicate hatao.")
        return plan
    if not wl:
        plan.blockers.append("Watchlist khaali hai.")
        return plan

    targets = [t.symbol for t in wl[:n]]
    keep_zone = {t.symbol for t in wl[:max(exit_rank, n)]}
    overflow = (wl[n].symbol
                if p_cfg.get("use_overflow_slot") and len(wl) > n else None)

    # --- exit_rank > n rakha hai toh? ----------------------------------
    # Rank n+1..exit_rank wale naam "keep zone" mein hain: bechte nahi, par
    # target bhi nahi hain -- yaani unka weight manage bhi nahi hota. Unka
    # paisa NAV mein ginta hai par top-n ko milta nahi. Ye jaanbujh kar hai
    # (churn kam karne ke liye) par chupchaap nahi hona chahiye.
    if exit_rank > n:
        # exclude overflow from parked warning to match investable calc
        overflow_set = {overflow} if overflow else set()
        parked = sorted(set(held) & (keep_zone - set(targets)) - overflow_set)
        if parked:
            pval = sum(held[s].total_qty * ltp.get(s, 0) for s in parked)
            plan.warnings.append(
                f"{len(parked)} naam keep-zone mein hain par top-{n} mein nahi: "
                f"{', '.join(parked)}. Inhe na becha ja raha hai na inka weight "
                f"adjust ho raha hai (exit_rank_threshold {exit_rank} > n_stocks "
                f"{n}). Inka Rs.{pval:,.0f} phansa rehta hai aur top-{n} utna hi "
                f"kam funded rahega.")

    # --- n se kam naam aaye toh? ---------------------------------------
    #   full        -> jitne naam hain unhi mein poora paisa (100% invested)
    #   fixed_slots -> hamesha NAV/n, baaki cash (backtest jaisa)
    raw_mode = str(p_cfg.get("partial_list_mode", "full")).strip().lower()
    if raw_mode not in ("full", "fixed_slots", "fixed"):
        plan.warnings.append(f"partial_list_mode '{p_cfg.get('partial_list_mode')}' galat, 'full' maan rahe hain")
        raw_mode = "full"
    # normalize fixed -> fixed_slots
    if raw_mode == "fixed":
        raw_mode = "fixed_slots"
    mode = raw_mode
    slots = len(targets) if mode == "full" else n
    if len(targets) < n:
        if mode == "full":
            plan.warnings.append(
                f"Watchlist mein sirf {len(targets)} naam hain ({n} nahi) -- "
                f"poora paisa inhi mein baat raha hai, har ek "
                f"~{100/slots:.0f}%. Portfolio 100% invested rahega.")
        else:
            plan.warnings.append(
                f"Sirf {len(targets)} naam hain -- {len(targets)*100//n}% "
                f"invested, baaki cash mein rahega.")

    # ---- 1. price / instrument availability ---------------------------
    needed = set(targets) | set(held) | ({overflow} if overflow else set())
    # also need keep-zone? not strictly needed to block, but check for ltp availability for parked calc
    for s in sorted(needed):
        if s not in security_ids:
            plan.blockers.append(f"{s}: securityId nahi mila (scrip master check karo).")
        # don't use elif - both errors matter
        if ltp.get(s, 0) is None or ltp.get(s, 0) <= 0:
            # only append if not already blocked for same symbol? allow both
            if f"{s}: securityId" not in " ".join(plan.blockers):
                plan.blockers.append(f"{s}: LTP nahi mila / zero hai.")
            elif s not in security_ids:
                pass  # already blocked for missing id
            else:
                plan.blockers.append(f"{s}: LTP nahi mila / zero hai.")
    if plan.blockers:
        return plan

    # ---- 2. NAV aur slice ---------------------------------------------
    # use only sellable? No, NAV is total value regardless of T+1
    holdings_value = sum(h.total_qty * ltp[h.symbol] for h in held.values() if ltp.get(h.symbol, 0) > 0)
    nav = holdings_value + free_cash
    # guard NaN/inf
    if nav != nav or math.isinf(nav):
        plan.blockers.append("NAV NaN/inf - prices ya cash galat hai")
        return plan
    plan.nav = nav
    if nav <= 0:
        plan.blockers.append(
            "Account bilkul khaali hai -- na koi share, na cash. "
            "Pehle Dhan mein paisa daalo, phir plan banega. "
            "(Ye app ki galti nahi hai; lagane ko kuch hai hi nahi.)")
        return plan

    # --- kitna paisa stocks mein jaayega? -------------------------------
    # Do alag cheezein hain aur dono ka paalan hota hai:
    #   cash_reserve_pct -> chhota technical buffer (charges/rounding)
    #   deploy budget    -> user ka faisla ("abhi 60% hi lagana hai")
    # Jo bhi CHHOTA ho wahi chalta hai.
    try:
        cr = float(p_cfg.get("cash_reserve_pct", 0.01) or 0.0)
        if cr != cr or cr < 0 or cr >= 0.5 or math.isinf(cr):
            plan.warnings.append(f"cash_reserve_pct {cr} galat, 0.01 maan rahe hain")
            cr = 0.01
    except (TypeError, ValueError):
        cr = 0.01
    reserve_cap = nav * (1 - cr)
    deploy_cap, deploy_label = resolve_deploy(p_cfg, nav)
    plan.deploy_label = deploy_label

    # clamp reserve_cap to valid
    if reserve_cap < 0:
        reserve_cap = 0
    if reserve_cap > nav:
        reserve_cap = nav
    if deploy_cap > reserve_cap:
        if deploy_cap > nav:
            plan.warnings.append(
                f"Deploy budget ({deploy_label}) NAV Rs.{nav:,.0f} se bada hai -- "
                f"itna paisa hai hi nahi. Jitna hai utna hi laga rahe hain.")
        else:
            plan.warnings.append(
                f"Deploy budget ({deploy_label}) cash reserve ki wajah se "
                f"Rs.{reserve_cap:,.0f} pe cap ho raha hai (1% buffer).")
        deploy_cap = reserve_cap

    target_equity = max(0.0, deploy_cap)
    plan.target_equity = target_equity

    # Keep-zone mein padi hui (bina manage ki) holdings bhi stocks hi hain --
    # deploy budget mein wo bhi ginti hain, warna "60% lagao" jhooth ho jaata.
    # BUT only count sellable part? Unsellable is trapped - top-n can't get it
    # So we cap parked_value to sellable? Actually total value is still equity.
    # We use total_qty * ltp but warn if unsellable part included.
    parked_now = sorted(set(held) & (keep_zone - set(targets)) - ({overflow} if overflow else set()))
    parked_value = sum(held[s].total_qty * ltp[s] for s in parked_now if ltp.get(s,0)>0)
    # also warn if parked has unsettled
    for s in parked_now:
        pos = held[s]
        if pos.total_qty > pos.sellable:
            plan.warnings.append(f"{s} keep-zone me hai par {pos.total_qty-pos.sellable} qty unsettled - wo bhi deployed me gina par sellable nahi")

    investable = max(0.0, target_equity - parked_value)
    if parked_value > 0 and investable < target_equity:
        plan.warnings.append(
            f"Keep-zone mein Rs.{parked_value:,.0f} pehle se laga hua hai -- "
            f"wo bhi deploy budget mein gina gaya. Top-{n} ke liye "
            f"Rs.{investable:,.0f} bacha.")

    if target_equity < reserve_cap - 1:
        plan.warnings.append(
            f"DEPLOY BUDGET: {deploy_label} = Rs.{target_equity:,.0f} stocks mein. "
            f"Baaki Rs.{max(0.0, nav - target_equity):,.0f} cash mein rahega "
            f"(NAV Rs.{nav:,.0f}). Poora lagana ho toh Plan tab se badal do.")

    slice_value = investable / slots if slots > 0 else 0.0
    plan.slice_value = slice_value

    # concentration: 1-2 naam wale hafte mein ye 50-100% ho jaata hai
    weight = slice_value / nav if nav > 0 else 0
    cap_raw = p_cfg.get("max_weight_per_stock_pct")
    cap = None
    weight_cap_val = float("inf")
    if cap_raw is not None:
        try:
            cap_val = float(cap_raw)
            if cap_val != cap_val or cap_val <= 0 or math.isinf(cap_val) or cap_val > 1.0:
                # 0 means no cap explicitly, >1 is 100%+ invalid
                if cap_val == 0:
                    cap = None
                else:
                    plan.warnings.append(f"max_weight_per_stock_pct {cap_raw} invalid, ignore")
                    cap = None
            else:
                cap = cap_val
                weight_cap_val = nav * cap
        except (TypeError, ValueError):
            plan.warnings.append(f"max_weight cap parse nahi hua {cap_raw}")
            cap = None
    if cap is not None and weight > cap:
        slice_value = weight_cap_val
        plan.slice_value = slice_value
        plan.warnings.append(
            f"Har stock {weight*100:.0f}% banta tha -- cap "
            f"{cap*100:.0f}% laga diya, baaki cash mein rahega.")
        # investable also effectively reduced? slice*n = weight_cap*slots vs investable
        # keep investable consistent? slice*n may be less than investable now, leftover cash will be cash_after
    elif weight > 0.25:
        plan.warnings.append(
            f"!! CONCENTRATION: har stock NAV ka {weight*100:.0f}% ho raha hai "
            f"({slots} naam). Ek stock 20% gira toh portfolio "
            f"{weight*20:.1f}% girega. Cap chahiye toh config mein "
            f"max_weight_per_stock_pct set karo.")

    # whole-share rounding kitni buri hogi?
    priciest = max((ltp[s] for s in targets), default=0.0)
    if priciest > 0 and slice_value > 0 and slice_value / priciest < 15:
        plan.warnings.append(
            f"Slice Rs.{slice_value:,.0f} sirf {slice_value/priciest:.1f} share "
            f"le paayega sabse mehnge scrip ka -- equal weight kaafi kharab "
            f"rahega. Capital badhao ya n kam karo.")

    try:
        mtv = float(c_cfg.get("min_trade_value_inr", 500) or 500)
        mtp = float(c_cfg.get("min_trade_pct_of_slice", 0.03) or 0.03)
        if mtv != mtv or mtv < 0 or math.isinf(mtv):
            mtv = 500
        if mtp != mtp or mtp < 0 or mtp > 1 or math.isinf(mtp):
            mtp = 0.03
        if slice_value != slice_value or math.isinf(slice_value):
            slice_value = 0
        min_trade = max(mtv, slice_value * mtp)
        if min_trade != min_trade or math.isinf(min_trade):
            min_trade = mtv
    except (TypeError, ValueError):
        min_trade = 500

    # ---- 3. SELL side: EXIT + TRIM ------------------------------------
    sells: list[PlannedOrder] = []

    # 3a. list se bahar -> poora exit (overflow slot ko chhod kar)
    # EXIT dust check: very small exits where cost > value -> warn but still sell if meaningful
    dp_per_scrip = float(c_cfg.get("dp_charge_per_scrip_inr", 14.75) or 0)
    est_sell_cost = float(c_cfg.get("est_sell_cost_pct", 0.0012) or 0)
    for sym, pos in sorted(held.items()):
        if sym in keep_zone or sym == overflow:
            continue
        qty = pos.sellable
        if qty <= 0:
            plan.skipped.append(Skipped(sym, "exit chahiye tha par DP-free qty 0 hai (T+1 pending)"))
            continue
        if pos.total_qty > pos.sellable:
            plan.warnings.append(
                f"{sym}: {pos.total_qty} hold, sirf {pos.sellable} sellable "
                f"(baaki unsettled) -- partial exit ho raha hai.")
        val = qty * ltp[sym]
        # skip dust exits where DP charges exceed net proceeds significantly?
        # But don't block exit - user wants out. Just warn if min_trade tiny and cost high
        est_cost_exit = val * est_sell_cost + dp_per_scrip
        if val < min_trade * 0.5 and val < 200 and est_cost_exit > val * 0.5:
            # still allow but warn - dust exit
            plan.warnings.append(f"{sym} EXIT Rs.{val:,.0f} bahut chhota, charges Rs.{est_cost_exit:,.0f} ~ {est_cost_exit/val*100:.0f}%")
        # validate limit_price not zero (stale ltp guard)
        lp = _limit_price(ltp[sym], Side.SELL, buf, circuit.get(sym) if circuit else None)
        if lp <= 0:
            plan.skipped.append(Skipped(sym, f"exit skip - LTP {ltp[sym]} se limit_price nahi bana"))
            continue
        sells.append(PlannedOrder(
            symbol=sym, security_id=security_ids[sym], side=Side.SELL, qty=qty,
            ref_price=ltp[sym], reason=Reason.EXIT,
            limit_price=lp,
            note=f"rank {exit_rank} se bahar"))

    # 3b. target mein hai par overweight -> trim
    for sym in targets:
        pos = held.get(sym)
        if pos is None:
            continue
        cur_val = pos.total_qty * ltp[sym]
        # slice 0 ho sakti hai (deploy budget 0) -- tab poora nikaalna hai,
        # divide-by-zero nahi karna.
        if slice_value > 0:
            drift = (cur_val - slice_value) / slice_value
        else:
            drift = float("inf") if cur_val > 0 else 0.0
        if drift <= band:
            continue
        excess_qty = min(pos.sellable,
                         _floor_qty(cur_val - slice_value, ltp[sym]))
        if excess_qty <= 0:
            continue
        if excess_qty * ltp[sym] < min_trade:
            plan.skipped.append(Skipped(
                sym, f"trim Rs.{excess_qty*ltp[sym]:,.0f} < min trade Rs.{min_trade:,.0f}"))
            continue
        sells.append(PlannedOrder(
            symbol=sym, security_id=security_ids[sym], side=Side.SELL,
            qty=excess_qty, ref_price=ltp[sym], reason=Reason.TRIM,
            limit_price=_limit_price(ltp[sym], Side.SELL, buf, circuit.get(sym) if circuit else None),
            note=f"weight {cur_val/nav*100:.1f}% -> target {slice_value/nav*100:.1f}%"))

    sell_value = sum(o.value for o in sells)
    try:
        sc = float(c_cfg.get("est_sell_cost_pct", 0.0012) or 0)
        if sc != sc or sc < 0 or sc > 0.1 or math.isinf(sc):
            sc = 0.0012
    except (TypeError, ValueError):
        sc = 0.0012
    net_proceeds = sell_value * (1 - sc)
    net_proceeds -= dp_per_scrip * len(sells)
    # floor at free_cash equivalent - selling should never reduce budget below free_cash alone
    # if net_proceeds negative (dust sells cost > proceeds), floor to 0 incremental
    if net_proceeds < 0 and sell_value < dp_per_scrip * len(sells) * 2:
        # dust exits - but we still executed them? budget should not go below free_cash
        # net_proceeds may be negative, but budget = free_cash + net_proceeds will decrease
        # Clamp net_proceeds to at least 0? Or at least sell_value - costs if costs exaggerated
        # Keep realistic negative but warn
        if net_proceeds < -free_cash:
            net_proceeds = -free_cash  # budget floor 0
        plan.warnings.append(f"Sell proceeds charges ke baad Rs.{net_proceeds:,.0f} bache - chhote exits mehnge pad rahe hain")
    # ultimate budget floor: free_cash itself
    if net_proceeds < -free_cash:
        net_proceeds = -free_cash

    # ---- 4. BUY side, cash ke andar rehte hue -------------------------
    # trim ke baad har target ki effective holding
    trimmed = {o.symbol: o.qty for o in sells if o.reason is Reason.TRIM}

    def size_buys(scale: float) -> tuple[list[PlannedOrder], float]:
        out: list[PlannedOrder] = []
        eff_slice = slice_value * scale
        for sym in targets:
            pos = held.get(sym)
            held_qty = (pos.total_qty - trimmed.get(sym, 0)) if pos else 0
            cur_val = held_qty * ltp[sym]
            # eff_slice 0 -> drift semantics: if held_qty>0 skip? but need to handle 0 target (deploy 0)
            if eff_slice > 0:
                drift = (cur_val - eff_slice) / eff_slice
            else:
                drift = float("inf") if cur_val > 0 else 0.0
                # eff_slice 0 means want zero - if we hold anything, we would have trimmed already
                # so for buys, if eff_slice==0 never buy
                if held_qty > 0:
                    continue
                # else eff_slice 0 and held 0 -> want 0, so skip buy (qty 0)
                continue
            if drift >= -band and held_qty > 0:
                continue                      # band ke andar -> haath mat lagao
            qty = _floor_qty(eff_slice - cur_val, ltp[sym])
            if qty <= 0:
                continue
            if qty * ltp[sym] < min_trade:
                plan.skipped.append(Skipped(sym, f"buy Rs.{qty*ltp[sym]:,.0f} < min trade Rs.{min_trade:,.0f} (scale {scale*100:.0f}%)"))
                continue
            lp = _limit_price(ltp[sym], Side.BUY, buf, circuit.get(sym) if circuit else None)
            if lp <= 0:
                plan.skipped.append(Skipped(sym, f"buy skip - LTP {ltp[sym]} invalid"))
                continue
            out.append(PlannedOrder(
                symbol=sym, security_id=security_ids[sym], side=Side.BUY,
                qty=qty, ref_price=ltp[sym],
                reason=Reason.TOPUP if held_qty > 0 else Reason.ENTRY,
                limit_price=lp,
                note=f"hold {held_qty} -> {held_qty + qty}"))
        try:
            bc = float(c_cfg.get("est_buy_cost_pct", 0.0004) or 0)
            if bc != bc or bc < 0 or bc > 0.1 or math.isinf(bc):
                bc = 0.0004
        except (TypeError, ValueError):
            bc = 0.0004
        cost = sum(o.value for o in out) * (1 + bc)
        return out, cost

    # ---- T+1 settlement logic ---------------------------------------
    # CNC delivery = T+1. Agar settlement_T1 true hai toh SELL ka paisa aaj BUY me nahi jodega.
    # Monthly rebalance: Last date ko SELL, 1st ko BUY.
    is_T1 = bool(x_cfg.get("settlement_T1", False)) if isinstance(x_cfg, dict) else False
    schedule = str(x_cfg.get("rebalance_schedule", "manual") or "manual").lower() if isinstance(x_cfg, dict) else "manual"
    
    if is_T1:
        # Aaj ke BUY sirf free_cash se honge, SELL proceeds kal ayenge
        budget = free_cash
        # Agar SELL ho rahe hain toh bata do ki BUY adhure rahenge aur kal pure honge
        if net_proceeds > 1:
            plan.warnings.append(
                f"T+1 SETTLEMENT (CNC): Aaj SELL se Rs.{net_proceeds:,.0f} milega par wo T+1 (kal) ko available hoga. "
                f"Aaj ke BUY sirf free cash Rs.{free_cash:,.0f} se honge. Baaki BUY agle trading day (1st ko) honge. "
                f"Isiliye aaj plan adhura dikhega — ye sach wala rebalance hai, turant wala nahi."
            )
            if schedule == "monthly_eom":
                plan.warnings.append(
                    f"MONTHLY REBALANCE: Last trading day (aaj) sirf SELL honge, BUY kal (1st trading day) honge jab T+1 paisa settle hoga."
                )
    else:
        budget = free_cash + net_proceeds
    buys, demand = size_buys(1.0)

    # 4a. paisa kam pad raha hai -> pehle overflow holding ko bhuna lo
    #     (11th slot ki priority top-10 se kam hai)
    # T+1 me overflow bech ke aaj BUY fund nahi karte - T+1 me paisa ayega kal
    ov_pos = held.get(overflow) if overflow else None
    if demand > budget and ov_pos and ov_pos.sellable > 0 and not is_T1:
        need = demand - budget
        # precise need: net proceeds = qty*price*(1 - sell_cost) - dp
        # solve qty = ceil((need + dp)/(price*(1-cost)))
        # previously +1 overshoot fixed to exact with ceil
        try:
            sc2 = float(c_cfg.get("est_sell_cost_pct", 0.0012) or 0)
        except (TypeError, ValueError):
            sc2 = 0.0012
        net_price = ltp[overflow] * (1 - sc2)
        if net_price > 0:
            # need includes dp later, so add dp to need
            required = (need + dp_per_scrip) / net_price
            release_qty = min(ov_pos.sellable, max(0, math.ceil(required)))
            # also show alternative floor+1 was overshoot; use ceil for precision
        else:
            release_qty = 0
        if release_qty > 0:
            lp = _limit_price(ltp[overflow], Side.SELL, buf, circuit.get(overflow) if circuit else None)
            if lp <= 0:
                release_qty = 0
            else:
                o = PlannedOrder(
                    symbol=overflow, security_id=security_ids[overflow],
                    side=Side.SELL, qty=release_qty, ref_price=ltp[overflow],
                    reason=Reason.OVERFLOW_TRIM,
                    limit_price=lp,
                    note="top-10 ko fund karne ke liye n+1 se nikaala")
                sells.append(o)
                budget += o.value * (1 - sc2)
                budget -= dp_per_scrip

    # 4b. ab bhi kam hai -> sab buys ko pro-rata chhota karo
    #     (equal weight bana rehta hai, bas slice chhota ho jaata hai)
    scale = 1.0
    best_buys, best_demand, best_scale = buys, demand, 1.0
    for _ in range(15):
        if demand <= budget:
            best_buys, best_demand, best_scale = buys, demand, scale
            break
        if demand <= 0 or budget <= 0:
            break
        new_scale = scale * (budget / demand) * 0.9995
        # keep best feasible so far (largest scale that fits)
        if demand <= budget:
            best_buys, best_demand, best_scale = buys, demand, scale
        scale = max(0.0, new_scale)
        if scale < 0.01:
            break
        buys, demand = size_buys(scale)
        if demand <= budget and demand > 0:
            best_buys, best_demand, best_scale = buys, demand, scale
    if best_demand <= budget and best_demand > 0:
        buys, demand, scale = best_buys, best_demand, best_scale
        if scale < 0.999:
            plan.warnings.append(
                f"Cash kam tha -- buys {scale*100:.0f}% slice par size kiye gaye "
                f"(equal weight bana hai, bas chhota).")
    elif demand > budget:
        # keep best feasible instead of discarding all
        if best_demand <= budget:
            buys, demand, scale = best_buys, best_demand, best_scale
            plan.warnings.append(
                f"Cash kam tha -- buys {scale*100:.0f}% slice par size kiye gaye "
                f"(equal weight bana hai, bas chhota).")
        else:
            # nothing fits even at tiny scale - keep empty but warn
            buys, demand = [], 0.0
            plan.warnings.append("Cash bilkul nahi bacha -- is hafte koi buy nahi.")
    elif scale < 0.999:
        plan.warnings.append(
            f"Cash kam tha -- buys {scale*100:.0f}% slice par size kiye gaye "
            f"(equal weight bana hai, bas chhota).")

    # ---- 5. n+1 overflow slot -----------------------------------------
    if overflow:
        ovp = ltp[overflow]
        ov_sold = sum(o.qty for o in sells if o.symbol == overflow)
        ov_held = (ov_pos.total_qty - ov_sold) if ov_pos else 0
        leftover = max(0.0, budget - demand)

        # YAHAN SAB SE BADA JAAL THA: overflow slot "bacha hua sab kuch"
        # utha leta tha. Matlab deploy budget aur cash reserve dono
        # chupchaap bekaar ho jaate -- user 60% bolta, app 100% laga deti.
        # Isliye n+1 ko bhi utni hi jagah milti hai jitni budget mein bachi hai.
        bought: dict[str, int] = {}
        for o in buys:
            bought[o.symbol] = bought.get(o.symbol, 0) + o.qty
        tgt_val_after = 0.0
        for s_ in targets:
            p_ = held.get(s_)
            q_ = (p_.total_qty if p_ else 0) - trimmed.get(s_, 0) + bought.get(s_, 0)
            tgt_val_after += q_ * ltp[s_]
        ov_room = max(0.0, investable - tgt_val_after)
        ov_room = min(ov_room, weight_cap_val)      # per-stock cap n+1 par bhi

        # FIX: ov_target_val includes existing held value which needs no buy cost
        # Correct: needed = max(0, ov_target_val - ov_held*ovp); qty = floor(needed / (ovp*(1+cost))) + ov_held
        try:
            bc_ov = float(c_cfg.get("est_buy_cost_pct", 0.0004) or 0)
            if bc_ov != bc_ov or bc_ov < 0 or bc_ov > 0.1 or math.isinf(bc_ov):
                bc_ov = 0.0004
        except (TypeError, ValueError):
            bc_ov = 0.0004
        ov_target_val = min(leftover + ov_held * ovp, ov_room)
        # overflow sizing fix: existing held part not inflated by buy cost
        held_val = ov_held * ovp
        if ov_target_val <= held_val:
            ov_target_qty = _floor_qty(ov_target_val, ovp)  # no buy cost for reducing/keeping
            # but if shrinking, delta negative handles trim
            if ov_target_val < held_val:
                ov_target_qty = _floor_qty(ov_target_val, ovp)
            else:
                ov_target_qty = ov_held
        else:
            need_val = ov_target_val - held_val
            buy_qty = _floor_qty(need_val, ovp * (1 + bc_ov))
            ov_target_qty = ov_held + buy_qty
        delta = ov_target_qty - ov_held

        if delta > 0 and delta * ovp >= min_trade:
            buys.append(PlannedOrder(
                symbol=overflow, security_id=security_ids[overflow],
                side=Side.BUY, qty=delta, ref_price=ovp,
                reason=Reason.OVERFLOW,
                limit_price=_limit_price(ovp, Side.BUY, buf, circuit.get(overflow) if circuit else None),
                note=f"bacha hua Rs.{min(leftover, ov_room):,.0f} n+1 mein"))
        elif delta < 0:
            # n+1 khud budget se bahar ja raha hai -> ghatana padega.
            # (Aisa tab hota hai jab deploy budget kam kar diya jaata hai.)
            if ov_pos is None or ov_held <= 0:
                # no existing position, nothing to trim - not a real skip, just no action
                if ov_held == 0 and -delta > 0:
                    # delta negative but held 0 means arithmetic wants 0, no position to trim
                    pass
                else:
                    plan.skipped.append(Skipped(
                        overflow, "n+1 ghatana tha par position nahi hai."))
            else:
                sellable_left = max(0, ov_pos.sellable - ov_sold)
                cut = min(-delta, sellable_left)
                if cut > 0 and cut * ovp >= min_trade:
                    lp2 = _limit_price(ovp, Side.SELL, buf, circuit.get(overflow) if circuit else None)
                    if lp2 > 0:
                        sells.append(PlannedOrder(
                            symbol=overflow, security_id=security_ids[overflow],
                            side=Side.SELL, qty=cut, ref_price=ovp,
                            reason=Reason.OVERFLOW_TRIM,
                            limit_price=lp2,
                            note="deploy budget ke andar laane ke liye n+1 ghataya"))
                    else:
                        plan.skipped.append(Skipped(overflow, f"n+1 trim limit_price invalid"))
                elif cut > 0:
                    plan.skipped.append(Skipped(
                        overflow, f"n+1 ko Rs.{cut*ovp:,.0f} ghatana tha par wo "
                                  f"min trade Rs.{min_trade:,.0f} se kam hai."))
                elif -delta > 0:
                    if sellable_left <= 0:
                        plan.skipped.append(Skipped(
                            overflow, "n+1 ghatana tha par sellable qty 0 hai "
                                      "(T+1 pending)."))
        elif leftover >= min_trade and ov_room >= min_trade:
            # check if overflow is restricted by room vs price
            if ovp > 0 and leftover >= ovp:
                plan.skipped.append(Skipped(
                    overflow, f"bacha Rs.{leftover:,.0f} -- n+1 ka 1 share bhi "
                              f"nahi aata / min trade se kam. Cash mein rakha."))
            else:
                plan.skipped.append(Skipped(
                    overflow, f"bacha Rs.{leftover:,.0f} -- n+1 overflow room Rs.{ov_room:,.0f} kam. Cash mein rakha."))

    plan.orders = _net_opposing(sells + buys, min_trade, plan)

    # ---- allocation completeness check ---------------------------------
    # If fewer BUYs than requested slots, warn explicitly with S.No context
    try:
        buys_after = [o for o in plan.orders if o.side is Side.BUY]
        # targets that are not covered by any order (neither buy nor hold-within-band)
        # Hold-within-band: held and drift within band => no order needed, not a miss
        # Missed = target symbol not in any order and not held-within-band and not skipped
        if slots > 0 and len(buys_after) < slots:
            # Check if skipped explains it
            skipped_syms = {s.symbol for s in plan.skipped if s.symbol in targets}
            # Also check held-within-band (no order but held correctly)
            # For now warn if skipped explains most misses
            if skipped_syms or len(buys_after) < len(targets):
                miss = slots - len(buys_after)
                # only warn if miss due to skipped/cash, not due to already correct holdings
                # Count holds that are within band (no order needed) - estimate
                holds_ok = 0
                for sym in targets:
                    pos = held.get(sym)
                    if pos and sym not in {o.symbol for o in plan.orders} and sym not in skipped_syms:
                        cur = pos.total_qty * ltp[sym]
                        drift = abs(cur - slice_value) / slice_value if slice_value>0 else 0
                        if drift <= band:
                            holds_ok += 1
                real_miss = miss - 0  # holds_ok are not miss, but our miss includes them? Let's compute
                # Effective expected buys = targets - holds_ok
                eff_expected = len(targets) - holds_ok
                if len(buys_after) < eff_expected:
                    skipped_list = ", ".join(sorted(skipped_syms)[:5]) + ("..." if len(skipped_syms)>5 else "")
                    plan.warnings.append(
                        f"Allocation adhura: {len(targets)} targets me se sirf {len(buys_after)} pe BUY bana ({eff_expected - len(buys_after)} miss). "
                        f"Skipped: {skipped_list if skipped_list else 'cash/min_trade/circuit'}. "
                        f"S.No column se dekho kaunse chhute. Cash badhao ya min_trade kam karo."
                    )
    except Exception as e:
        # never fail plan due to warning logic
        pass

    # ---- 6. risk gates -------------------------------------------------
    hint = ""
    if target_equity < reserve_cap - 1:
        hint = (f" Dhyan do: is hafte deploy budget {deploy_label} par set hai "
                f"(Rs.{target_equity:,.0f}) -- isi wajah se itna bik raha hai. "
                f"Poora nikaalna hai toh 'Sab Bech Do' use karo.")
    _apply_risk_gates(plan, cfg, ltp, churn_hint=hint)
    _check_circuits(plan, circuit or {}, cfg)
    return plan


# ----------------------------------------------------------------------
def build_liquidation_plan(
    *,
    run_id: str,
    holdings: Iterable[Position],
    free_cash: float,
    ltp: Mapping[str, float],
    security_ids: Mapping[str, str],
    cfg: Mapping,
    circuit: Mapping[str, CircuitInfo] | None = None,
) -> Plan:
    """SAB KUCH bech do -- poora portfolio cash mein.

    Watchlist se koi lena-dena nahi. Sirf jo hold hai wo bikta hai.

    Churn gate yahan JAAN-BUJH KAR nahi lagta -- churn 100% hona hi hai,
    wahi to maqsad hai. Baaki saare guards (single-order cap, circuit,
    liquidity) waise ke waise lagte hain.
    """
    x_cfg, c_cfg = cfg["execution"], cfg["costs"]
    buf = float(x_cfg["limit_buffer_pct"])
    plan = Plan(run_id=run_id, nav=0.0, free_cash=free_cash, slice_value=0.0)
    plan.is_liquidation = True
    plan.target_equity = 0.0
    plan.deploy_label = "sab cash mein"

    # aggregate duplicates similarly
    held = {}
    for h in holdings:
        if h.symbol in held:
            prev = held[h.symbol]
            tot = prev.total_qty + h.total_qty
            avail = prev.available_qty + h.available_qty
            avg = (prev.avg_price * prev.total_qty + h.avg_price * h.total_qty) / tot if tot else 0
            held[h.symbol] = Position(symbol=h.symbol, security_id=prev.security_id,
                                      total_qty=tot, available_qty=min(avail, tot), avg_price=avg)
        else:
            held[h.symbol] = h
    if not held:
        plan.blockers.append(
            "Portfolio mein kuch hai hi nahi -- bechne ko kuch nahi.")
        return plan

    missing_ltp = [s for s in sorted(held) if ltp.get(s, 0) is None or ltp.get(s, 0) <= 0]
    missing_sec = [s for s in sorted(held) if s not in security_ids or not security_ids[s]]
    if missing_ltp:
        plan.blockers.append(
            "In scrip ka live price nahi mila, isliye bech nahi sakte: "
            + ", ".join(missing_ltp))
    if missing_sec:
        plan.blockers.append(
            "In scrip ka securityId nahi mila: " + ", ".join(missing_sec))
    if plan.blockers:
        return plan

    try:
        buf = float(x_cfg.get("limit_buffer_pct", 0.003) or 0.003)
        if buf != buf or buf < 0 or math.isinf(buf):
            buf = 0.003
    except (TypeError, ValueError):
        buf = 0.003
    plan.nav = sum(h.total_qty * ltp[h.symbol] for h in held.values() if ltp.get(h.symbol,0)>0) + free_cash

    stuck = 0.0
    for sym, pos in sorted(held.items()):
        qty = pos.sellable
        lp = _limit_price(ltp[sym], Side.SELL, buf, circuit.get(sym) if circuit else None)
        if lp <= 0:
            plan.skipped.append(Skipped(sym, f"SAB BECHO skip - limit_price invalid LTP {ltp[sym]}"))
            stuck += pos.total_qty * ltp[sym]
            continue
        if qty <= 0:
            stuck += pos.total_qty * ltp[sym]
            plan.skipped.append(Skipped(
                sym, f"{pos.total_qty} share hain par DP-free 0 -- T+1 settle "
                     f"hone ke baad hi bikenge"))
            continue
        if pos.total_qty > pos.sellable:
            left = pos.total_qty - pos.sellable
            stuck += left * ltp[sym]
            plan.warnings.append(
                f"{sym}: {pos.total_qty} mein se sirf {pos.sellable} bik "
                f"rahe hain -- {left} abhi unsettled hain (T+1). "
                f"Wo agle din bechne padenge.")
        plan.orders.append(PlannedOrder(
            symbol=sym, security_id=security_ids[sym], side=Side.SELL,
            qty=qty, ref_price=ltp[sym], reason=Reason.EXIT,
            limit_price=lp,
            note="SAB BECHO"))

    if not plan.orders:
        plan.blockers.append(
            "Ek bhi share abhi bech nahi sakte -- sab T+1 mein atke hain. "
            "Kal try karo.")
        return plan

    sell_value = sum(o.value for o in plan.orders)
    plan.warnings.insert(0, (
        f"POORA PORTFOLIO BIK RAHA HAI: {len(plan.orders)} scrip, "
        f"Rs.{sell_value:,.0f}. Iske baad tumhara paisa 100% cash mein hoga."
        + (f" (Rs.{stuck:,.0f} abhi bhi atka rahega -- unsettled.)"
           if stuck > 0 else "")))

    _apply_risk_gates(plan, cfg, ltp, skip_churn=True)
    _check_circuits(plan, circuit or {}, cfg)
    return plan


# ----------------------------------------------------------------------
def _net_opposing(orders: list[PlannedOrder], min_trade: float,
                  plan: Plan) -> list[PlannedOrder]:
    """Ek hi scrip ka BUY aur SELL dono? Dono ko jodo, ek hi order banao.

    Aisa tab hota hai jab n+1 slot ko top-N fund karne ke liye becha jaata
    hai aur baad mein bacha hua paisa usi mein wapas jaata hai. Dono order
    bhejne ka matlab:
      * STT dono taraf, stamp duty, DP charge, dohra slippage -- sab bekaar
      * bina wajah gain/loss book ho jaata hai (STCG)
      * same-day sell+buy delivery ke bajaye intraday treat ho sakta hai

    Net karke ek order bhejna har haal mein sasta aur saaf hai. Cash ka
    hisaab bilkul same rehta hai (buy_value - sell_value nahi badalta).
    """
    by_sym: dict[str, list[PlannedOrder]] = {}
    for o in orders:
        by_sym.setdefault(o.symbol, []).append(o)

    out: list[PlannedOrder] = []
    for sym, grp in by_sym.items():
        if len(grp) == 1:
            out.extend(grp)
            continue
        buys = [o for o in grp if o.side is Side.BUY]
        sells = [o for o in grp if o.side is Side.SELL]
        bq = sum(o.qty for o in buys)
        sq = sum(o.qty for o in sells)

        if not (buys and sells):
            # Ek hi taraf ke do order. Broker ko DO order bhejna kabhi theek
            # nahi -- do baar charges, do baar slippage, aur reconcile karte
            # waqt ye samajhna mushkil ho jaata hai ki hua kya. Jod do.
            src = grp[0]
            # validate limit_price consistency - if divergent, use weighted
            total_q = bq or sq
            # re-check min_trade after aggregation: two dust 400+400=800 should now pass
            # but individual were already dropped before netting, so this is for future if caller aggregates earlier
            out.append(PlannedOrder(
                symbol=sym, security_id=src.security_id, side=src.side,
                qty=total_q, ref_price=src.ref_price, reason=src.reason,
                limit_price=src.limit_price,
                note=f"{len(grp)} order jode gaye -- {src.note}"))
            continue

        net = bq - sq
        px = grp[0].ref_price
        plan.warnings.append(
            f"{sym}: BUY {bq} aur SELL {sq} dono ban rahe the -- net karke "
            f"ek hi order bheja ja raha hai ({abs(net)} "
            f"{'BUY' if net > 0 else 'SELL' if net < 0 else 'kuch nahi'}). "
            f"Isse faltu charges aur ek bekaar ka STCG event bach gaya.")

        if net == 0:
            plan.skipped.append(Skipped(sym, "BUY aur SELL barabar the -- dono cancel"))
            continue
        # min_trade par SIRF net BUY drop karo. Net SELL kabhi mat girao --
        # baaki buys usi paise ke bharose size hue hain; sell hata denge toh
        # cash kam pad jaayega aur order reject honge.
        # BUT warn if net SELL is dust and costs exceed value
        if net < 0 and abs(net) * px < min_trade:
            # dust sell - still execute per comment, but warn about profitability
            est_cost = abs(net) * px * 0.0012 + 14.75
            if est_cost > abs(net) * px * 0.3:
                plan.warnings.append(f"{sym} net SELL Rs.{abs(net)*px:,.0f} dust par charges Rs.{est_cost:,.0f} - mehenga")
        if net > 0 and net * px < min_trade:
            plan.skipped.append(Skipped(
                sym, f"net BUY Rs.{net*px:,.0f} < min trade "
                     f"Rs.{min_trade:,.0f} -- chhod diya"))
            continue

        src = (buys if net > 0 else sells)[0]
        out.append(PlannedOrder(
            symbol=sym, security_id=src.security_id,
            side=Side.BUY if net > 0 else Side.SELL,
            qty=abs(net), ref_price=px, reason=src.reason,
            limit_price=src.limit_price,
            note=f"net of BUY {bq} / SELL {sq}"))

    # SELL pehle, phir BUY -- executor isi order par chalta hai
    out.sort(key=lambda o: 0 if o.side is Side.SELL else 1)
    return out


# ----------------------------------------------------------------------
def _check_circuits(plan: Plan, circuit: Mapping[str, CircuitInfo],
                    cfg: Mapping) -> None:
    """Circuit par lagi scrip ka order bharega hi nahi -- doosri taraf koi
    hai hi nahi. Order phir bhi jaata hai (tumhara choice), par plan mein
    saaf dikh jaata hai.

    Narrow band (2%/5%) alag warning hai: aisa scrip aksar surveillance
    (ASM/GSM) mein hota hai ya bahut illiquid hota hai."""
    if not circuit:
        return
    try:
        narrow = float(cfg["risk"].get("narrow_band_warn_pct", 10.0) or 10.0)
        if narrow != narrow or narrow <= 0 or math.isinf(narrow):
            narrow = 10.0
    except (TypeError, ValueError):
        narrow = 10.0

    for o in plan.orders:
        ci = circuit.get(o.symbol)
        if not ci:
            continue
        if o.side is Side.BUY and ci.at_upper:
            plan.warnings.append(
                f"!! {o.symbol} UPPER CIRCUIT par hai (Rs.{ci.upper:,.2f}) -- "
                f"BUY order bharne ke chance na ke barabar hain. Bechne wala "
                f"hi koi nahi.")
        elif o.side is Side.SELL and ci.at_lower:
            plan.warnings.append(
                f"!! {o.symbol} LOWER CIRCUIT par hai (Rs.{ci.lower:,.2f}) -- "
                f"SELL order nahi bharega. Position aaj nikal nahi paayegi.")

    band_flagged = []
    for o in plan.orders:
        ci = circuit.get(o.symbol)
        b = ci.band_pct if ci else None
        if b is not None and b <= narrow and o.symbol not in band_flagged:
            band_flagged.append(f"{o.symbol} ({b:.0f}%)")
    if band_flagged:
        plan.warnings.append(
            f"Chhote circuit band wale scrip: {', '.join(band_flagged)}. "
            f"Aise scrip aksar ASM/GSM surveillance mein hote hain -- "
            f"slippage aur fill dono kharab rehte hain.")

    # ---- liquidity / impact cost -------------------------------------
    # Order aaj ke traded value ka jitna bada hissa hoga, price utna hi
    # tumhare khilaaf hilega. Ye backtest mein KABHI nahi dikhta --
    # wahan closing price par muft fill maan liya jaata hai.
    try:
        limit = float(cfg["risk"].get("max_pct_of_traded_value", 0.05) or 0.05)
        if limit != limit or limit <= 0 or limit > 1 or math.isinf(limit):
            limit = 0.05
    except (TypeError, ValueError):
        limit = 0.05
    heavy = []
    for o in plan.orders:
        ci = circuit.get(o.symbol)
        if not ci or ci.traded_value <= 0:
            continue
        try:
            share = o.value / ci.traded_value if ci.traded_value else 0
        except (TypeError, ZeroDivisionError):
            continue
        if share > limit:
            heavy.append((share, o, ci))
    for share, o, ci in sorted(heavy, key=lambda t: -t[0]):
        plan.warnings.append(
            f"!! LIQUIDITY: {o.symbol} {o.side.value} Rs.{o.value:,.0f} = "
            f"aaj ke traded value (Rs.{ci.traded_value:,.0f}) ka "
            f"{share*100:.0f}%. Itna bada order price apne khilaaf hilata hai; "
            f"nikalne mein kai din lag sakte hain.")


# ----------------------------------------------------------------------
def _apply_risk_gates(plan: Plan, cfg: Mapping, ltp: Mapping[str, float],
                      skip_churn: bool = False, churn_hint: str = "") -> None:
    r = cfg["risk"]
    # Penny guard FIRST so single-order cap not inflated by penny buys that will be removed
    try:
        floor_price = float(r.get("min_price_inr", 10) or 10)
        if floor_price != floor_price or floor_price < 0 or math.isinf(floor_price):
            floor_price = 10
    except (TypeError, ValueError):
        floor_price = 10
    for o in list(plan.orders):
        if o.side is Side.BUY and o.ref_price < floor_price:
            plan.orders.remove(o)
            plan.skipped.append(Skipped(
                o.symbol, f"price Rs.{o.ref_price} < min Rs.{floor_price} "
                          f"(penny stock guard -- buy nahi karenge)"))

    try:
        cap = float(r.get("max_single_order_value_inr", 2000000) or 2000000)
        if cap != cap or cap < 0 or math.isinf(cap):
            cap = 2000000
    except (TypeError, ValueError):
        cap = 2000000
    over = [o for o in plan.orders if o.value > cap]
    if over:
        biggest = max(o.value for o in over)
        # 10% margin ke saath agla round number suggest karo
        want = biggest * 1.1
        try:
            step = 10 ** max(0, len(str(int(want))) - 2) if want > 0 else 1
            suggest = int((want // step + 1) * step)
        except (ValueError, OverflowError):
            suggest = int(want) + 1000
        for o in over:
            plan.blockers.append(
                f"{o.symbol} {o.side.value} Rs.{o.value:,.0f} > single-order cap "
                f"Rs.{cap:,.0f}.")
        plan.blockers.append(
            f"^ {len(over)} order cap se bade hain. Tumhari capital ke hisaab se "
            f"ye normal hai -- config.yaml mein "
            f"max_single_order_value_inr: {suggest:,} kar do "
            f"(abhi {int(cap):,} hai). Ye guard tabhi kaam ka hai jab wo "
            f"tumhare normal order size se thoda upar ho.".replace(",", ","))

    # Gate SELL side par hai. Buy side ko cash constraint pehle hi rok
    # deta hai, aur pehle deployment mein churn zero hota hai (kuch becha
    # nahi) -- warna app ka pehla hi run block ho jaata.
    try:
        max_to = float(r.get("max_turnover_pct", 0.85) or 0.85)
        if max_to != max_to or max_to < 0 or max_to > 5 or math.isinf(max_to):
            max_to = 0.85
    except (TypeError, ValueError):
        max_to = 0.85
    if not skip_churn and plan.churn_pct > max_to:
        plan.blockers.append(
            f"Portfolio ka {plan.churn_pct*100:.0f}% bik raha hai, cap "
            f"{max_to*100:.0f}% hai. Kuch galat lag raha hai -- plan review "
            f"karo. (Genuine ho toh config mein max_turnover_pct badhao.)"
            + churn_hint)

    for o in plan.orders:
        if o.qty <= 0:
            plan.blockers.append(f"{o.symbol}: qty {o.qty} -- bug hai, ruko.")
        if o.ref_price is None or o.ref_price != o.ref_price or o.ref_price <= 0 or math.isinf(o.ref_price):
            plan.blockers.append(f"{o.symbol}: ref_price invalid {o.ref_price} -- bug hai, ruko.")
        if o.limit_price is not None and (o.limit_price != o.limit_price or math.isinf(o.limit_price) or o.limit_price < 0):
            plan.blockers.append(f"{o.symbol}: limit_price invalid -- bug hai, ruko.")
