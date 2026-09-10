"""static_console.py — serving the built console from FastAPI (#1019).

Until this existed, the console the operator used was a Vite DEV server run
permanently as a scheduled task. That cost a second process, an /api proxy,
a host-check workaround for the tailnet, and the dev server's own attack
surface — and it silently half-deployed: a dev server cannot hot-swap its
own bundler, so #1014's vite 5→8 bump left the merged #1011 button
invisible while the README still promised "no restart step exists".

Serving the build instead makes the console one origin and one process. The
dev flow is untouched: with no `frontend/dist` on disk, `mount_console`
declines and logs, so `pixi run server` + `pixi run client` behaves exactly
as before.

Two rules the caching has to get right, in opposite directions:

- `/assets/*` filenames carry Vite's content hash, so they are immutable by
  construction and cached for a year. A changed file is a changed name.
- `index.html` and the root-level files (favicon, and later the web app
  manifest and service worker) keep their names across builds, so they must
  revalidate every time. An index.html cached even briefly points the
  browser at asset hashes the deploy just deleted — a blank console with a
  404 in the network tab, which is exactly the failure this module exists
  to stop being possible.
"""

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Vite emits hashed bundles here; everything else in dist/ keeps a stable name.
ASSETS_SUBDIR = "assets"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
REVALIDATE_CACHE = "no-cache"


def console_dist_dir() -> Path:
    """Where the built console lives. CONSOLE_DIST_DIR overrides, so the
    build can be staged elsewhere without moving the checkout."""
    override = os.getenv("CONSOLE_DIST_DIR", "").strip()
    return Path(override) if override else _PROJECT_ROOT / "frontend" / "dist"


def _resolve_within(root: Path, relative: str) -> Path | None:
    """*relative* resolved under *root*, or None if it escapes.

    The SPA fallback takes an arbitrary URL path from the network, so this
    is the one place a `../` traversal could read outside dist/. Compare
    RESOLVED paths (symlinks included), never the string — `is_relative_to`
    on unresolved paths is satisfied by a path that resolves elsewhere.
    """
    try:
        candidate = (root / relative).resolve()
    except (OSError, ValueError):  # malformed path, or a name the OS refuses
        return None
    root_resolved = root.resolve()
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        return None
    return candidate


def mount_console(app: FastAPI, dist: Path | None = None) -> bool:
    """Serve the built console at `/`. Returns whether it mounted.

    Declines (and says so) when there is no build — a fresh checkout, or the
    dev flow where Vite serves the frontend itself. Never raises: a missing
    build must not stop the API from starting, because the API is what the
    scheduled entrypoints and the operator's own curl still need.

    Registered AFTER every /api route, so those keep matching first and an
    unknown /api path still 404s as JSON rather than being answered with the
    SPA shell — a fetch that silently receives HTML instead of its 404 is a
    debugging trap, and the console's own error handling reads status codes.
    """
    dist = dist or console_dist_dir()
    index = dist / "index.html"
    if not index.is_file():
        logger.info("No built console at %s — serving the API only (run `pixi run build-frontend`)", dist)
        return False

    assets = dist / ASSETS_SUBDIR
    if assets.is_dir():
        app.mount(f"/{ASSETS_SUBDIR}", _ImmutableStaticFiles(directory=assets), name="console-assets")

    # GET and HEAD both (#1019): a bare @app.get answers HEAD with 405, and
    # HEAD on the console root is what a health check or an uptime probe
    # sends. Caught end-to-end against a real uvicorn, not by the unit tests
    # -- TestClient exercised the handler directly and never saw the method
    # routing that produced the 405.
    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_console(path: str) -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        # /api is handled by the real routes above; reaching here means no
        # route matched, so this is a genuine 404 and must read as one.
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        if path:
            candidate = _resolve_within(dist, path)
            if candidate is None:
                raise HTTPException(status_code=404, detail="Not Found")
            if candidate.is_file():
                return FileResponse(candidate, headers={"Cache-Control": REVALIDATE_CACHE})
        # Any other path is a client-side route; the SPA resolves it itself.
        return FileResponse(index, headers={"Cache-Control": REVALIDATE_CACHE})

    logger.info("Serving the built console from %s", dist)
    return True


class _ImmutableStaticFiles(StaticFiles):
    """StaticFiles that marks every response immutable — correct ONLY for
    Vite's content-hashed `assets/` output, never for stable filenames."""

    def file_response(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = IMMUTABLE_CACHE
        return response
