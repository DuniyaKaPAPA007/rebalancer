# Weekly Equal-Weight Rebalancer — Kaise Kaam Karti Hai

> **One line:** Trendlyne screener se `watchlist.csv` leke har rebalance pe portfolio ko equal-weight (`NAV / n`) pe wapas lana — DhanHQ v2 se live, T+1 settlement ke saath, bina wash-trade ke.

---

## 1. Overview

* **Purpose:** Momentum/Golden-Cross jaise screener ke top-10 stocks me har period equal paisa lagana. Har rebalance pe jo bahar ho gaya usko `EXIT`, jo naya hai `ENTRY`, jo reh gaya usko `TRIM/TOPUP` se exact weight pe lana.
* **Interface:** do tareeke
  * **Web:** `START-APP.bat` → `http://127.0.0.1:8770` (Connect → Watchlist → Plan → Execute → Portfolio)
  * **CLI:** `python -m rebalancer.cli plan` → `python -m rebalancer.cli execute --approve`
* **Modes:** `paper` (nakli ₹1 cr, koi order nahi jaata, backtest jaisa) vs `live` (Dhan credentials se asli NAV, rehearsal ke baad `haan`/`sab bech do` se real orders)
* **Source of truth:** `Dhan holdings API` — quantity hamesha wahi se, `runs.db` sirf audit.

---

## 2. End-to-End Flow (Monthly EOM + T+1)

```
Trendlyne CSV (25 rows) 
  → watchlist.py (auto-detect: SIMPLE/SCREENER/BACKTEST) 
  → instruments.py (symbol/ISIN → securityId via .cache/scrip_master.csv)
  → Dhan API (holdings + fundlimit + marketfeed/quote + fallback yahoo/nse)
  → planner.py:build_plan() [ PURE: (holdings, ltp, watchlist, config) → Plan ]
  → report.py (human text + cost)
  → executor.py (SELL → T+1 settle → BUY)
  → store.py (SQLite runs.db + plans/*.json)
```

**Monthly kalender (settlement_T1: true, rebalance_schedule: monthly_eom):**

* **31st (Last trading day, EOM): SELL Phase**
  * `free_cash` = Dhan se (jaise ₹50k)
  * `SELL`: `OLD1, OLD2` (list se bahar) + `TRIM` (overweight)
  * `BUY` is din **sirf** `free_cash` se, `SELL` ke ₹2L ka proceeds **kal** ayega → warning: `T+1 SETTLEMENT: Aaj BUY sirf free_cash se, baaki kal`
  * Executor LIVE pe aaj **sirf SELL** bhejta hai, BUY ko `AWAITING_T1_BUY` pe rokta hai (dry_run me dono preview dikhata hai)

* **1st (Next trading day, T+1): BUY Phase**
  * Subah `free_cash = kal ka 50k + sell settled 2L = 2.5L`
  * Wahi watchlist se naya plan → ab `BUY` (ENTRY/TOPUP) pure paise se, `SELL` minimal
  * Executor ab BUY bhejta hai

> Agar `settlement_T1: false` kar do (intraday) toh purana instant `budget = free_cash + sell_proceeds` same day lagta hai — CNC ke liye galat.

---

## 3. Core Logic — `rebalancer/planner.py` (Pure Diff Engine)

**`NAV = Σ(holdings_qty * LTP) + free_cash`**

1. **`n = resolve_n(list_len)`** — `n_stocks: auto` = `len(list)-1` agar `overflow` on (10+1), warna `len(list)`. `exit_rank_threshold: auto` = `n` (strict top-n).
2. **`slice = investable / slots`** — `investable = target_equity - parked_keep_zone_value`
   * `target_equity` = `min( NAV*(1-cash_reserve), deploy_budget )`  
     `deploy_budget`: `all` = NAV, `pct` = `NAV*pct` (0.6 ya 60 dono 60%), `amount` = fixed Rs.
3. **`slots`** = `len(targets)` if `partial_list_mode: full` (4 naam → 25% each, 100% invested) else `n` (40% invested, 60% cash).
4. **`SELL`:**
   * `EXIT`: `keep_zone` aur `overflow` ke bahar → `sellable = min(total, available)` (T+1 pending nahi bechte)
   * `TRIM`: target me overweight aur `drift = (cur-slice)/slice > band` → `excess_qty = floor((cur-slice)/price)` agar `> min_trade` (max(₹500, slice*3%)) warna `Skipped`
5. **`net_proceeds = sell_value*(1 - est_sell_cost) - DP*len(sells)`** (floor at `0` if dust eats it)
6. **`BUY: size_buys(scale)`**
   * `eff_slice = slice*scale`, `held_qty = total - trimmed`
   * `drift >= -band` aur `held>0` → band ke andar → skip
   * `qty = floor(eff_slice - cur_val)/price` agar `>= min_trade`
   * Limit clamp circuit ke andar + tick 0.05 `planner.py:37`
7. **Cash shortage:** `budget` = `free_cash` (T1) ya `free_cash+proceeds` (same-day). Pehle `overflow (11th)` bech ke fund karo (ceil exact, `+1` overshoot fix), phir pro-rata `scale *= budget/demand*0.9995` 15 iter, best feasible rakho — pura discard nahi (pehle 0 kar deta tha).
8. **Overflow `n+1`:** `ov_room = investable - tgt_val_after` (up to cap), `held_val` ka buy-cost nahi lagta — `need = target - held`, `buy_qty = floor(need / price*(1+cost))` (pehle pura target ko cost se divide karke under-buy hota tha).
9. **`_net_opposing`:** same symbol pe `BUY 500 + SELL 200` → `net BUY 300` ek order, STCG/charges bachao; dust net BUY `<min_trade` skip, net SELL warn.
10. **Warnings:** allocation adhura (`buys < slots` + skipped list + `S.No`), concentration (`>25%` → cap suggest), pricest slice `<15 shares` precision, stale LTP `>5%`, microcap `<2000cr`, narrow band `<=10%`, liquidity `>1%` traded value.
11. **Minimum Capital `min_capital_for_targets()` — 8/10 ka fix:**
    * Har top-n ka `price` dekho: `JSWDULUX 3118` → 1 share ke liye `3118`, `IDEA 13.94` → `₹500` ke liye `36 share = 501`
    * `min_slice = max(price_i if price>=500 else ceil(500/price)*price)` → cheap stock decide karta hai
    * Golden Cross top10: `min_slice=3118`, `min_investable=31188`, `min_nav≈31566` (1% reserve + 0.2% cost)
    * Plan ke baad `allocated = target_equity` vs `need` compare → `allocated < need` → warning:
      `CAPITAL KAM HAI: Top 10 me har ek me 1 valid order (₹500) ke liye min slice ₹3,118 → total ₹31,188 + reserve. Aapne ₹10k allocate kiya → 2 miss (8/10). Min NAV ₹31,566 chahiye.`
    * Web `Plan` tab me `Stats` me `Minimum NAV` + `Min / stock` + `CAPITAL KAM / kaafi` banner + per-stock `CHENNPETRO 1393×1...` detail, `S.No` se kaunsa chhuta clear.

**Risk gates `_apply_risk_gates` (penny pehle, phir cap):**
* `max_single_order_value 20L` → BLOCKED + suggest
* `min_price 10` → BUY skip only, SELL never (position trap nahi)
* `max_turnover 85%` churn (`sell/NAV`) pe, first deploy churn 0 → pass
* `qty==0` / `limit invalid` → BLOCKED

**Liquidation `build_liquidation_plan`:** watchlist ignore, sab `SELL` `sellable` tak, `securityId` + `ltp` both check, unsettled `Skipped`.

---

## 4. Price & Circuit

* **Primary:** Dhan `marketfeed/quote` (1 req/s, chunk 500) → `CircuitInfo {ltp, upper, lower, prev_close, volume}`
* **Fallback:** `prices.py` `fetch([yahoo,nse])` — `yahoo` per-thread Session (thread-safe fix), `age>15m` stale drop, `NSE` circuit enrich karta hai yahoo me (`has_circuit` merge), `upperCP/lowerCP` ko `intraHigh/Low` se fallback nahi (false circuit fix).
* **Circuit use:** `at_upper = ltp >= upper*0.998` (0.2% tight), `band = (upper-base)/base` where `base=prev_close` (prev 0 → None), liquidity `o.value / traded_value`.

---

## 5. Dhan Client `rebalancer/dhan.py`

* Buckets: `order 8/s (<10)`, `data 4/s (<5)`, `quote 0.9/s (<1)`, `other 15/s`
* `wait` lock hold nahi karta (sleep ke bina reserve)
* `429` → `Retry-After` + jitter, `500` → `DhanNoData` only for `/holdings` with `no data` + `holding/position` keyword (HTML `empty` swallow nahi)
* `Session` + `_req_lock` (thread-safe), chunk partial continue (ek chunk fail pe next chunk try, pehle `return` karta tha)
* `place_order` guard: `find_order_by_correlation` (GET /orders/external/{cid}) + `cid` hash (30 chars, `sha1` 6char suffix, truncate collision fix) + `retries=1` for orders.

---

## 6. Executor `rebalancer/executor.py`

* **Age check BEFORE `EXECUTING`** (pehle dry skip karta tha) + `TIMEOUT 30s WAL`
* **SELL → gap 20s → BUY** (funds settle). T1 LIVE me SELL ke baad BUY skip + `AWAITING_T1_BUY`
* `_fit_to_available_cash`: `limit_price` se need (pehle `ref_price` kam estimate), copy not mutate, affordable `remaining/eff_price` ke baad next cheaper buys try (pehle `break` karta tha)
* `_await_fills`: poll **after** check (pehle sleep pehle), `TERMINAL_OK` = `TRADED,COMPLETE,FILLED` etc, `filledQty` multi-key (`filledQuantity`...), avg price multi-key, `modify_to_market` total qty try phir remaining, converted ko cancel nahi, `_validate_order` pre-flight (`securityId>0, qty>0, price>0, tick`)
* `reconcile`: holdings `avg_price` fallback if ltp 0, nav + cash, drift vs slice.

---

## 7. Web `web/routes.py` (FastAPI 127.0.0.1:8770)

* `STATE` + `RLock` (pehle global dict race), `_cfg` snapshot, `_client` double-checked, `upload` → `mkstemp` unique (pehle fixed `rebal_upload.csv`), `file.read` streaming limit 5MB, sanitize filename `Path.name[:100]` + strip `<>`, error sanitized
* `autodetect` monotonic + lock, `creds_broken` sanitized `[^ -~]`, latch reset on `creds/verify` success (pehle latched rehta tha)
* `mode`, `slots`, `deploy`, `creds/clear` sab `STATE_LOCK` + `plan=None` invalidation
* `deploy` preview `NAV` via same price path as plan (Dhan→fallback)
* `report` includes `S.No` `report.py:60` + web `app.js:459` `S.No | Kya | Symbol | Qty | Price | Limit | Value | Kyun`, allocation mismatch banner `wantSlots vs gotBuys + skipped list` + `skipped` banner.

---

## 8. Store, Instruments, Watchlist, Config

* **Store:** `PRAGMA WAL, busy_timeout 30s`, `_conn` rollback/commit safe, `record_order` allowed cols + `MAX(filled_qty)`, `save_run` `ON CONFLICT` only `status/plan_json`, index on `created_at`.
* **Instruments:** `_download` atomic `tmp+replace` + stale fallback if download fail (pehle corrupt), segment `E/EQUITY/NSE_EQ`, instrument `EQUITY/ES/EQ`, series `EQ` only (BE/BZ reject).
* **Watchlist:** `_looks_like_backtest` 3 lines `as on date+nav+period/holdings`, `_num` `N/A/--/null` → None, duplicate O(n) via set + ISIN check, `_pick` substring fallback, size guard.
* **Config:** `n_stocks: "10.0"` → float→int, `cash_reserve 0-0.5`, `deploy_mode` typo guard, `execution` numeric 0-0.1, `prices fallback` `yahoo/nse` only, `risk allowed_window HH:MM`, 1MB size bomb guard, `settlement_T1` bool + `rebalance_schedule` enum.

---

## 9. Backtest vs Live

* Planner **pure** — backtest me wahi code live me chalta hai. `backtest_realistic/*.csv` 8 months (Aug-Mar) se walk-forward: `Aug normal → Sep high churn 60% → Oct partial 4 → Nov microcap → Dec circuit → Jan low cash → Feb rename ISIN → Mar` — `store.db` nahi, `holdings` T+1 se update, NAV `79L-1Cr` stable, no wash, no oversell.
* **Costs:** `estimate_costs` STT 0.1% both, txn 0.00307%, stamp 0.015% buy, SEBI 0.0001%, GST 18%, DP 14.75/scrip. `annualised_cost = one_time*52` → `pct_of_nav`.
* Paper `PaperClient` `RLock`, `seed_from` backtest period se holdings seed karta hai taaki rebalance dikhe.

---

## 10. Configuration Quick Ref

| Section | Key | Example | Note |
|---|---|---|---|
| portfolio | `n_stocks` | `auto` / `10` | auto = list len - overflow |
|  | `partial_list_mode` | `full` | `fixed_slots` = NAV/n cash drag |
|  | `deploy_mode/pct/amount` | `pct 60` | T1 me next day settle |
|  | `max_weight_per_stock_pct` | `0.25` | null = no cap |
|  | `settlement_T1` | `true` | **CNC ke liye true** |
|  | `rebalance_schedule` | `monthly_eom` | weekly/monthly/manual |
| execution | `order_type` | `LIMIT` | `limit_buffer 0.3%` |
|  | `phase_gap_sec` | `20` | T1 me buys kal |
| prices | `fallback` | `[yahoo,nse]` | `stale_warn 20m` |
| risk | `max_turnover` | `0.85` | churn gate |
|  | `min_price` | `10` | penny guard |
|  | `DH-905` | `whitelist` | Dhan Profile → Static IP + regenerate token, 7 day lock, 0.0.0.0 nahi chalta SEBI 1 Apr 2026 se |

---

## 11. Zerodha-like Portfolio Tracking (No Fake Data) + NAV + EMA

### 11.1 NAV kaise banta hai — 100% Real, No Fake

* **Formula:** `NAV = holdings_value + free_cash`  
  `holdings_value = Σ ( har stock ka qty × uska live LTP )`  
  Example: `CHENNPETRO 71×1393.50=98,938 + IIFL 145×679.80=98,571 + ... + free_cash 50,000 = NAV 10,50,000`
* **Kahan se aata hai:**
  * `holdings` → Dhan API `GET /holdings` → `tradingSymbol, totalQty, availableQty, avgCostPrice` (bonus/split/merger Dhan khud adjust kar deta hai, `runs.db` me nahi)
  * `free_cash` → Dhan API `GET /fundlimit` → `availabelBalance` (typo wala key bhi handle)
  * `LTP` → Dhan `POST /marketfeed/ltp` (1 req/s) → fallback `yahoo/nse` agar paid Data API nahi (delayed ~15m, warning)
  * **Live me** `source = Dhan LIVE - 100% real` badge, **Paper** me `PAPER - demo fake` badge — confuse nahi.
* **Kab save hota hai:** Har `Portfolio` refresh (`/api/holdings`) + har `Plan` execute ke `reconcile` pe `Store.record_nav(nav, holdings_value, free_cash, source, captured_at)` call hota hai. Same minute me duplicate (`captured_at[:16]` same aur NAV 0.1% same) skip — spam nahi. Table `nav_history(captured_at, nav, holdings_value, free_cash, source)` me `PRAGMA WAL` ke saath.
* **Daily vs Weekly:** `GET /api/portfolio/nav_history?timeframe=daily` → har date ka last record; `weekly` → `ISO year-week` ka last (Friday close jaisa) — `OrderedDict` se resample. Limit `90` default, `30` chart me.

### 11.2 EMA kaise nikalta hai — Customizable

* **Formula:** `k = 2 / (period+1)` ; `EMA[0]=NAV[0]` ; `EMA[i]= NAV[i]*k + EMA[i-1]*(1-k)` ; Seed `SMA` pehle `period` tak. Example `period=10, NAV=100,102,105` → `k=0.1818`, `EMA1=100, EMA2=100*0.1818+100*0.818=100.36`...
* **API:** `GET /api/portfolio/ema?period=10&timeframe=daily&limit=90` → `[{date, nav, ema}]` period `2-200` (10,20,50 common). `weekly` me weekly NAV pe EMA — long trend, `daily` me short trend.
* **Custom:** UI me `EMA 10/20/50` preset + custom input `2-200` + `Daily/Weekly` select + `+ Add / Clear` + pills `EMA 10 daily ✕` (localStorage `emas` + `navTf` persist, `navTrackChart` me NAV solid `#0f172a` + EMA dashed colors).

### 11.3 Fund Flows — Withdraw/Deposit Zerodha jaisa

* **Table:** `fund_flows(flow_date, amount, flow_type, note, created_at)` — `amount` `+` = DEPOSIT, `-` = WITHDRAW.
* **API:** `POST /api/portfolio/fund_flow {amount:50000, flow_type:WITHDRAW, note:"rent"}` → `-50000` store, Paper me `PaperClient._cash` bhi `+ amount` (withdraw kam) + `capital` adjust, Live me Dhan ka real cash kam hoga — app sirf track karti hai, double debit nahi.
* **Effect:** `NAV = holdings + cash`, withdraw ke baad `cash -50k` → `NAV -50k`, `holdings_value` same, `P&L` same (realized 0), `nav_history` next point pe dip, `fund_flows` table me `S.No | Date | WITHDRAW | -₹50,000 | rent`, `summary` me `total_deposit / total_withdraw`. Deposit reverse.
* **Example:** `NAV 10L (9.5L holdings + 50k cash) → WITHDRAW 50k → holdings 9.5L + cash 0 = NAV 9.5L → deposit 1L → NAV 10.5L`. Chart pe dip/spike dikhega, EMA usko smooth karega — return track karne ke liye NAV ko EMA ke upar/neeche dekho.

* **No fake:** Live me `Dhan LIVE - 100% real` source, holdings/cash Dhan se; Paper me `PAPER - demo fake` badge alag, confuse nahi.

---

## 12. World-Class UI + S.No Everywhere

* **S.No har table me:** Sell, Buy, Cost (1-4), Portfolio holdings, Runs, Watchlist, Config, Price test sample, Home rebalancing table, Report text (`S.NO` + reconciliation) — 100% tables.
* **Allocation:** `S.No | Kya | Symbol | Qty | Price | Limit | Value | Kyun` + foot, cost `S.No | Kharcha | Amount`, runs `S.No | Run | Kab | Status | NAV`, etc.
* **World-class design:** Inter + JetBrains Mono, glass header `backdrop-blur`, gradient logo, pill tabs, cards hover lift, stats with spark bar, sticky tables, banners with left accent, dropzone radial glow, skeletons, toasts (`Copied ✓`), `⌘K` quick, `?` help, Chart.js donnuts/bars, search `⌕` live filter, `Copy`/`CSV`/`JSON` export, `EMA` pills, `fund flow` table — dark/light, responsive, `v=3` cache bust.

---

## 13. Verification (Bug-Proof)

* **87 pytest:** `test_planner 34 + test_endtoend 19 + test_hardening 22 + test_t1 11` (+1 fix) = **76→87 passed**
* **1000 fuzz:** `500 T1 true + 500 false` random holdings/prices/NaN/inf/duplicate/circuit — no crash, no wash, no oversell, CID unique
* **8-month realistic backtest:** high churn, partial, microcap, circuit, low cash, ISIN rename — warnings + S.No + T+1 split correct, NAV stable.
* **T+1 backtest:** 12-month walk-forward (EOM sell → next day buy) NAV history + EMA 10 weekly/daily toggle tested, fund flow withdraw 50k → NAV -50k verified.
* **Minimum capital:** Golden Cross top10 `min_nav 31566` → below `31k` 8/10 warning, above `10L` 10/10.

---

## 14. How to Run

```bash
# 1. Setup
1-SETUP.bat  # pip install -r requirements.txt + check
# 2. Credentials
creds.bat  # set DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN (web Profile → DhanHQ APIs)
# 3. Whitelist IP (mandatory 1 Apr 2026): web.dhan.co → Profile → Static IP → Primary/Secondary → regenerate token
# 4. Start
START-APP.bat  # http://127.0.0.1:8770
# Flow: Connect → Watchlist CSV drop → (Deploy slider) → Plan Banao (S.No table) → Execute rehearsal → haan → LIVE (EOM SELL, next day BUY if T1)
# CLI: python -m rebalancer.cli plan; python -m rebalancer.cli execute --approve
```

`plans/*.json` + `runs.db` me audit, `.gitignore` me `creds.bat, runs.db, plans/, .cache/`.

