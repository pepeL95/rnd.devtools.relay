from __future__ import annotations

import json
from pathlib import Path

import click
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
    assert data["sessions"] == []


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
            "-s",
            "coord-main",
        ],
    )

    assert result.exit_code == 0
    participants = client.get("/participants").json()
    participant = next(item for item in participants if item["agent_id"] == "coordinator")
    assert participant["metadata"]["active_session"] == "coord-main"
    channels = client.get("/channels").json()
    assert any(item["channel_id"] == "frontend-debug" for item in channels)


def test_send_uses_local_config_and_minimal_flags(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.setattr("rnd_devtools_relay.cli._tmux_session_exists", lambda target: True)
    monkeypatch.setattr("rnd_devtools_relay.cli._resolve_tmux_pane_target", lambda session, channel, agent: "%42")
    injected: list[tuple[str, str]] = []
    monkeypatch.setattr("rnd_devtools_relay.cli._inject_tmux", lambda target, text: injected.append((target, text)))
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
            "-s",
            "sender-main",
        ],
        catch_exceptions=False,
    )
    client.post("/participants", json={"agent_id": "receiver", "metadata": {"active_session": "receiver-main", "sessions": ["receiver-main"]}})
    client.post("/channels/ops/join", json={"agent_id": "receiver"})

    result = runner.invoke(
        app,
        ["send", "-m", "@receiver inspect the logs", "-a", "receiver"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert injected[0][0] == "%42"
    threads = client.get("/threads", params={"channel_id": "ops"}).json()
    assert len(threads) == 1
    thread_id = threads[0]["thread_id"]
    history = client.get(f"/threads/{thread_id}/messages").json()
    assert len(history) == 1
    assert history[0]["payload"] == "@receiver inspect the logs"
    assert history[0]["delivery_status"] == "delivered"
    assert len(injected) == 1
    rendered = injected[0][1]
    assert "You received a relay message from another agent." in rendered
    assert "Sender: sender" in rendered
    assert f"Thread: {thread_id}" in rendered
    assert f'relay respond -m "<your response>" -t {thread_id}' in rendered
    assert "Session:" not in rendered
    assert "Channel:" not in rendered
    assert rendered.endswith("Incoming message:\n@receiver inspect the logs")


def test_respond_reuses_direct_bridge_thread(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.setattr("rnd_devtools_relay.cli._tmux_session_exists", lambda target: True)
    monkeypatch.setattr(
        "rnd_devtools_relay.cli._resolve_tmux_pane_target",
        lambda session, channel, agent: "%42" if agent == "receiver" else "%41",
    )
    injected: list[tuple[str, str]] = []
    monkeypatch.setattr("rnd_devtools_relay.cli._inject_tmux", lambda target, text: injected.append((target, text)))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "sender", "-c", "ops", "-s", "shared-main"], catch_exceptions=False)
    client.post("/participants", json={"agent_id": "receiver", "metadata": {"active_session": "shared-main", "sessions": ["shared-main"]}})
    client.post("/channels/ops/join", json={"agent_id": "receiver"})
    runner.invoke(app, ["send", "-m", "inspect the logs", "-a", "receiver"], catch_exceptions=False)
    threads = client.get("/threads", params={"channel_id": "ops"}).json()
    assert len(threads) == 1
    thread_id = threads[0]["thread_id"]

    runner.invoke(app, ["config", "-a", "receiver", "-c", "ops", "-s", "shared-main"], catch_exceptions=False)
    result = runner.invoke(app, ["respond", "-m", "done", "-t", thread_id], catch_exceptions=False)

    assert result.exit_code == 0
    assert injected[0][0] == "%42"
    assert injected[1][0] == "%41"
    assert "Incoming message:\ndone" in injected[1][1]
    assert f"To acknowledge: relay ack -t {thread_id}" in injected[1][1]
    assert f'To follow up: relay respond -t {thread_id} -m "<follow-up request>"' in injected[1][1]
    history = client.get(f"/threads/{thread_id}/messages").json()
    assert len(history) == 2
    assert history[1]["sender_agent_id"] == "receiver"
    assert history[1]["recipient_agent_id"] == "sender"
    assert history[1]["delivery_status"] == "delivered"
    assert history[1]["metadata"]["kind"] == "response"
    assert history[1]["metadata"]["reply_to_envelope_id"] == history[0]["envelope_id"]


def test_ack_marks_latest_inbound_unacknowledged_message_on_thread(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.setattr("rnd_devtools_relay.cli._tmux_session_exists", lambda target: True)
    monkeypatch.setattr(
        "rnd_devtools_relay.cli._resolve_tmux_pane_target",
        lambda session, channel, agent: "%42" if agent == "receiver" else "%41",
    )
    monkeypatch.setattr("rnd_devtools_relay.cli._inject_tmux", lambda target, text: None)
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "sender", "-c", "ops", "-s", "shared-main"], catch_exceptions=False)
    client.post("/participants", json={"agent_id": "receiver", "metadata": {"active_session": "shared-main", "sessions": ["shared-main"]}})
    client.post("/channels/ops/join", json={"agent_id": "receiver"})
    runner.invoke(app, ["send", "-m", "inspect the logs", "-a", "receiver"], catch_exceptions=False)
    thread_id = client.get("/threads", params={"channel_id": "ops"}).json()[0]["thread_id"]

    runner.invoke(app, ["config", "-a", "receiver", "-c", "ops", "-s", "shared-main"], catch_exceptions=False)
    runner.invoke(app, ["respond", "-m", "done", "-t", thread_id], catch_exceptions=False)

    runner.invoke(app, ["config", "-a", "sender", "-c", "ops", "-s", "shared-main"], catch_exceptions=False)
    result = runner.invoke(app, ["ack", "-t", thread_id], catch_exceptions=False)

    assert result.exit_code == 0
    acked = json.loads(result.stdout)
    assert acked["thread_id"] == thread_id
    assert acked["recipient_agent_id"] == "sender"
    assert acked["acked_at"] is not None


def test_ack_fails_without_inbound_unacknowledged_message(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "sender", "-c", "ops", "-s", "shared-main"], catch_exceptions=False)
    client.post("/participants", json={"agent_id": "receiver", "metadata": {"active_session": "shared-main", "sessions": ["shared-main"]}})
    client.post("/channels/ops/join", json={"agent_id": "receiver"})
    runner.invoke(app, ["send", "-m", "inspect the logs", "-a", "receiver"], catch_exceptions=False)
    thread_id = client.get("/threads", params={"channel_id": "ops"}).json()[0]["thread_id"]

    result = runner.invoke(app, ["ack", "-t", thread_id])

    assert result.exit_code != 0
    assert "no inbound unacknowledged message found" in result.output


def test_respond_fails_clearly_when_thread_does_not_exist(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "receiver", "-c", "ops", "-s", "shared-main"], catch_exceptions=False)

    result = runner.invoke(app, ["respond", "-m", "done", "-t", "thread-missing"])

    assert result.exit_code != 0
    assert isinstance(result.exception, click.ClickException)
    assert "thread-missing" in str(result.exception)
    assert "start a new exchange" in str(result.exception)


def test_ack_fails_clearly_when_thread_does_not_exist(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "sender", "-c", "ops", "-s", "shared-main"], catch_exceptions=False)

    result = runner.invoke(app, ["ack", "-t", "thread-missing"])

    assert result.exit_code != 0
    assert isinstance(result.exception, click.ClickException)
    assert "thread-missing" in str(result.exception)
    assert "start a new exchange" in str(result.exception)


def test_ls_lists_channel_members_by_default_and_all_with_flag(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(
        app,
        ["config", "-a", "sender", "-c", "ops", "-s", "shared-main"],
        catch_exceptions=False,
    )
    client.post("/participants", json={"agent_id": "receiver", "metadata": {"active_session": "receiver-main", "sessions": ["receiver-main"]}})
    client.post("/channels/ops/join", json={"agent_id": "receiver"})
    client.post("/participants", json={"agent_id": "observer", "metadata": {"active_session": "observer-main", "sessions": ["observer-main"]}})

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
    runner.invoke(app, ["config", "-a", "sender", "-c", "ops", "-s", "sender-main"], catch_exceptions=False)
    client.post("/participants", json={"agent_id": "receiver", "metadata": {"active_session": "receiver-main", "sessions": ["receiver-main"]}})

    result = runner.invoke(app, ["send", "-m", "inspect the logs", "-a", "receiver"])
    assert result.exit_code != 0
    assert "not subscribed to channel" in result.output
    assert "relay ls" in result.output


def test_config_show_displays_agent_orientation(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "coordinator", "-c", "frontend-debug", "-s", "coord-main"], catch_exceptions=False)

    result = runner.invoke(app, ["config", "show"], catch_exceptions=False)
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["agent_id"] == "coordinator"
    assert data["active_channel"] == "frontend-debug"
    assert data["active_session"] == "coord-main"
    assert data["sessions"] == ["coord-main"]


def test_config_clear_resets_local_orientation_but_keeps_base_url(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init", "--base-url", "http://127.0.0.1:9000"])
    runner.invoke(app, ["config", "-a", "coordinator", "-c", "frontend-debug", "-s", "coord-main"], catch_exceptions=False)

    result = runner.invoke(app, ["config", "--clear"], catch_exceptions=False)

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["base_url"] == "http://127.0.0.1:9000"
    assert data["agent_id"] is None
    assert data["channels"] == []
    assert data["active_channel"] is None
    assert data["sessions"] == []
    assert data["active_session"] is None


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


def test_send_marks_message_delivered_when_tmux_injection_succeeds(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.setattr("rnd_devtools_relay.cli._tmux_session_exists", lambda target: True)
    monkeypatch.setattr("rnd_devtools_relay.cli._resolve_tmux_pane_target", lambda session, channel, agent: "%42")
    injected: list[tuple[str, str]] = []
    monkeypatch.setattr("rnd_devtools_relay.cli._inject_tmux", lambda target, text: injected.append((target, text)))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "sender", "-c", "ops", "-s", "sender-main"], catch_exceptions=False)
    client.post("/participants", json={"agent_id": "receiver", "metadata": {"active_session": "receiver-main", "sessions": ["receiver-main"]}})
    client.post("/channels/ops/join", json={"agent_id": "receiver"})
    result = runner.invoke(app, ["send", "-m", "inspect the logs", "-a", "receiver"], catch_exceptions=False)

    assert result.exit_code == 0
    pending = client.get("/messages/pending", params={"recipient_agent_id": "receiver", "channel_id": "ops"}).json()
    assert pending == []
    assert len(injected) == 1
    assert injected[0][0] == "%42"
    rendered = injected[0][1]
    assert "Incoming message:\ninspect the logs" in rendered
    assert "Session:" not in rendered
    assert "Channel:" not in rendered


def test_send_decodes_escaped_newlines_before_delivery(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.setattr("rnd_devtools_relay.cli._tmux_session_exists", lambda target: True)
    monkeypatch.setattr("rnd_devtools_relay.cli._resolve_tmux_pane_target", lambda session, channel, agent: "%42")
    injected: list[tuple[str, str]] = []
    monkeypatch.setattr("rnd_devtools_relay.cli._inject_tmux", lambda target, text: injected.append((target, text)))
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "sender", "-c", "ops", "-s", "sender-main"], catch_exceptions=False)
    client.post("/participants", json={"agent_id": "receiver", "metadata": {"active_session": "receiver-main", "sessions": ["receiver-main"]}})
    client.post("/channels/ops/join", json={"agent_id": "receiver"})

    result = runner.invoke(
        app,
        ["send", "-m", "hey there again\\nensure to say hello back", "-a", "receiver"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    threads = client.get("/threads", params={"channel_id": "ops"}).json()
    thread_id = threads[0]["thread_id"]
    history = client.get(f"/threads/{thread_id}/messages").json()
    assert history[0]["payload"] == "hey there again\nensure to say hello back"
    assert injected[0][1].endswith("Incoming message:\nhey there again\nensure to say hello back")


def test_send_marks_failure_when_tmux_target_missing(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.setattr("rnd_devtools_relay.cli._tmux_session_exists", lambda target: False)
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "receiver", "-c", "ops", "-s", "receiver-main"], catch_exceptions=False)
    client.post("/participants", json={"agent_id": "sender", "metadata": {"active_session": "sender-main", "sessions": ["sender-main"]}})
    client.post("/channels/ops/join", json={"agent_id": "sender"})
    result = runner.invoke(app, ["send", "-m", "inspect the logs", "-a", "receiver"])

    assert result.exit_code != 0
    pending = client.get("/messages/pending", params={"recipient_agent_id": "receiver", "channel_id": "ops"}).json()
    assert pending == []
    history = client.get("/threads", params={"channel_id": "ops"}).json()
    thread_id = history[0]["thread_id"]
    messages = client.get(f"/threads/{thread_id}/messages").json()
    assert messages[0]["delivery_status"] == "failed"
    assert "tmux session `receiver-main` not found" == messages[0]["delivery_error"]


def test_send_uses_session_in_thread_identity(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.setattr("rnd_devtools_relay.cli._tmux_session_exists", lambda target: True)
    monkeypatch.setattr("rnd_devtools_relay.cli._resolve_tmux_pane_target", lambda session, channel, agent: "%42")
    monkeypatch.setattr("rnd_devtools_relay.cli._inject_tmux", lambda target, text: None)
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "sender", "-c", "ops", "-s", "alpha"], catch_exceptions=False)
    client.post("/participants", json={"agent_id": "receiver", "metadata": {"active_session": "receiver-main", "sessions": ["receiver-main"]}})
    client.post("/channels/ops/join", json={"agent_id": "receiver"})

    runner.invoke(app, ["send", "-m", "first", "-a", "receiver"], catch_exceptions=False)
    runner.invoke(app, ["config", "--active-session", "beta", "-s", "beta"], catch_exceptions=False)
    runner.invoke(app, ["send", "-m", "second", "-a", "receiver"], catch_exceptions=False)

    threads = client.get("/threads", params={"channel_id": "ops"}).json()
    assert len(threads) == 2
    thread_ids = {thread["thread_id"] for thread in threads}
    assert len(thread_ids) == 2


def test_send_fails_when_channel_window_lacks_recipient_pane(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(db_path=tmp_path / "relay.db", node_id="local"))
    monkeypatch.setattr("rnd_devtools_relay.cli._client", lambda base_url: ClientAdapter(client))
    monkeypatch.setattr("rnd_devtools_relay.cli._tmux_session_exists", lambda target: True)
    monkeypatch.setattr(
        "rnd_devtools_relay.cli._resolve_tmux_pane_target",
        lambda session, channel, agent: (_ for _ in ()).throw(
            __import__("typer").BadParameter(
                f"no tmux pane titled `{agent}` found in window `{session}:{channel}`. "
                "Set the pane title to the agent name with `tmux select-pane -T <agent>`."
            )
        ),
    )
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["init"])
    runner.invoke(app, ["config", "-a", "sender", "-c", "ops", "-s", "sender-main"], catch_exceptions=False)
    client.post("/participants", json={"agent_id": "receiver", "metadata": {"active_session": "relay", "sessions": ["relay"]}})
    client.post("/channels/ops/join", json={"agent_id": "receiver"})

    result = runner.invoke(app, ["send", "-m", "inspect the logs", "-a", "receiver"])

    assert result.exit_code != 0
    threads = client.get("/threads", params={"channel_id": "ops"}).json()
    messages = client.get(f"/threads/{threads[0]['thread_id']}/messages").json()
    assert "no tmux pane titled `receiver` found in window `relay:ops`" in messages[0]["delivery_error"]
