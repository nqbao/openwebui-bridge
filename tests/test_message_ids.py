import sqlite3
from contextlib import asynccontextmanager

import httpx
import pytest
from test_proxy import FakeBackend, msg

from openwebui_bridge.app import create_app
from openwebui_bridge.config import Profile, Settings
from openwebui_bridge.store import Store


@asynccontextmanager
async def bridge(tmp_path, backend_name="claude"):
    backend = FakeBackend()
    profile = Profile(cwd=tmp_path, backend=backend_name, allowed_tools=[])
    app = create_app(
        Settings(profiles={"test": profile}, database=tmp_path / "sessions.db"),
        {backend_name: backend, "claude": backend},
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client, backend, app


async def turn(
    client, messages, user_id=None, assistant_id=None, parent=None, parent_known=True, **headers
):
    headers = {"X-OpenWebUI-Chat-Id": "chat", "X-OpenWebUI-User-Id": "user", **headers}
    if user_id:
        headers["X-OpenWebUI-User-Message-Id"] = user_id
    if assistant_id:
        headers["X-OpenWebUI-Message-Id"] = assistant_id
    if parent_known:
        headers["X-OpenWebUI-User-Message-Parent-Id"] = parent or ""
    return await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "test", "messages": messages, "stream": True},
    )


@pytest.mark.parametrize("backend_name", ["claude", "codex"])
async def test_resume_regenerate_branch_switch_and_native_invalidation(tmp_path, backend_name):
    async with bridge(tmp_path, backend_name) as (client, backend, app):
        first = [msg("user", "first")]
        await turn(client, first, "u1", "a1")
        second = first + [msg("assistant", "answer"), msg("user", "second")]
        await turn(client, second, "u2", "a2", "a1")
        assert backend.calls[-1][1:] == ("second", "session-1")
        key = app.state.store.db.execute("SELECT chat_id FROM sessions").fetchone()[0]
        assert app.state.store.get_branch(key, "a1") is None
        # Regeneration cannot resume the now-mutated session at a1.
        await turn(client, second, "u2", "a2b", "a1")
        assert backend.calls[-1][2] is None
        assert "first" in backend.calls[-1][1]
        assert app.state.store.get_branch(key, "a2") is not None
        # Switch back to a2: its independent session still matches this prefix.
        third = second + [msg("assistant", "answer"), msg("user", "third")]
        await turn(client, third, "u3", "a3", "a2")
        assert backend.calls[-1][1:] == ("third", "session-2")
        assert app.state.store.get_branch(key, "a2") is None
        assert app.state.store.get_branch(key, "a2b") is not None
        # Returning to the untouched regenerated branch resumes its own session.
        await turn(client, third, "u3b", "a3b", "a2b")
        assert backend.calls[-1][1:] == ("third", "session-3")


@pytest.mark.parametrize(
    "parent,parent_known,expected_resume",
    [
        (None, False, "session-1"),  # Missing header preserves legacy behavior.
        (None, True, None),  # Explicit empty parent is a root, not current head.
        ("unknown", True, None),  # Matching text does not override an unknown parent.
    ],
)
async def test_missing_empty_and_unknown_parent_are_distinct(
    tmp_path, parent, parent_known, expected_resume
):
    async with bridge(tmp_path) as (client, backend, _):
        first = [msg("user", "first")]
        await turn(client, first, "u1", "a1")
        messages = first + [msg("assistant", "answer"), msg("user", "second")]
        await turn(client, messages, "u2", "a2", parent, parent_known)
        assert backend.calls[-1][2] == expected_resume


@pytest.mark.parametrize("change", ["content", "user_id_reused", "assistant_id_reused"])
async def test_hashes_and_duplicate_ids_prevent_wrong_resume(tmp_path, change):
    async with bridge(tmp_path) as (client, backend, _):
        await turn(client, [msg("user", "first")], "u1", "a1")
        messages = [
            msg("user", "edited" if change == "content" else "first"),
            msg("assistant", "answer"),
            msg("user", "second"),
        ]
        await turn(
            client,
            messages,
            "u1" if change == "user_id_reused" else "u2",
            "a1" if change == "assistant_id_reused" else "a2",
            "a1",
        )
        assert backend.calls[-1][2] is None


async def test_failed_resume_invalidates_branch_reference(tmp_path):
    async with bridge(tmp_path) as (client, backend, app):
        first = [msg("user", "first")]
        await turn(client, first, "u1", "a1")
        messages = first + [msg("assistant", "answer"), msg("user", "second")]
        backend.fail = True
        response = await turn(client, messages, "u2", "a2", "a1")
        assert "server_error" in response.text
        assert app.state.store.db.execute("SELECT COUNT(*) FROM branch_sessions").fetchone()[0] == 0
        backend.fail = False
        await turn(client, messages, "u2", "a2", "a1")
        assert backend.calls[-1][2] is None


async def test_branch_mappings_survive_restart_and_remain_user_scoped(tmp_path):
    first = [msg("user", "first")]
    async with bridge(tmp_path) as (client, _, _):
        await turn(client, first, "u1", "a1")
    async with bridge(tmp_path) as (client, backend, _):
        messages = first + [msg("assistant", "answer"), msg("user", "second")]
        await turn(client, messages, "u2", "a2", "a1", **{"X-OpenWebUI-User-Id": "other"})
        assert backend.calls[-1][2] is None
        await turn(client, messages, "u2", "a2", "a1")
        assert backend.calls[-1][1:] == ("second", "session-1")


async def test_task_headers_do_not_change_branch_mappings(tmp_path):
    async with bridge(tmp_path) as (client, _, app):
        first = [msg("user", "first")]
        await turn(client, first, "u1", "a1")
        before = [tuple(row) for row in app.state.store.db.execute("SELECT * FROM branch_sessions")]
        await turn(
            client,
            first,
            "task-user",
            "task-asst",
            "a1",
            **{"X-OpenWebUI-Task": "title_generation"},
        )
        after = [tuple(row) for row in app.state.store.db.execute("SELECT * FROM branch_sessions")]
        assert before == after


async def test_oversized_message_id_is_rejected_before_backend(tmp_path):
    async with bridge(tmp_path) as (client, backend, app):
        response = await turn(client, [msg("user", "first")], "u" * 201, "a1")
        assert response.status_code == 400
        assert not backend.calls
        assert app.state.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_old_database_migration_and_branch_metadata(tmp_path):
    path = tmp_path / "sessions.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE sessions (
            chat_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, cwd TEXT NOT NULL,
            prefix_hash TEXT NOT NULL, updated_at REAL NOT NULL)""")
        db.execute("INSERT INTO sessions VALUES ('chat', 'session', '.', 'hash', 0)")
    store = Store(path)
    try:
        assert store.get("chat")["session_id"] == "session"
        assert store.get("chat")["parent_known"] == 0
        store.save("chat", "new", ".", "new-hash", "u1", "a1", None, True)
        branch = store.get_branch("chat", "a1")
        assert branch["user_message_id"] == "u1" and branch["parent_known"] == 1
        store.invalidate_session("chat", "new")
        assert store.get("chat") is None and store.get_branch("chat", "a1") is None
    finally:
        store.close()
