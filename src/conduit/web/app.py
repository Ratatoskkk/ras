"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import STATIC_DIR, ConfigStore, Settings, get_settings
from ..logs import get_logger
from ..services.context import Conduit
from ..services.supervisor import Supervisor
from ..services.tasks import build_tasks
from .api import router as api_router
from .security import AccessMiddleware
from .ws import router as ws_router

log = get_logger("web")


def create_app(
    settings: Settings | None = None,
    config_store: ConfigStore | None = None,
    *,
    run_tasks: bool = True,
    static_dir: Path = STATIC_DIR,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = Conduit(settings=settings, config_store=config_store)
        await ctx.start()
        supervisor = Supervisor(ctx)
        supervisor.register_all(build_tasks())
        # Services can now ask for a task to run immediately -- a watchlist add
        # should not wait out the search interval.
        ctx.request_run = supervisor.trigger
        app.state.conduit = ctx
        app.state.supervisor = supervisor
        if run_tasks:
            await supervisor.start()
        else:
            log.info("background tasks disabled for this instance")
        try:
            yield
        finally:
            if run_tasks:
                await supervisor.stop()
            await ctx.stop()

    app = FastAPI(
        title="rás",
        version=__version__,
        description="Plex watchlist to tracker to qBittorrent automation.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(AccessMiddleware, settings=settings)
    app.include_router(api_router)
    app.include_router(ws_router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled request error", extra={"path": request.url.path})
        # The details (SQL, local paths) stay in the log, not the response.
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    _mount_frontend(app, static_dir)
    return app


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    """Serve the dashboard. No build step: these are plain files.

    Because filenames carry no content hash, everything is served
    ``no-cache`` -- the browser still revalidates cheaply with an ETag, but an
    edited file is never stale. That is the trade for not needing a bundler.
    """
    if not static_dir.exists():
        log.warning("static directory missing", extra={"path": str(static_dir)})
        return

    class RevalidatingStatic(StaticFiles):
        def file_response(self, *args, **kwargs):  # type: ignore[override]
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/assets", RevalidatingStatic(directory=static_dir / "assets"), name="assets")

    root = static_dir.resolve()
    index = root / "index.html"
    headers = {"Cache-Control": "no-cache"}

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        # An unmatched /api/... path is a client bug, not a deep link. Serving
        # index.html there turns a typo into a silent, confusing 200.
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        if path:
            try:
                candidate = (root / path).resolve()
                # Path containment by path segments, not by string prefix: a
                # sibling directory called "static_something" starts with the
                # same characters as "static" and would otherwise be served.
                servable = candidate.is_relative_to(root) and candidate.is_file()
            except (ValueError, OSError):
                # NUL bytes and names Windows rejects: not a file, not a 500.
                servable = False
            if servable:
                return FileResponse(candidate, headers=headers)
        return FileResponse(index, headers=headers)
