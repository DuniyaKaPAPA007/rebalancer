"""Configurable nakli Dhan -- har edge case simulate karne ke liye."""
from fastapi import FastAPI
from fastapi.responses import JSONResponse
app = FastAPI()
S = {"holdings": "empty_list", "cash": 500000.0, "quote": "ok", "orders_fail": False}

def _hold_rows(n):
    base=[("HFCL","21951",4100,210.0),("STLTECH","18564",1450,620.0),
          ("DIACABS","12043",2500,372.0),("SWANDEF","19112",900,980.0)]
    return [{"tradingSymbol":s,"securityId":sid,"totalQty":q,"availableQty":q,
             "avgCostPrice":p,"exchange":"NSE"} for s,sid,q,p in base[:n]]

@app.post("/v2/_set/{k}/{v}")
def _set(k:str,v:str):
    S[k]=float(v) if k=="cash" else (v=="true" if v in("true","false") else v)
    return dict(S)

@app.get("/v2/fundlimit")
def funds():
    m=S["holdings"]
    if m=="auth_fail": return JSONResponse({"errorType":"Invalid_Authentication"},status_code=401)
    return {"availabelBalance":S["cash"],"withdrawableBalance":S["cash"],
            "sodLimit":S["cash"],"utilizedAmount":0.0}

@app.get("/v2/holdings")
def holdings():
    m=S["holdings"]
    if m=="empty_list":  return []
    if m=="null":        return JSONResponse(content=None)
    if m=="no_data_500": return JSONResponse({"errorType":"Data_Missing_Error","errorMessage":"No data available for the given criteria"},status_code=500)
    if m=="wrapped_empty": return {"data":[]}
    if m=="auth_fail":   return JSONResponse({"errorType":"Invalid_Authentication"},status_code=401)
    if m=="outage":      return JSONResponse({"errorType":"Internal_Server_Error"},status_code=503)
    if m=="zero_qty":    return [{"tradingSymbol":"HFCL","securityId":"21951","totalQty":0,"availableQty":0,"avgCostPrice":0}]
    if m.startswith("n"): return _hold_rows(int(m[1:]))
    return []

def _px(sid:str)->float:
    return round(50 + (int(sid) % 900) * 1.7, 2)

def _feed(body):
    out={}
    for seg, ids in (body or {}).items():
        out[seg] = {str(i): {"last_price": _px(str(i)),
                             "upper_circuit_limit": _px(str(i))*1.2,
                             "lower_circuit_limit": _px(str(i))*0.8,
                             "prev_close": _px(str(i)),
                             "volume": 5_00_000,
                             "average_price": _px(str(i))} for i in ids}
    return out

@app.get("/v2/marketfeed/ltp")
def ltp(): return {}
@app.post("/v2/marketfeed/ltp")
def ltp2(body: dict|None=None):
    if S["quote"]=="fail": return JSONResponse({"errorType":"err"},status_code=500)
    return {"status":"success","data":_feed(body)}
@app.post("/v2/marketfeed/quote")
def quote(body: dict|None=None):
    if S["quote"]=="fail": return JSONResponse({"errorType":"err"},status_code=500)
    return {"status":"success","data":_feed(body)}
@app.post("/v2/orders")
def place(body: dict|None=None):
    if S["orders_fail"]: return JSONResponse({"errorType":"RMS_Error","errorMessage":"insufficient funds"},status_code=400)
    return {"orderId":"MOCK1","orderStatus":"TRANSIT"}
@app.get("/v2/orders/{oid}")
def order(oid:str): return {"orderId":oid,"orderStatus":"TRADED","filledQty":1,"averageTradedPrice":100.0}
@app.get("/v2/orders")
def all_orders(): return []
