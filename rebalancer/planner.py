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
    if price <= 0:
        return 0
    return max(0, math.floor(value / price))


def _limit_price(ltp: float, side: Side, buffer_pct: float) -> float:
    """BUY thoda upar, SELL thoda neeche -- fill ki guarantee badhti hai
    bina market order ke slippage ke."""
    mult = (1 + buffer_pct) if side is Side.BUY else (1 - buffer_pct)
    return round(ltp * mult, 2)          # NSE tick size = 0.05 for most,
                                          # 0.01 allowed; 2 decimals safe hai


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
    if str(raw).strip().lower() == "auto":
        use_of = bool(p_cfg.get("use_overflow_slot"))
        n = list_len - 1 if (use_of and list_len > 1) else list_len
        return max(1, n)
    return max(1, int(raw))


def resolve_exit_rank(p_cfg: Mapping, list_len: int, n: int) -> int:
    """Rank isse neeche gaya toh naam bahar. auto = n (strict top-n)."""
    raw = p_cfg.get("exit_rank_threshold", "auto")
    if str(raw).strip().lower() == "auto":
        return n
    return max(n, int(raw))


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
    mode = str(p_cfg.get("deploy_mode", "all")).strip().lower()

    if mode in ("pct", "percent", "percentage", "%"):
        try:
            raw = float(p_cfg.get("deploy_pct", 1.0) or 0.0)
        except (TypeError, ValueError):
            raw = 1.0
        if raw != raw or raw < 0:              # NaN / negative
            raw = 0.0
        # 0.60 = 60%, aur 60 bhi = 60%. Fraction kabhi 1 se upar nahi ja
        # sakta, aur 1% deploy karne ka koi matlab nahi -- isliye ye
        # heuristic safe hai.
        pct = raw / 100.0 if raw > 1.0 else raw
        pct = min(max(pct, 0.0), 1.0)
        return nav * pct, f"NAV ka {pct * 100:.4g}%"

    if mode in ("amount", "amt", "rupees", "inr", "fixed"):
        try:
            amt = float(p_cfg.get("deploy_amount", 0) or 0.0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt != amt or amt < 0:
            amt = 0.0
        return amt, f"Rs.{_inr_label(amt)} (fixed)"

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
    p_cfg, c_cfg, r_cfg, x_cfg = cfg["portfolio"], cfg["costs"], cfg["risk"], cfg["execution"]
    band = float(p_cfg["drift_band_pct"])
    buf = float(x_cfg["limit_buffer_pct"])

    held: dict[str, Position] = {h.symbol: h for h in holdings}
    plan = Plan(run_id=run_id, nav=0.0, free_cash=free_cash, slice_value=0.0)

    # ---- 0. watchlist sanity ------------------------------------------
    wl = sorted(watchlist, key=lambda t: t.rank)

    # n_stocks / exit_rank_threshold "auto" ho sakte hain -- tab list ka
    # size hi decide karta hai. Isse 5 naam ho ya 50, app waise hi chalti hai.
    n = resolve_n(p_cfg, len(wl))
    exit_rank = resolve_exit_rank(p_cfg, len(wl), n)
    symbols = [t.symbol for t in wl]
    if len(set(symbols)) != len(symbols):
        plan.blockers.append("Watchlist mein duplicate symbol hai.")
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
        parked = sorted(set(held) & (keep_zone - set(targets)))
        if parked:
            pval = sum(held[s].total_qty * ltp[s] for s in parked if s in ltp)
            plan.warnings.append(
                f"{len(parked)} naam keep-zone mein hain par top-{n} mein nahi: "
                f"{', '.join(parked)}. Inhe na becha ja raha hai na inka weight "
                f"adjust ho raha hai (exit_rank_threshold {exit_rank} > n_stocks "
                f"{n}). Inka Rs.{pval:,.0f} phansa rehta hai aur top-{n} utna hi "
                f"kam funded rahega.")

    # --- n se kam naam aaye toh? ---------------------------------------
    #   full        -> jitne naam hain unhi mein poora paisa (100% invested)
    #   fixed_slots -> hamesha NAV/n, baaki cash (backtest jaisa)
    mode = str(p_cfg.get("partial_list_mode", "full")).lower()
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
    for s in sorted(needed):
        if s not in security_ids:
            plan.blockers.append(f"{s}: securityId nahi mila (scrip master check karo).")
        elif ltp.get(s, 0) <= 0:
            plan.blockers.append(f"{s}: LTP nahi mila / zero hai.")
    if plan.blockers:
        return plan

    # ---- 2. NAV aur slice ---------------------------------------------
    holdings_value = sum(h.total_qty * ltp[h.symbol] for h in held.values())
    nav = holdings_value + free_cash
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
    reserve_cap = nav * (1 - float(p_cfg["cash_reserve_pct"]))
    deploy_cap, deploy_label = resolve_deploy(p_cfg, nav)
    plan.deploy_label = deploy_label

    if deploy_cap > reserve_cap:
        if deploy_cap > nav:
            plan.warnings.append(
                f"Deploy budget ({deploy_label}) NAV Rs.{nav:,.0f} se bada hai -- "
                f"itna paisa hai hi nahi. Jitna hai utna hi laga rahe hain.")
        deploy_cap = reserve_cap

    target_equity = max(0.0, deploy_cap)
    plan.target_equity = target_equity

    # Keep-zone mein padi hui (bina manage ki) holdings bhi stocks hi hain --
    # deploy budget mein wo bhi ginti hain, warna "60% lagao" jhooth ho jaata.
    parked_now = sorted(set(held) & (keep_zone - set(targets)) - ({overflow} if overflow else set()))
    parked_value = sum(held[s].total_qty * ltp[s] for s in parked_now)

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
    weight = slice_value / nav
    cap = p_cfg.get("max_weight_per_stock_pct")
    weight_cap_val = nav * float(cap) if cap else float("inf")
    if cap and weight > float(cap):
        slice_value = weight_cap_val
        plan.slice_value = slice_value
        plan.warnings.append(
            f"Har stock {weight*100:.0f}% banta tha -- cap "
            f"{float(cap)*100:.0f}% laga diya, baaki cash mein rahega.")
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

    min_trade = max(float(c_cfg["min_trade_value_inr"]),
                    slice_value * float(c_cfg["min_trade_pct_of_slice"]))

    # ---- 3. SELL side: EXIT + TRIM ------------------------------------
    sells: list[PlannedOrder] = []

    # 3a. list se bahar -> poora exit (overflow slot ko chhod kar)
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
        sells.append(PlannedOrder(
            symbol=sym, security_id=security_ids[sym], side=Side.SELL, qty=qty,
            ref_price=ltp[sym], reason=Reason.EXIT,
            limit_price=_limit_price(ltp[sym], Side.SELL, buf),
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
            limit_price=_limit_price(ltp[sym], Side.SELL, buf),
            note=f"weight {cur_val/nav*100:.1f}% -> target {slice_value/nav*100:.1f}%"))

    sell_value = sum(o.value for o in sells)
    net_proceeds = sell_value * (1 - float(c_cfg["est_sell_cost_pct"]))
    net_proceeds -= float(c_cfg["dp_charge_per_scrip_inr"]) * len(sells)

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
            drift = (cur_val - eff_slice) / eff_slice if eff_slice > 0 else 0.0
            if drift >= -band and held_qty > 0:
                continue                      # band ke andar -> haath mat lagao
            qty = _floor_qty(eff_slice - cur_val, ltp[sym])
            if qty <= 0:
                continue
            if qty * ltp[sym] < min_trade:
                continue
            out.append(PlannedOrder(
                symbol=sym, security_id=security_ids[sym], side=Side.BUY,
                qty=qty, ref_price=ltp[sym],
                reason=Reason.TOPUP if held_qty > 0 else Reason.ENTRY,
                limit_price=_limit_price(ltp[sym], Side.BUY, buf),
                note=f"hold {held_qty} -> {held_qty + qty}"))
        cost = sum(o.value for o in out) * (1 + float(c_cfg["est_buy_cost_pct"]))
        return out, cost

    budget = free_cash + net_proceeds
    buys, demand = size_buys(1.0)

    # 4a. paisa kam pad raha hai -> pehle overflow holding ko bhuna lo
    #     (11th slot ki priority top-10 se kam hai)
    ov_pos = held.get(overflow) if overflow else None
    if demand > budget and ov_pos and ov_pos.sellable > 0:
        need = demand - budget
        release_qty = min(ov_pos.sellable,
                          _floor_qty(need / (1 - float(c_cfg["est_sell_cost_pct"])),
                                     ltp[overflow]) + 1)
        if release_qty > 0:
            o = PlannedOrder(
                symbol=overflow, security_id=security_ids[overflow],
                side=Side.SELL, qty=release_qty, ref_price=ltp[overflow],
                reason=Reason.OVERFLOW_TRIM,
                limit_price=_limit_price(ltp[overflow], Side.SELL, buf),
                note="top-10 ko fund karne ke liye n+1 se nikaala")
            sells.append(o)
            budget += o.value * (1 - float(c_cfg["est_sell_cost_pct"]))
            budget -= float(c_cfg["dp_charge_per_scrip_inr"])

    # 4b. ab bhi kam hai -> sab buys ko pro-rata chhota karo
    #     (equal weight bana rehta hai, bas slice chhota ho jaata hai)
    scale = 1.0
    for _ in range(12):
        if demand <= budget:
            break
        scale *= (budget / demand) * 0.9995 if demand > 0 else 0.0
        buys, demand = size_buys(scale)
    if demand > budget:
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

        ov_target_val = min(leftover + ov_held * ovp, ov_room)
        ov_target_qty = _floor_qty(
            ov_target_val, ovp * (1 + float(c_cfg["est_buy_cost_pct"])))
        delta = ov_target_qty - ov_held

        if delta > 0 and delta * ovp >= min_trade:
            buys.append(PlannedOrder(
                symbol=overflow, security_id=security_ids[overflow],
                side=Side.BUY, qty=delta, ref_price=ovp,
                reason=Reason.OVERFLOW,
                limit_price=_limit_price(ovp, Side.BUY, buf),
                note=f"bacha hua Rs.{min(leftover, ov_room):,.0f} n+1 mein"))
        elif delta < 0:
            # n+1 khud budget se bahar ja raha hai -> ghatana padega.
            # (Aisa tab hota hai jab deploy budget kam kar diya jaata hai.)
            sellable_left = (ov_pos.sellable - ov_sold) if ov_pos else 0
            cut = min(-delta, max(0, sellable_left))
            if cut > 0 and cut * ovp >= min_trade:
                sells.append(PlannedOrder(
                    symbol=overflow, security_id=security_ids[overflow],
                    side=Side.SELL, qty=cut, ref_price=ovp,
                    reason=Reason.OVERFLOW_TRIM,
                    limit_price=_limit_price(ovp, Side.SELL, buf),
                    note="deploy budget ke andar laane ke liye n+1 ghataya"))
            elif cut > 0:
                plan.skipped.append(Skipped(
                    overflow, f"n+1 ko Rs.{cut*ovp:,.0f} ghatana tha par wo "
                              f"min trade Rs.{min_trade:,.0f} se kam hai."))
            elif -delta > 0:
                plan.skipped.append(Skipped(
                    overflow, "n+1 ghatana tha par sellable qty 0 hai "
                              "(T+1 pending)."))
        elif leftover >= min_trade and ov_room >= min_trade:
            plan.skipped.append(Skipped(
                overflow, f"bacha Rs.{leftover:,.0f} -- n+1 ka 1 share bhi "
                          f"nahi aata / min trade se kam. Cash mein rakha."))

    plan.orders = _net_opposing(sells + buys, min_trade, plan)

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

    held = {h.symbol: h for h in holdings}
    if not held:
        plan.blockers.append(
            "Portfolio mein kuch hai hi nahi -- bechne ko kuch nahi.")
        return plan

    missing = [s for s in sorted(held) if ltp.get(s, 0) <= 0]
    if missing:
        plan.blockers.append(
            "In scrip ka live price nahi mila, isliye bech nahi sakte: "
            + ", ".join(missing))
        return plan

    plan.nav = sum(h.total_qty * ltp[h.symbol] for h in held.values()) + free_cash

    stuck = 0.0
    for sym, pos in sorted(held.items()):
        qty = pos.sellable
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
            limit_price=_limit_price(ltp[sym], Side.SELL, buf),
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
            out.append(PlannedOrder(
                symbol=sym, security_id=src.security_id, side=src.side,
                qty=bq or sq, ref_price=src.ref_price, reason=src.reason,
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
    narrow = float(cfg["risk"].get("narrow_band_warn_pct", 10.0))

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
    limit = float(cfg["risk"].get("max_pct_of_traded_value", 0.05))
    heavy = []
    for o in plan.orders:
        ci = circuit.get(o.symbol)
        if not ci or ci.traded_value <= 0:
            continue
        share = o.value / ci.traded_value
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

    cap = float(r["max_single_order_value_inr"])
    over = [o for o in plan.orders if o.value > cap]
    if over:
        biggest = max(o.value for o in over)
        # 10% margin ke saath agla round number suggest karo
        want = biggest * 1.1
        step = 10 ** max(0, len(str(int(want))) - 2)
        suggest = int((want // step + 1) * step)
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

    # Penny-stock guard SIRF buy side par. SELL kabhi block nahi karna --
    # jo scrip pehle se hold hai usse nikalne ka raasta hamesha khula rahe,
    # warna position phansi reh jaati hai.
    floor_price = float(r["min_price_inr"])
    for o in list(plan.orders):
        if o.side is Side.BUY and o.ref_price < floor_price:
            plan.orders.remove(o)
            plan.skipped.append(Skipped(
                o.symbol, f"price Rs.{o.ref_price} < min Rs.{floor_price} "
                          f"(penny stock guard -- buy nahi karenge)"))

    # Gate SELL side par hai. Buy side ko cash constraint pehle hi rok
    # deta hai, aur pehle deployment mein churn zero hota hai (kuch becha
    # nahi) -- warna app ka pehla hi run block ho jaata.
    max_to = float(r["max_turnover_pct"])
    if not skip_churn and plan.churn_pct > max_to:
        plan.blockers.append(
            f"Portfolio ka {plan.churn_pct*100:.0f}% bik raha hai, cap "
            f"{max_to*100:.0f}% hai. Kuch galat lag raha hai -- plan review "
            f"karo. (Genuine ho toh config mein max_turnover_pct badhao.)"
            + churn_hint)

    for o in plan.orders:
        if o.qty <= 0:
            plan.blockers.append(f"{o.symbol}: qty {o.qty} -- bug hai, ruko.")
