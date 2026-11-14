"""FastAPI app of the reporting UI.

Read-only over orders, order lines, products and categories; it never reads
conversations, the API request log or the Picnic token.  Every ``/api`` route
except the login needs a session cookie (see ``login.py``), and the Telegram
allowlist is checked again on each request, so removing a user locks them
out at once.

The built frontend (``frontend/dist``, or ``WEB_STATIC_DIR``) is served from
the same origin; during frontend development Vite proxies ``/api`` here.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import access, reporting
from ..db import queries as db
from ..db.engine import ensure_schema
from . import login

logger = logging.getLogger(__name__)

Level = Literal["global", "l1", "l2", "l3", "product", "uncategorised"]
Bucket = Literal["week", "month"]


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class Totals(BaseModel):
    spend: int  # cents, quantity × unit price of the order lines
    units: int
    orders: int


class HierarchyNode(Totals):
    level: Level
    id: str
    name: str | None  # None: not looked up yet (or the global / uncategorised node)
    children: list[HierarchyNode]


class Crumb(BaseModel):
    level: Level
    id: str
    name: str | None


class Summary(Totals):
    first_order: date | None
    last_order: date | None
    # Sum of the orders' own totals (incl. fees and deposits); global node only.
    order_total: int | None


class BucketTotals(Totals):
    start: date


class TimeSeries(BaseModel):
    path: list[Crumb]
    bucket: Bucket
    summary: Summary
    buckets: list[BucketTotals]


class Me(BaseModel):
    user_id: int


class LoginRequest(BaseModel):
    token: str


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def current_user(request: Request) -> int:
    """The logged-in, still-allowed Telegram user id, else 401."""
    user_id = login.verify_session(request.cookies.get(login.SESSION_COOKIE))
    if user_id is None or not access.is_allowed(user_id):
        raise HTTPException(status_code=401, detail="Not logged in.")
    return user_id


User = Annotated[int, Depends(current_user)]
DateFrom = Annotated[date | None, Query(alias="from")]
DateTo = Annotated[date | None, Query(alias="to")]


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await ensure_schema("the web app")
    yield


def _static_dir() -> Path:
    return Path(os.getenv("WEB_STATIC_DIR", "frontend/dist")).resolve()


def create_app() -> FastAPI:
    app = FastAPI(title="Picnic Like a Pro — Reports", lifespan=_lifespan)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/auth/login", response_model=Me)
    async def log_in(body: LoginRequest, response: Response) -> Me:
        user_id = await login.redeem_login_token(body.token)
        if user_id is None or not access.is_allowed(user_id):
            raise HTTPException(
                status_code=401,
                detail="This login link is invalid, expired or already used.",
            )
        response.set_cookie(
            login.SESSION_COOKIE,
            login.sign_session(user_id),
            max_age=int(login.SESSION_TTL.total_seconds()),
            httponly=True,
            samesite="lax",
            secure=login.base_url().startswith("https://"),
        )
        return Me(user_id=user_id)

    @app.post("/api/auth/logout", status_code=204)
    async def log_out(response: Response) -> None:
        response.delete_cookie(login.SESSION_COOKIE)

    @app.get("/api/me", response_model=Me)
    async def me(user_id: User) -> Me:
        return Me(user_id=user_id)

    @app.get("/api/hierarchy", response_model=HierarchyNode)
    async def hierarchy(_user: User, date_from: DateFrom = None, date_to: DateTo = None) -> dict:
        async with db.get_db() as session:
            orders = await db.report_orders(session)
            order_lines = await db.report_order_lines(session)
            products = await db.report_products(session)
        days = reporting.order_days(orders, date_from, date_to)
        return reporting.hierarchy(reporting.build_lines(days, order_lines), products)

    @app.get("/api/timeseries", response_model=TimeSeries)
    async def timeseries(
        _user: User,
        level: Level = "global",
        id: str = "all",  # noqa: A002 — the query parameter's name
        bucket: Bucket = "month",
        date_from: DateFrom = None,
        date_to: DateTo = None,
    ) -> dict:
        async with db.get_db() as session:
            orders = await db.report_orders(session)
            order_lines = await db.report_order_lines(session)
            products = await db.report_products(session)
        selected = reporting.select_node(products, level, id)
        if selected is None:
            raise HTTPException(status_code=404, detail="No such category or product.")
        path, product_ids = selected
        days = reporting.order_days(orders, date_from, date_to)
        lines = [
            line
            for line in reporting.build_lines(days, order_lines)
            if line.product_id in product_ids
        ]
        order_total = None
        if level == "global":
            order_total = sum(o["total_price"] or 0 for o in orders if o["id"] in days)
        return {
            "path": path,
            "bucket": bucket,
            "summary": {**reporting.summary(lines), "order_total": order_total},
            "buckets": reporting.timeseries(lines, bucket),
        }

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        """The built frontend; unknown paths get ``index.html`` (client routing)."""
        static = _static_dir()
        if path.startswith("api/") or not (static / "index.html").is_file():
            raise HTTPException(status_code=404)
        candidate = (static / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(static):
            return FileResponse(candidate)
        return FileResponse(static / "index.html")

    return app


def main() -> None:
    import uvicorn  # noqa: PLC0415

    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_app(),
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
