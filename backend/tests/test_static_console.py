"""#1019: the built console served from FastAPI.

The three behaviours worth pinning are the ones whose failure is silent or
dangerous: a stale index.html (blank console pointing at deleted asset
hashes), an /api 404 answered with HTML (a fetch that reads the shell as its
payload), and a path traversal out of dist/ (arbitrary file read on a host
that also holds the live ledger and .env).
"""

import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.static_console import (
    IMMUTABLE_CACHE,
    REVALIDATE_CACHE,
    _resolve_within,
    console_dist_dir,
    mount_console,
)


@pytest.fixture
def dist(tmp_path):
    """A minimal Vite-shaped build."""
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><div id=app></div>")
    (d / "assets" / "index-abc123.js").write_text("console.log(1)")
    (d / "favicon.svg").write_text("<svg/>")
    return d


@pytest.fixture
def client(dist):
    app = FastAPI()

    @app.get("/api/ping")
    async def ping() -> dict:
        return {"ok": True}

    assert mount_console(app, dist) is True
    return TestClient(app)


class TestServingTheBuild:
    def test_root_serves_the_shell(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "id=app" in r.text

    def test_a_client_side_route_falls_back_to_the_shell(self, client):
        # /books is a Svelte route, not a file — the SPA resolves it itself.
        r = client.get("/books")
        assert r.status_code == 200
        assert "id=app" in r.text

    def test_head_on_the_shell_is_answered_not_405ed(self, client):
        # A bare @app.get answers HEAD with 405. Uptime probes and health
        # checks send HEAD, and a 405 on the console root reads as broken.
        r = client.head("/")
        assert r.status_code == 200

    def test_a_root_level_file_is_served_as_itself(self, client):
        r = client.get("/favicon.svg")
        assert r.status_code == 200
        assert "<svg/>" in r.text


class TestCaching:
    def test_hashed_assets_are_immutable(self, client):
        # The filename carries the content hash, so a changed file is a
        # changed URL and a year of caching is safe.
        r = client.get("/assets/index-abc123.js")
        assert r.status_code == 200
        assert r.headers["cache-control"] == IMMUTABLE_CACHE

    def test_the_shell_must_revalidate(self, client):
        # A cached index.html survives a deploy and points the browser at
        # asset hashes that no longer exist — a blank console.
        assert client.get("/").headers["cache-control"] == REVALIDATE_CACHE
        assert client.get("/books").headers["cache-control"] == REVALIDATE_CACHE

    def test_stable_root_files_must_revalidate(self, client):
        assert client.get("/favicon.svg").headers["cache-control"] == REVALIDATE_CACHE


class TestApiIsNeverShadowed:
    def test_a_real_api_route_still_answers(self, client):
        assert client.get("/api/ping").json() == {"ok": True}

    def test_an_unknown_api_path_404s_as_json_not_html(self, client):
        # The console's error handling reads status codes; a fetch handed the
        # SPA shell with a 200 would parse HTML as its payload.
        r = client.get("/api/does-not-exist")
        assert r.status_code == 404
        assert "id=app" not in r.text

    def test_the_bare_api_prefix_404s_too(self, client):
        assert client.get("/api").status_code == 404


class TestTraversalIsRefused:
    """The guard is unit-tested, NOT driven through the HTTP client.

    Discovered the hard way (#1019): every one of these paths passes through
    TestClient even with the guard deleted, because httpx normalises `../`
    out of the URL before the request is ever made. An HTTP-level traversal
    test here is vacuous — it asserts what the client already guarantees and
    would go green on a completely unguarded handler. The handler still takes
    its path from `path:path`, which any raw client can populate however it
    likes, so the guard is real and gets tested where it actually lives.
    """

    @pytest.mark.parametrize(
        "relative",
        [
            "../pyproject.toml",
            "../../etc/passwd",
            "assets/../../pyproject.toml",
            "../backend/main.py",
            "..",
            "../.env",
        ],
    )
    def test_paths_escaping_dist_are_refused(self, dist, relative):
        # This host holds basis.db and .env beside the checkout, so an escape
        # here is an arbitrary file read, not a cosmetic bug.
        assert _resolve_within(dist, relative) is None, f"{relative!r} escaped dist/"

    def test_a_dotted_name_is_a_name_not_a_traversal(self, dist):
        # "....//x" defeats parsers that strip or collapse dot runs. Python
        # treats "...." as an ordinary directory name, so this resolves
        # INSIDE dist and is refused later by simply not existing. Pinned so
        # the distinction stays deliberate rather than accidental.
        resolved = _resolve_within(dist, "....//pyproject.toml")
        assert resolved is not None
        assert resolved.is_relative_to(dist.resolve())
        assert not resolved.exists()

    @pytest.mark.parametrize("relative", ["index.html", "assets/index-abc123.js", "favicon.svg"])
    def test_legitimate_paths_resolve(self, dist, relative):
        resolved = _resolve_within(dist, relative)
        assert resolved is not None and resolved.is_file()

    def test_a_symlink_pointing_out_of_dist_is_refused(self, dist, tmp_path):
        # Resolution follows symlinks, which is why the check compares
        # RESOLVED paths — a string-prefix check would accept this.
        secret = tmp_path / "secret.txt"
        secret.write_text("[project]")
        (dist / "escape.txt").symlink_to(secret)
        assert _resolve_within(dist, "escape.txt") is None

    def test_the_handler_refuses_an_escaping_path_it_is_handed(self, dist):
        # Belt-and-braces at the transport layer: a client that does NOT
        # normalise still cannot read outside dist/.
        app = FastAPI()
        mount_console(app, dist)
        route = next(r for r in app.routes if getattr(r, "name", "") == "serve_console")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(route.endpoint("../pyproject.toml"))
        assert exc.value.status_code == 404


class TestNoBuildPresent:
    def test_mount_declines_without_a_build_and_the_api_still_serves(self, tmp_path):
        # The dev flow and a fresh checkout both hit this: Vite serves the
        # frontend, and the API must come up regardless.
        app = FastAPI()

        @app.get("/api/ping")
        async def ping() -> dict:
            return {"ok": True}

        assert mount_console(app, tmp_path / "nothing-here") is False
        client = TestClient(app)
        assert client.get("/api/ping").json() == {"ok": True}
        assert client.get("/").status_code == 404

    def test_dist_dir_is_overridable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONSOLE_DIST_DIR", str(tmp_path / "staged"))
        assert console_dist_dir() == tmp_path / "staged"

    def test_dist_dir_defaults_into_the_checkout(self, monkeypatch):
        monkeypatch.delenv("CONSOLE_DIST_DIR", raising=False)
        assert console_dist_dir().parts[-2:] == ("frontend", "dist")
