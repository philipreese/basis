"""Tests for the operational kill switch (backend/trading_control.py, #65).

Every fail-closed default from spec/supervision.md gets a failing test here,
per the rule that prose enforces nothing:
- missing row → HALT_ENTRIES
- unrecognized state value → HALT_ENTRIES
- unreadable store → HALT_ENTRIES
- sentinel file overrides an ACTIVE database
- RESUME is console-only (ntfy channel is HALT-only, asymmetric)
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import trading_control as tc
from backend.database import get_db
from backend.models import AuditEventModel, Base, BookModel, TradingControlModel

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _no_sentinel(monkeypatch, tmp_path):
    """Point the sentinel at a temp path that doesn't exist by default."""
    monkeypatch.setenv("HALT_FILE", str(tmp_path / "HALT"))


async def _seed(maker, scope: str, state: str) -> None:
    async with maker() as session:
        session.add(TradingControlModel(scope=scope, state=state, reason="", actor="test", changed_at="t0"))
        await session.commit()


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_missing_global_row_reads_as_halt(self, session_maker):
        async with session_maker() as session:
            scope, state = await tc.check_trading_control(session)
        assert (scope, state) == (tc.GLOBAL_SCOPE, tc.HALT_ENTRIES)

    @pytest.mark.asyncio
    async def test_unrecognized_state_value_reads_as_halt(self, session_maker):
        await _seed(session_maker, "GLOBAL", "TOTALLY_FINE_TRUST_ME")
        async with session_maker() as session:
            _scope, state = await tc.check_trading_control(session)
        assert state == tc.HALT_ENTRIES

    @pytest.mark.asyncio
    async def test_unreadable_store_reads_as_halt(self, session_maker):
        # #464: get_control_state reads via session.execute (a real SELECT,
        # not the identity-map session.get) — patch the method it now uses.
        async with session_maker() as session:
            with patch.object(session, "execute", side_effect=RuntimeError("db exploded")):
                state = await tc.get_control_state(session, "GLOBAL")
        assert state == tc.HALT_ENTRIES

    @pytest.mark.asyncio
    async def test_book_without_control_row_is_halted(self, session_maker):
        await _seed(session_maker, "GLOBAL", tc.ACTIVE)
        async with session_maker() as session:
            scope, state = await tc.check_trading_control(session, book_id="B03")
        assert (scope, state) == ("B03", tc.HALT_ENTRIES)

    @pytest.mark.asyncio
    async def test_sentinel_file_overrides_active_database(self, session_maker, monkeypatch, tmp_path):
        halt_file = tmp_path / "HALT"
        halt_file.write_text("stop")
        monkeypatch.setenv("HALT_FILE", str(halt_file))
        await _seed(session_maker, "GLOBAL", tc.ACTIVE)
        async with session_maker() as session:
            scope, state = await tc.check_trading_control(session)
        assert (scope, state) == ("SENTINEL", tc.HALT_ENTRIES)


class TestEffectiveState:
    @pytest.mark.asyncio
    async def test_all_active(self, session_maker):
        await _seed(session_maker, "GLOBAL", tc.ACTIVE)
        await _seed(session_maker, "B01", tc.ACTIVE)
        async with session_maker() as session:
            assert await tc.check_trading_control(session, "B01") == (tc.GLOBAL_SCOPE, tc.ACTIVE)

    @pytest.mark.asyncio
    async def test_global_halt_blocks_every_book(self, session_maker):
        await _seed(session_maker, "GLOBAL", tc.HALT_ENTRIES)
        await _seed(session_maker, "B01", tc.ACTIVE)
        async with session_maker() as session:
            scope, state = await tc.check_trading_control(session, "B01")
        assert (scope, state) == (tc.GLOBAL_SCOPE, tc.HALT_ENTRIES)

    @pytest.mark.asyncio
    async def test_book_halt_is_scoped(self, session_maker):
        await _seed(session_maker, "GLOBAL", tc.ACTIVE)
        await _seed(session_maker, "B01", tc.HALT_ENTRIES)
        await _seed(session_maker, "B02", tc.ACTIVE)
        async with session_maker() as session:
            assert await tc.check_trading_control(session, "B01") == ("B01", tc.HALT_ENTRIES)
            assert await tc.check_trading_control(session, "B02") == (tc.GLOBAL_SCOPE, tc.ACTIVE)


class TestChokePoint:
    @pytest.mark.asyncio
    async def test_halted_raises_and_audits(self, session_maker):
        await _seed(session_maker, "GLOBAL", tc.HALT_ENTRIES)
        async with session_maker() as session:
            with pytest.raises(tc.TradingHaltedError):
                await tc.assert_entries_allowed(session, "B01")
            await session.commit()
        async with session_maker() as session:
            events = (await session.execute(select(AuditEventModel))).scalars().all()
        checks = [e for e in events if e.event_type == "CONTROL_CHECK"]
        assert len(checks) == 1
        assert checks[0].payload["state_read"] == tc.HALT_ENTRIES

    @pytest.mark.asyncio
    async def test_active_passes_and_still_audits(self, session_maker):
        await _seed(session_maker, "GLOBAL", tc.ACTIVE)
        await _seed(session_maker, "B01", tc.ACTIVE)
        async with session_maker() as session:
            await tc.assert_entries_allowed(session, "B01")
            await session.commit()
        async with session_maker() as session:
            events = (await session.execute(select(AuditEventModel))).scalars().all()
        assert any(e.event_type == "CONTROL_CHECK" and e.payload["state_read"] == tc.ACTIVE for e in events)

    @pytest.mark.asyncio
    async def test_a_long_lived_session_sees_a_mid_run_halt_from_a_second_session(self, session_maker):
        # #464 (Audit II R3 F1): a session.get() choke-point read is an
        # identity-map hit once the row has been loaded once — the executor
        # holds one session all night, so a console HALT posted mid-run
        # (a DIFFERENT session/process) would never be seen. Load the row
        # once here (as Layer A does at run start), THEN flip it via a
        # second session, and prove THIS session's next choke-point call
        # still sees the halt.
        await _seed(session_maker, "GLOBAL", tc.ACTIVE)
        async with session_maker() as long_lived:
            # Warm the identity map — mirrors the executor loading control
            # rows once at Layer A start.
            await tc.get_control_state(long_lived, "GLOBAL")

            # A console HALT lands mid-run, via a completely different session.
            async with session_maker() as console_session:
                await tc.set_control(
                    console_session, "GLOBAL", tc.HALT_ENTRIES, reason="operator halt", actor="console"
                )

            # The long-lived session's next choke-point read must NOT reuse
            # the stale ACTIVE instance from its identity map.
            with pytest.raises(tc.TradingHaltedError):
                await tc.assert_entries_allowed(long_lived, "B01")
            await long_lived.commit()
        async with session_maker() as session:
            events = (await session.execute(select(AuditEventModel))).scalars().all()
        checks = [e for e in events if e.event_type == "CONTROL_CHECK"]
        assert checks[-1].payload["state_read"] == tc.HALT_ENTRIES


class TestSetControl:
    @pytest.mark.asyncio
    async def test_halt_writes_row_and_audit(self, session_maker):
        async with session_maker() as session:
            await tc.set_control(session, "GLOBAL", tc.HALT_ENTRIES, reason="manual test", actor="console")
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            events = (await session.execute(select(AuditEventModel))).scalars().all()
        assert row.state == tc.HALT_ENTRIES
        assert any(e.event_type == "CONTROL_STATE_CHANGED" for e in events)

    @pytest.mark.asyncio
    async def test_resume_requires_console_privilege(self, session_maker):
        await _seed(session_maker, "GLOBAL", tc.HALT_ENTRIES)
        async with session_maker() as session:
            with pytest.raises(PermissionError, match="console-only"):
                await tc.set_control(session, "GLOBAL", tc.ACTIVE, reason="oops", actor="ntfy")
            await tc.set_control(session, "GLOBAL", tc.ACTIVE, reason="verified ok", actor="console", allow_resume=True)
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
        assert row.state == tc.ACTIVE

    @pytest.mark.asyncio
    async def test_unknown_state_rejected(self, session_maker):
        async with session_maker() as session:
            with pytest.raises(ValueError, match="Unknown trading-control state"):
                await tc.set_control(session, "GLOBAL", "PAUSE_ISH", reason="r", actor="console")


def _ntfy_line(message: str, time: int = 0) -> str:
    return json.dumps({"event": "message", "message": message, "time": time})


class TestNtfyChannel:
    @pytest.mark.asyncio
    async def test_no_topic_configured_is_noop(self, session_maker, monkeypatch):
        monkeypatch.delenv("NTFY_COMMAND_TOPIC", raising=False)
        async with session_maker() as session:
            assert await tc.apply_ntfy_commands(session) == 0

    @pytest.mark.asyncio
    async def test_halt_commands_applied(self, session_maker, monkeypatch):
        monkeypatch.setenv("NTFY_COMMAND_TOPIC", "basis-cmd-test")
        body = "\n".join(
            [
                json.dumps({"event": "open"}),
                _ntfy_line("HALT"),
                _ntfy_line("HALT B03"),
            ]
        )
        resp = SimpleNamespace(text=body, raise_for_status=lambda: None)
        with patch.object(tc.httpx, "get", return_value=resp):
            async with session_maker() as session:
                applied = await tc.apply_ntfy_commands(session)
        assert applied == 2
        async with session_maker() as session:
            global_row = await session.get(TradingControlModel, "GLOBAL")
            book_row = await session.get(TradingControlModel, "B03")
        assert global_row.state == tc.HALT_ENTRIES
        assert book_row.state == tc.HALT_ENTRIES

    @pytest.mark.asyncio
    async def test_resume_over_ntfy_ignored_and_audited(self, session_maker, monkeypatch):
        monkeypatch.setenv("NTFY_COMMAND_TOPIC", "basis-cmd-test")
        await _seed(session_maker, "GLOBAL", tc.HALT_ENTRIES)
        resp = SimpleNamespace(text=_ntfy_line("RESUME"), raise_for_status=lambda: None)
        with patch.object(tc.httpx, "get", return_value=resp):
            async with session_maker() as session:
                applied = await tc.apply_ntfy_commands(session)
        assert applied == 0
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            events = (await session.execute(select(AuditEventModel))).scalars().all()
        assert row.state == tc.HALT_ENTRIES  # still latched
        assert any(e.event_type == "CONTROL_RESUME_REMOTE_IGNORED" for e in events)

    @pytest.mark.asyncio
    async def test_poll_failure_is_safe_noop(self, session_maker, monkeypatch):
        monkeypatch.setenv("NTFY_COMMAND_TOPIC", "basis-cmd-test")
        with patch.object(tc.httpx, "get", side_effect=RuntimeError("offline")):
            async with session_maker() as session:
                assert await tc.apply_ntfy_commands(session) == 0

    @pytest.mark.asyncio
    async def test_watermark_prevents_reapplying_yesterdays_halt(self, session_maker, monkeypatch):
        # H7 (#278): a HALT the operator already resumed must not be silently
        # re-applied by the next poll's 24h lookback.
        monkeypatch.setenv("NTFY_COMMAND_TOPIC", "basis-cmd-test")
        resp = SimpleNamespace(text=_ntfy_line("HALT", time=1000), raise_for_status=lambda: None)
        with patch.object(tc.httpx, "get", return_value=resp):
            async with session_maker() as session:
                assert await tc.apply_ntfy_commands(session) == 1
        # Console resume, then the next poll sees the SAME message again.
        async with session_maker() as session:
            await tc.set_control(session, "GLOBAL", tc.ACTIVE, reason="verified", actor="console", allow_resume=True)
        with patch.object(tc.httpx, "get", return_value=resp) as mock_get:
            async with session_maker() as session:
                assert await tc.apply_ntfy_commands(session) == 0
        assert mock_get.call_args.kwargs["params"]["since"] == "1001"  # watermark + 1
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
        assert row.state == tc.ACTIVE  # the resume survived

    @pytest.mark.asyncio
    async def test_applied_halt_pushes_a_receipt(self, session_maker, monkeypatch):
        monkeypatch.setenv("NTFY_COMMAND_TOPIC", "basis-cmd-test")
        resp = SimpleNamespace(text=_ntfy_line("HALT B03", time=2000), raise_for_status=lambda: None)
        with (
            patch.object(tc.httpx, "get", return_value=resp),
            patch("backend.operator.send_ntfy") as mock_push,
        ):
            async with session_maker() as session:
                assert await tc.apply_ntfy_commands(session) == 1
        title = mock_push.call_args[0][0]
        assert "remote HALT applied" in title


@pytest_asyncio.fixture
async def client(session_maker):
    from backend.main import app

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


class TestApi:
    @pytest.mark.asyncio
    async def test_get_view(self, client, session_maker):
        await _seed(session_maker, "GLOBAL", tc.ACTIVE)
        resp = await client.get("/api/trading-control")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sentinel_halt"] is False
        assert data["controls"][0]["scope"] == "GLOBAL"

    @pytest.mark.asyncio
    async def test_console_can_halt_and_resume_global(self, client):
        resp = await client.post(
            "/api/trading-control", json={"scope": "GLOBAL", "state": "HALT_ENTRIES", "reason": "drill"}
        )
        assert resp.status_code == 200
        resp = await client.post(
            "/api/trading-control", json={"scope": "GLOBAL", "state": "ACTIVE", "reason": "drill over"}
        )
        assert resp.status_code == 200
        states = {c["scope"]: c["state"] for c in resp.json()["controls"]}
        assert states["GLOBAL"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_unknown_book_scope_404s(self, client):
        resp = await client.post(
            "/api/trading-control", json={"scope": "B99", "state": "HALT_ENTRIES", "reason": "typo"}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_known_book_scope_accepted(self, client, session_maker):
        async with session_maker() as session:
            session.add(
                BookModel(
                    id="B01",
                    name="test",
                    config={},
                    config_version=1,
                    config_hash="",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="t0",
                )
            )
            await session.commit()
        resp = await client.post(
            "/api/trading-control", json={"scope": "B01", "state": "HALT_ENTRIES", "reason": "book drill"}
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_resume_blocked_while_sentinel_present(self, client, monkeypatch, tmp_path):
        halt_file = tmp_path / "HALT"
        halt_file.write_text("stop")
        monkeypatch.setenv("HALT_FILE", str(halt_file))
        resp = await client.post(
            "/api/trading-control", json={"scope": "GLOBAL", "state": "ACTIVE", "reason": "trying anyway"}
        )
        assert resp.status_code == 409
