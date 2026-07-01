from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from rnd_devtools_relay.api import create_app
from rnd_devtools_relay.cli import app


class ClientAdapter:
    def __init__(self, client: TestClient):
        self.client = client

    def __enter__(self) -> TestClient:
        return self.client

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


runner = CliRunner()


def test_init_creates_local_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    config_path = tmp_path / ".relay" / "config.json"
    assert config_path.exists()
    data = json.loads(config_path.read_text())
    assert data["base_url"] == "http://127.0.0.1:8000"
    assert data["channels"] == []


def test_config_registers_agent_and_channel_membership(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "config",
            "-a",
            "coordinator",
            "-c",
            "frontend-debug",
        ],
    )

    assert result.exit_code == 0
    participants = client.get("/participants").json()
    assert any(item["agent_id"] == "coordinator" for item in participants)
    channels = client.get("/channels").json()
    assert any(item["channel_id"] == "frontend-debug" for item in channels)


def test_send_uses_local_config_and_minimal_flags(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(
        app,
        [
            "config",
            "-a",
            "sender",
            "-c",
            "ops",
        ],
        catch_exceptions=False,
    )
    client.post("/participants", json={"agent_id": "receiver", "metadata": {}})
    client.post("/channels/ops/join", json={"agent_id": "receiver"})

    result = runner.invoke(
        app,
        ["send", "-m", "@receiver inspect the logs", "-a", "receiver"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    threads = client.get("/threads", params={"channel_id": "ops"}).json()
    assert any(thread["thread_id"] == "chat-sender-to-receiver" for thread in threads)
    history = client.get("/threads/chat-sender-to-receiver/messages").json()
    assert len(history) == 1
    assert history[0]["payload"] == "@receiver inspect the logs"


def test_ls_lists_channel_members_by_default_and_all_with_flag(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(
        app,
        ["config", "-a", "sender", "-c", "ops"],
        catch_exceptions=False,
    )
    client.post("/participants", json={"agent_id": "receiver", "metadata": {}})
    client.post("/channels/ops/join", json={"agent_id": "receiver"})
    client.post("/participants", json={"agent_id": "observer", "metadata": {}})

    channel_result = runner.invoke(app, ["ls"], catch_exceptions=False)
    assert channel_result.exit_code == 0
    channel_agents = json.loads(channel_result.stdout)
    assert {item["agent_id"] for item in channel_agents} == {"sender", "receiver"}

    all_result = runner.invoke(app, ["ls", "-a"], catch_exceptions=False)
    assert all_result.exit_code == 0
    all_agents = json.loads(all_result.stdout)
    assert {item["agent_id"] for item in all_agents} == {"sender", "receiver", "observer"}


def test_send_rejects_recipient_not_in_channel(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(
        app,
        ["config", "-a", "sender", "-c", "ops"],
        catch_exceptions=False,
    )
    client.post("/participants", json={"agent_id": "receiver", "metadata": {}})

    result = runner.invoke(app, ["send", "-m", "inspect the logs", "-a", "receiver"])
    assert result.exit_code != 0
    assert "not subscribed to channel" in result.output
    assert "relay ls" in result.output


def test_config_show_displays_agent_orientation(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "coordinator", "-c", "frontend-debug"], catch_exceptions=False)

    result = runner.invoke(app, ["config", "show"], catch_exceptions=False)
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["agent_id"] == "coordinator"
    assert data["active_channel"] == "frontend-debug"


def test_register_adds_agent_to_channel_without_touching_local_config(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["register", "-a", "network-specialist", "-c", "frontend-debug"], catch_exceptions=False)

    assert result.exit_code == 0
    participants = client.get("/participants").json()
    assert any(item["agent_id"] == "network-specialist" for item in participants)
    members = client.get("/channels/frontend-debug/participants").json()
    assert any(item["agent_id"] == "network-specialist" for item in members)
    config_data = json.loads((tmp_path / ".relay" / "config.json").read_text())
    assert config_data["agent_id"] is None
