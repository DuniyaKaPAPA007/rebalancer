"""FastAPI entry point.

Chalane ka tareeka -- START-APP.bat double-click karo.
Ya manually:  python -m uvicorn web.api:app --host 127.0.0.1 --port 8770
Phir browser mein kholo: http://127.0.0.1:8770
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from web.routes import register_routes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")

_HERE = Path(__file__).resolve().parent
_STATIC = _HERE / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="Weekly Rebalancer", version="2.0")
    register_routes(app)
    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        f = _STATIC / "index.html"
        if f.exists():
            return HTMLResponse(f.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>index.html missing</h1>", status_code=500)

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    return app


app = create_app()
