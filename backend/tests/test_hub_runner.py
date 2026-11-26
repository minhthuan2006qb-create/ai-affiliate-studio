from __future__ import annotations

import importlib


def test_runner_once_records_heartbeats_and_survives_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FACEBOOK_HUB_DB", str(tmp_path / "hub.sqlite3"))
    monkeypatch.delenv("AI_API_BASE_URL", raising=False)
    monkeypatch.delenv("META_TOKEN_ENCRYPTION_KEY", raising=False)
    import app.facebook_hub as hub
    importlib.reload(hub)
    monkeypatch.setattr(hub, "DB_PATH", tmp_path / "hub.sqlite3")
    from app import hub_runner

    def boom():
        raise RuntimeError("nguồn hỏng")
    calls = []
    tasks = [("sources", 1800, boom), ("schedule", 60, lambda: calls.append(1) or "ok-run")]
    monkeypatch.setattr(hub_runner, "build_tasks", lambda minutes: tasks)
    monkeypatch.setattr("sys.argv", ["hub_runner", "--once"])
    hub_runner.main()
    assert calls == [1]  # an error in one task does not stop the next
    with hub.connect() as conn:
        rows = {r["task"]: dict(r) for r in conn.execute("SELECT * FROM fb_worker_heartbeats")}
    assert rows["sources"]["last_status"] == "error" and "nguồn hỏng" in rows["sources"]["last_message"]
    assert rows["schedule"]["last_status"] == "ok"


def test_real_tasks_run_once_without_network_or_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("FACEBOOK_HUB_DB", str(tmp_path / "hub.sqlite3"))
    monkeypatch.delenv("AI_API_BASE_URL", raising=False)
    monkeypatch.delenv("META_TOKEN_ENCRYPTION_KEY", raising=False)
    import app.facebook_hub as hub
    importlib.reload(hub)
    monkeypatch.setattr(hub, "DB_PATH", tmp_path / "hub.sqlite3")
    from app import hub_runner
    hub_runner.ensure_heartbeat_schema()
    for name, interval, func in hub_runner.build_tasks(30):
        entry = hub_runner.run_task(name, interval, func)
        assert entry["status"] == "ok", entry
    with hub.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fb_worker_heartbeats").fetchone()[0] == 5


def test_token_task_runs_when_meta_key_is_configured(tmp_path, monkeypatch):
    # Regression: on a real server setup_env.sh sets the key, so the task must not crash.
    monkeypatch.setenv("FACEBOOK_HUB_DB", str(tmp_path / "hub.sqlite3"))
    monkeypatch.setenv("META_TOKEN_ENCRYPTION_KEY", "x" * 44)
    import app.facebook_hub as hub
    importlib.reload(hub)
    monkeypatch.setattr(hub, "DB_PATH", tmp_path / "hub.sqlite3")
    from app import hub_runner
    hub_runner.ensure_heartbeat_schema()
    entry = hub_runner.run_task("token_health", 43200, hub_runner._token_cycle)
    assert entry["status"] == "ok", entry
    assert entry["message"].startswith("pages=0")

    import app.facebook_token_health as th
    monkeypatch.setattr(th, "check_all_tokens", lambda: {"checked": 2, "pages": [
        {"health_status": "healthy"}, {"health_status": "expiring"}]})
    entry = hub_runner.run_task("token_health", 43200, hub_runner._token_cycle)
    assert entry["status"] == "ok" and entry["message"] == "pages=2 healthy,expiring"
