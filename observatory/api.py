"""
The Observatory API — GET only, bearer-token auth, no write path anywhere.

    GET /api/v1/health                 no auth: ok + projector lag
    GET /api/v1/state
    GET /api/v1/events?since_id=&ring=&severity=&limit=
    GET /api/v1/trades?days=
    GET /api/v1/trace/signal/{id}
    GET /api/v1/candles?tf=&days=
    GET /api/v1/perf?window=day|week|all
    GET /api/v1/heartbeats?minutes=
    WS  /ws/events                     pushes new events

Auth: `Authorization: Bearer <token>`, compared in constant time. The query
parameter `token=` is accepted ONLY on the WebSocket, because browsers cannot
set headers on a WebSocket handshake; uvicorn's access log is therefore off,
so that query string is never written to a log. Any method other than GET/HEAD
is answered 405 before routing. CORS is off. OpenAPI/docs pages are off.

Run:  python -m observatory.api      (reads OBSERVATORY_* from the environment)
"""
import asyncio
import hmac
import logging
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Optional

import psycopg2
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from . import queries
from .db import DatabaseUnavailable, ReadOnlyDB
from .events import SEVERITIES, Projector
from .settings import Settings, SettingsError, load_settings

logger = logging.getLogger("observatory.api")

_ALLOWED_METHODS = ("GET", "HEAD")
WS_PUSH_INTERVAL_S = 1.0
WS_BATCH_LIMIT = 200


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReadOnlyMethods:
    """ASGI middleware: every HTTP request that is not GET/HEAD gets 405."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] not in _ALLOWED_METHODS:
            response = JSONResponse(
                {"error": "method not allowed: the observatory is read-only"},
                status_code=405,
                headers={"Allow": ", ".join(_ALLOWED_METHODS)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _token_matches(presented: Optional[str], expected: str) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def _bearer(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def build_app(
    settings: Optional[Settings] = None,
    db: Optional[ReadOnlyDB] = None,
    projector: Optional[Projector] = None,
    start_projector: bool = True,
    clock=_utcnow,
) -> FastAPI:
    """Assemble the app. Raises SettingsError when the environment is incomplete."""
    settings = settings or load_settings()
    db = db or ReadOnlyDB(settings.database_url)
    projector = projector or Projector(db)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        thread = None
        if start_projector:
            thread = threading.Thread(target=projector.run, args=(stop,), name="observatory-projector", daemon=True)
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=5)
            db.close()

    app = FastAPI(
        title="NEXUS Observatory",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(ReadOnlyMethods)
    app.state.projector = projector
    app.state.db = db

    def require_token(request: Request) -> None:
        if not _token_matches(_bearer(request.headers.get("authorization")), settings.token):
            raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(DatabaseUnavailable)
    async def _db_unavailable(_request: Request, exc: DatabaseUnavailable):
        logger.error("database unavailable: %s", exc)
        return JSONResponse({"error": "database unavailable"}, status_code=503)

    @app.exception_handler(psycopg2.Error)
    async def _db_error(_request: Request, exc: psycopg2.Error):
        # e.g. a query cancelled by the 5 s statement timeout. The class name is
        # logged; nothing from the database is echoed to the client.
        logger.error("database error: %s", type(exc).__name__)
        return JSONResponse({"error": "database error"}, status_code=503)

    @app.get("/api/v1/health")
    def health():
        projector_health = projector.health()
        return {"ok": True, "projector_lag_s": projector_health["lag_s"], "projector": projector_health}

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])

    @api.get("/state")
    def state():
        return queries.state(db, clock())

    @api.get("/events")
    def events(
        since_id: int = Query(0, ge=0),
        ring: Optional[int] = Query(None, ge=0, le=3),
        severity: Optional[Literal[SEVERITIES]] = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ):
        found = projector.events(since_id=since_id, ring=ring, severity=severity, limit=limit)
        return {
            "epoch": projector.epoch,
            "last_event_id": projector.last_event_id(),
            "count": len(found),
            "events": found,
        }

    @api.get("/trades")
    def trades(days: int = Query(7, ge=1, le=90)):
        return queries.trades(db, clock(), days)

    @api.get("/trace/signal/{signal_id}")
    def trace_signal(signal_id: int):
        found = queries.trace_signal(db, signal_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")
        return found

    @api.get("/candles")
    def candles(tf: Literal[queries.TIMEFRAMES] = Query("1h"), days: int = Query(7, ge=1, le=365)):
        return queries.candles(db, clock(), tf, days)

    @api.get("/perf")
    def perf(window: Literal[tuple(queries.WINDOWS)] = Query("all")):
        return queries.perf(db, clock(), window)

    @api.get("/heartbeats")
    def heartbeats(minutes: int = Query(60, ge=1, le=1440)):
        return queries.heartbeats(db, clock(), minutes)

    app.include_router(api)
    app.state.api_router = api  # the token-protected routes, for audits and tests

    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket):
        presented = _bearer(websocket.headers.get("authorization")) or websocket.query_params.get("token")
        if not _token_matches(presented, settings.token):
            await websocket.close(code=1008)  # policy violation; handshake refused
            return
        await websocket.accept()
        try:
            last = int(websocket.query_params.get("since_id") or projector.last_event_id())
        except ValueError:
            last = projector.last_event_id()
        try:
            while True:
                batch = projector.events(since_id=last, limit=WS_BATCH_LIMIT)
                for event in batch:
                    await websocket.send_json(event)
                    last = event["id"]
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=WS_PUSH_INTERVAL_S)
                except asyncio.TimeoutError:
                    continue
                if message.get("type") == "websocket.disconnect":
                    return
        except WebSocketDisconnect:
            return

    return app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    import uvicorn

    uvicorn.run(
        build_app(settings),
        host=settings.bind,
        port=settings.port,
        access_log=False,  # the WebSocket may carry token= in its query string
        server_header=False,
        proxy_headers=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
