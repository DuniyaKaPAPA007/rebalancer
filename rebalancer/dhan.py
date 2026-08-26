"""
DhanHQ v2 client.

Rate limits (Dhan support, Aug 2026):
    Order APIs      10 req/s
    Data APIs        5 req/s
    Quote APIs       1 req/s
    Non-trading     20 req/s
Har category ka apna throttle hai -- ek shared limiter galat hota.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Mapping, Optional

import requests

from .models import CircuitInfo, Position

log = logging.getLogger(__name__)


class DhanError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, body: Any = None):
        super().__init__(msg)
        self.status, self.body = status, body


class DhanNoData(DhanError):
    """Dhan ne error bheja par asal mein "kuch hai hi nahi" ka matlab hai.

    Naya account, khaali portfolio, ya jis din koi position nahi -- Dhan
    in sab par bhi kabhi-kabhi 5xx bhej deta hai. Ye crash nahi hona
    chahiye; khaali list bilkul valid jawaab hai.
    """


_NO_DATA_MARKERS = (
    "no data", "data_missing", "data missing", "no holding", "no record",
    "not found", "no position", "empty",
)


def _short(body: Any, n: int = 200) -> str:
    t = body if isinstance(body, str) else str(body)
    t = " ".join(t.split())
    return t[:n] + ("..." if len(t) > n else "")


def _looks_like_no_data(body: Any) -> bool:
    t = _short(body, 500).lower()
    return any(m in t for m in _NO_DATA_MARKERS)


class _Throttle:
    """Simple per-category rate limiter. Thread-safe."""

    def __init__(self, per_sec: float):
        self._min_gap = 1.0 / per_sec
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        # FIX: don't hold lock while sleeping
        with self._lock:
            now = time.monotonic()
            gap = now - self._last
            wait_needed = self._min_gap - gap if gap < self._min_gap else 0
            # reserve slot immediately to avoid thundering herd
            self._last = now + wait_needed
        if wait_needed > 0:
            time.sleep(wait_needed)


class DhanClient:

    def __init__(self, client_id: str, access_token: str,
                  base_url: str = "https://api.dhan.co/v2",
                  timeout: int = 20):
        if not client_id or not access_token:
            raise DhanError(
                "DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN set nahi hain. "
                "Dhan web -> Profile -> DhanHQ Trading APIs se lo.")
        self.client_id = client_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers.update({
            "access-token": access_token,
            "client-id": client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._req_lock = threading.Lock()
        self._t = {
            "order": _Throttle(8),      # limit 10, thoda margin
            "data": _Throttle(4),       # limit 5
            "quote": _Throttle(0.9),    # limit 1
            "other": _Throttle(15),     # limit 20
        }

    # ------------------------------------------------------------------
    def _req(self, method: str, path: str, *, bucket: str = "other",
              json: Any = None, retries: int = 3) -> Any:
        url = f"{self.base_url}{path}"
        last: Exception | None = None
        for attempt in range(retries):
            self._t[bucket].wait()
            try:
                with self._req_lock:
                    r = self._s.request(method, url, json=json, timeout=self.timeout)
            except requests.RequestException as e:
                last = e
                import random as _r3
                time.sleep(1.5 * (attempt + 1) + _r3.uniform(0, 0.4))
                continue

            if r.status_code == 429:                       # rate limited
                # respect Retry-After if present
                retry_after = r.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else 2.0 * (attempt + 1)
                except (TypeError, ValueError):
                    wait = 2.0 * (attempt + 1)
                # add jitter 0-0.5s to avoid herding
                import random
                wait += random.uniform(0, 0.5)
                time.sleep(wait)
                last = DhanError("rate limited", 429, r.text)
                continue
            if r.status_code >= 500:                       # server side
                body = _safe_json(r)
                # FIX: only treat as no-data if status is 500 AND body contains marker AND looks like holdings/positions empty
                # avoid swallowing real 500 with generic "empty"
                # check more specific: must have errorCode or explicit no data
                is_no_data_body = False
                if isinstance(body, dict):
                    # Dhan error structure may have errorType/errorCode
                    txt = str(body).lower()
                    is_no_data_body = _looks_like_no_data(txt) and any(k in txt for k in ["holding", "position", "data"])
                elif isinstance(body, str):
                    is_no_data_body = _looks_like_no_data(body) and ("holding" in body.lower() or "position" in body.lower() or "data" in body.lower())
                # be conservative: only Holdings/positions endpoints get no-data treatment
                if is_no_data_body and path in ("/holdings", "/positions", "/holdings/positions"):
                    raise DhanNoData(f"{method} {path}: koi data nahi",
                                     r.status_code, body)
                # also if body clearly says no data even with generic path, allow
                elif _looks_like_no_data(body) and path == "/holdings":
                    # legacy but check holdings only
                    # verify it's not HTML error page containing "empty"
                    if isinstance(body, str) and "<html" in body.lower():
                        pass  # real error, retry
                    else:
                        raise DhanNoData(f"{method} {path}: koi data nahi",
                                         r.status_code, body)
                import random as _rnd2
                time.sleep(1.5 * (attempt + 1) + _rnd2.uniform(0, 0.3))
                last = DhanError(f"server error {r.status_code}",
                                 r.status_code, body)
                continue
            if r.status_code >= 400:
                # 4xx = hamari galti. Retry karne ka koi matlab nahi,
                # aur order APIs par retry DUPLICATE ORDER bana sakta hai.
                body = _safe_json(r)
                # Special handling for DH-905 Invalid IP - give actionable message
                try:
                    b_str = str(body)
                    if "DH-905" in b_str or "Invalid IP" in b_str:
                        msg = (
                            "Dhan DH-905: Invalid IP - Tumhara IP Dhan me whitelist nahi hai. "
                            "Dhan web -> Profile -> DhanHQ Trading APIs -> Whitelist IP me "
                            "jaake apna public IP add karo (https://ifconfig.me pe dekho). "
                            "Ya 0.0.0.0 allow karo. Order API IP-check karta hai, data API nahi."
                        )
                        raise DhanError(msg, r.status_code, body)
                except DhanError:
                    raise
                except:
                    pass
                raise DhanError(f"{method} {path} -> {r.status_code}",
                                r.status_code, body)
            if not r.content:
                return None
            return _safe_json(r)
        detail = ""
        if isinstance(last, DhanError) and last.body:
            detail = f" -- Dhan ne kaha: {_short(last.body)}"
        raise DhanError(f"{method} {path} {retries} baar try kiya, nahi hua: "
                        f"{last}{detail}",
                        getattr(last, "status", None),
                        getattr(last, "body", None))

    # ---- read-only ----------------------------------------------------
    def funds(self) -> dict:
        return self._req("GET", "/fundlimit", bucket="other") or {}

    def available_cash(self) -> float:
        f = self.funds()
        for k in ("availabelBalance", "availableBalance", "withdrawableBalance"):
            if k in f:                       # Dhan ke docs mein typo hai, dono handle
                return float(f[k] or 0)
        raise DhanError(f"fundlimit response samajh nahi aaya: {list(f)}")

    def holdings(self) -> list[Position]:
        """Portfolio. Khaali account bhi bilkul valid hai -- crash nahi."""
        try:
            rows = self._req("GET", "/holdings", bucket="other") or []
        except DhanNoData:
            log.info("Dhan ne holdings par 'no data' bheja -- portfolio "
                     "khaali maan rahe hain.")
            return []
        if isinstance(rows, dict):          # kabhi {"data": [...]} aata hai
            rows = rows.get("data") or rows.get("holdings") or []
        if not isinstance(rows, list):
            return []
        out: list[Position] = []
        for r in rows:
            total = int(r.get("totalQty") or 0)
            if total <= 0:
                continue
            # availableQty = DP-free. Isse zyada bechna = short delivery.
            avail = int(r.get("availableQty", r.get("dpQty", total)) or 0)
            out.append(Position(
                symbol=(r.get("tradingSymbol") or r.get("securityId") or "").upper(),
                security_id=str(r.get("securityId")),
                total_qty=total,
                available_qty=avail,
                avg_price=float(r.get("avgCostPrice") or 0),
            ))
        return out

    def ltp(self, security_ids: list[str],
            segment: str = "NSE_EQ") -> dict[str, float]:
        """Bulk LTP. Quote API 1 req/s hai isliye chunk bade rakhe hain."""
        out: dict[str, float] = {}
        ids = [str(i) for i in security_ids]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            body = {segment: [int(x) if x.isdigit() else x for x in chunk]}
            try:
                resp = self._req("POST", "/marketfeed/ltp", bucket="quote", json=body)
            except DhanError as e:
                log.warning("ltp chunk %d-%d fail: %s (partial continue)", i, i+len(chunk), e)
                continue
            except Exception as e:
                log.warning("ltp chunk unexpected fail: %s", e)
                continue
            try:
                data = ((resp or {}).get("data") or {}).get(segment, {})
                for sec_id, payload in data.items():
                    price = payload.get("last_price") if isinstance(payload, dict) else payload
                    if price:
                        try:
                            out[str(sec_id)] = float(price)
                        except (TypeError, ValueError):
                            continue
            except (AttributeError, TypeError) as e:
                log.warning("ltp parse fail chunk %d: %s", i, e)
                continue
        return out

    def quotes(self, security_ids: list[str], symbol_of: dict[str, str],
               segment: str = "NSE_EQ") -> dict[str, CircuitInfo]:
        """LTP ke saath circuit limits bhi. Isse pata chalta hai ki scrip
        upper/lower circuit par lagi toh nahi -- lagi hai toh order bharega
        hi nahi, kyunki doosri taraf koi hai hi nahi."""
        out: dict[str, CircuitInfo] = {}
        ids = [str(i) for i in security_ids]
        # FIX: use lock for session thread-safety (requests.Session not thread-safe)
        _lock = threading.Lock()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            body = {segment: [int(x) if x.isdigit() else x for x in chunk]}
            try:
                # lock around request to protect Session
                with _lock:
                    resp = self._req("POST", "/marketfeed/quote", bucket="quote", json=body)
            except DhanError as e:
                log.warning("quote chunk %d-%d fail: %s (continue next chunk)", i, i+len(chunk), e)
                continue
            except Exception as e:
                log.warning("quote chunk unexpected fail: %s", e)
                continue
            try:
                data = ((resp or {}).get("data") or {}).get(segment, {})
                for sec_id, q in data.items():
                    if not isinstance(q, dict):
                        continue
                    sym = symbol_of.get(str(sec_id))
                    if not sym:
                        continue
                    ohlc = q.get("ohlc") or {}
                    out[sym] = CircuitInfo(
                        symbol=sym,
                        ltp=float(q.get("last_price") or 0),
                        upper=float(q.get("upper_circuit_limit") or 0),
                        lower=float(q.get("lower_circuit_limit") or 0),
                        prev_close=float(ohlc.get("close") or q.get("close") or 0),
                        volume=int(q.get("volume") or 0),
                    )
            except (AttributeError, TypeError, ValueError) as e:
                log.warning("quote parse fail chunk %d: %s", i, e)
                continue
        return out

    # ---- orders -------------------------------------------------------
    def find_order_by_correlation(self, correlation_id: str) -> Optional[dict]:
        """Idempotency ka dil. Restart ke baad ye batata hai ki order
        pehle hi ja chuka hai ya nahi."""
        try:
            r = self._req("GET", f"/orders/external/{correlation_id}", bucket="order")
        except DhanError as e:
            if e.status in (400, 404):
                return None
            raise
        if isinstance(r, list):
            return r[0] if r else None
        return r or None

    def place_order(self, *, security_id: str, side: str, qty: int,
                    exchange_segment: str, product_type: str,
                    order_type: str = "LIMIT", price: float = 0.0,
                    validity: str = "DAY",
                    correlation_id: str | None = None) -> dict:
        if qty <= 0:
            raise DhanError(f"qty {qty} -- order place nahi karenge.")

        # --- IDEMPOTENCY GUARD --------------------------------------
        # Network timeout ke baad blind retry = double order = double paisa.
        if correlation_id:
            existing = self.find_order_by_correlation(correlation_id)
            if existing:
                log.warning("Order %s pehle hi ja chuka hai (%s) -- skip.",
                            correlation_id, existing.get("orderStatus"))
                return existing

        body = {
            "dhanClientId": self.client_id,
            "transactionType": side,
            "exchangeSegment": exchange_segment,
            "productType": product_type,
            "orderType": order_type,
            "validity": validity,
            "securityId": str(security_id),
            "quantity": int(qty),
            "price": round(float(price), 2) if order_type == "LIMIT" else 0,
        }
        if correlation_id:
            body["correlationId"] = correlation_id[:30]
        return self._req("POST", "/orders", bucket="order", json=body, retries=1)

    def order(self, order_id: str) -> dict:
        r = self._req("GET", f"/orders/{order_id}", bucket="order")
        return (r[0] if isinstance(r, list) and r else r) or {}

    def all_orders(self) -> list[dict]:
        return self._req("GET", "/orders", bucket="order") or []

    def modify_to_market(self, order_id: str, qty: int) -> dict:
        return self._req("PUT", f"/orders/{order_id}", bucket="order", retries=1,
                         json={"dhanClientId": self.client_id,
                               "orderId": str(order_id),
                               "orderType": "MARKET",
                               "quantity": int(qty),
                               "validity": "DAY"})

    def cancel(self, order_id: str) -> dict:
        return self._req("DELETE", f"/orders/{order_id}", bucket="order", retries=1)


def _safe_json(r: requests.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text
