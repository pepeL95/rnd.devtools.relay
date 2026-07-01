from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import typer
import uvicorn

from .api import create_app

app = typer.Typer(help="Relay CLI.")
config_app = typer.Typer(help="Configure the local relay workspace.", invoke_without_command=True)
app.add_typer(config_app, name="config")

RELAY_DIR = Path(".relay")
CONFIG_PATH = RELAY_DIR / "config.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url.rstrip("/"), timeout=10.0)


def _print(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "default"


def _normalize_message_text(message: str) -> str:
    return message.replace("\\r\\n", "\n").replace("\\n", "\n")


def _bridge_thread_id(channel_id: str, agent_a: str, agent_b: str) -> str:
    pair = sorted([agent_a, agent_b])
    digest = hashlib.sha1(f"{channel_id}:{pair[0]}:{pair[1]}".encode()).hexdigest()[:10]
    return f"bridge-{_slugify(channel_id)}-{_slugify(pair[0])}-{_slugify(pair[1])}-{digest}"


def _default_config() -> dict[str, Any]:
    return {
        "base_url": DEFAULT_BASE_URL,
        "agent_id": None,
        "channels": [],
        "active_channel": None,
    }


def _ensure_relay_dir() -> None:
    RELAY_DIR.mkdir(parents=True, exist_ok=True)


def _load_config(required: bool = True) -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        if required:
            raise typer.BadParameter("relay is not initialized in this directory. Run `relay init` first.")
        return _default_config()
    return json.loads(CONFIG_PATH.read_text())


def _save_config(config: dict[str, Any]) -> None:
    _ensure_relay_dir()
    CONFIG_PATH.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


def _apply_config_updates(
    config_data: dict[str, Any],
    *,
    agent_id: str | None = None,
    channels: list[str] | None = None,
    base_url: str | None = None,
    active_channel: str | None = None,
) -> dict[str, Any]:
    if agent_id is not None:
        config_data["agent_id"] = agent_id
    if base_url is not None:
        config_data["base_url"] = base_url.rstrip("/")
    if channels:
        existing = list(config_data.get("channels") or [])
        for item in channels:
            if item not in existing:
                existing.append(item)
        config_data["channels"] = existing
        if not config_data.get("active_channel"):
            config_data["active_channel"] = existing[0]
    if active_channel is not None:
        config_data["active_channel"] = active_channel
    return config_data


def _require_agent_config(config: dict[str, Any]) -> str:
    agent_id = config.get("agent_id")
    if not agent_id:
        raise typer.BadParameter("agent is not configured. Run `relay config --agent-id ... --channel ...`.")
    return str(agent_id)


def _require_active_channel(config: dict[str, Any], override: str | None = None) -> str:
    channel_id = override or config.get("active_channel")
    if not channel_id:
        raise typer.BadParameter("no active channel configured. Run `relay config --channel ...` first.")
    return str(channel_id)


def _register_participant(client: httpx.Client, agent_id: str) -> None:
    response = client.post("/participants", json={"agent_id": agent_id, "metadata": {}})
    response.raise_for_status()


def _ensure_channel(client: httpx.Client, channel_id: str) -> None:
    response = client.get("/channels")
    response.raise_for_status()
    channels = response.json()
    if any(channel["channel_id"] == channel_id for channel in channels):
        return
    create = client.post("/channels", json={"channel_id": channel_id, "name": channel_id, "metadata": {}})
    create.raise_for_status()


def _join_channel(client: httpx.Client, channel_id: str, agent_id: str) -> None:
    _ensure_channel(client, channel_id)
    response = client.post(f"/channels/{channel_id}/join", json={"agent_id": agent_id})
    response.raise_for_status()


def _ensure_config_registration(config: dict[str, Any], channel_override: str | None = None) -> tuple[str, str, str]:
    agent_id = _require_agent_config(config)
    base_url = str(config.get("base_url") or DEFAULT_BASE_URL)
    channel_id = _require_active_channel(config, override=channel_override)
    with _client(base_url) as client:
        _register_participant(client, agent_id)
        _join_channel(client, channel_id, agent_id)
    return base_url, agent_id, channel_id


def _list_channel_participants(client: httpx.Client, channel_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/channels/{channel_id}/participants")
    response.raise_for_status()
    return list(response.json())


def _ensure_recipient_exists(client: httpx.Client, channel_id: str, recipient_agent_id: str) -> None:
    participants = [item for item in _list_channel_participants(client, channel_id) if item["agent_id"] == recipient_agent_id]
    if not participants:
        raise typer.BadParameter(
            f"recipient agent `{recipient_agent_id}` is not subscribed to channel `{channel_id}`. "
            "Use `relay ls` to inspect channel members."
        )


def _ensure_direct_thread(client: httpx.Client, channel_id: str, sender_agent_id: str, recipient_agent_id: str) -> str:
    pair = sorted([sender_agent_id, recipient_agent_id])
    thread_id = _bridge_thread_id(channel_id, pair[0], pair[1])
    response = client.get(f"/threads/{thread_id}")
    if response.status_code == 404:
        create = client.post(
            "/threads",
            json={
                "thread_id": thread_id,
                "channel_id": channel_id,
                "created_by_agent_id": sender_agent_id,
                "subject": f"{pair[0]} <-> {pair[1]}",
                "metadata": {
                    "kind": "direct_bridge",
                    "participants": pair,
                },
            },
        )
        create.raise_for_status()
    else:
        response.raise_for_status()
    return thread_id


def _get_thread(client: httpx.Client, thread_id: str) -> dict[str, Any]:
    response = client.get(f"/threads/{thread_id}")
    response.raise_for_status()
    return dict(response.json())


def _infer_thread_peer(thread: dict[str, Any], local_agent_id: str, channel_id: str) -> str:
    if thread.get("channel_id") != channel_id:
        raise typer.BadParameter(f"thread `{thread['thread_id']}` does not belong to active channel `{channel_id}`.")
    participants = list((thread.get("metadata") or {}).get("participants") or [])
    if len(participants) != 2:
        raise typer.BadParameter(f"thread `{thread['thread_id']}` is not a direct a2a bridge.")
    if local_agent_id not in participants:
        raise typer.BadParameter(f"agent `{local_agent_id}` is not a participant in thread `{thread['thread_id']}`.")
    return participants[0] if participants[1] == local_agent_id else participants[1]


def _tmux_target(agent_id: str, channel_id: str) -> str:
    return f"{_slugify(agent_id)}__{_slugify(channel_id)}"


def _render_delivery_message(envelope: dict[str, Any]) -> str:
    return envelope["payload"]


def _tmux_session_exists(target: str) -> bool:
    result = subprocess.run(["tmux", "has-session", "-t", target], capture_output=True, text=True)
    return result.returncode == 0


def _inject_tmux(target: str, text: str) -> None:
    buffer_name = f"relay-{uuid4()}"
    subprocess.run(["tmux", "set-buffer", "-b", buffer_name, text], check=True)
    subprocess.run(["tmux", "paste-buffer", "-d", "-p", "-b", buffer_name, "-t", target], check=True)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)


def _deliver_envelope_via_tmux(client: httpx.Client, envelope: dict[str, Any]) -> dict[str, Any]:
    target = _tmux_target(envelope["recipient_agent_id"], envelope["channel_id"])
    try:
        if not _tmux_session_exists(target):
            error = f"tmux session `{target}` not found"
            client.post(f"/messages/{envelope['envelope_id']}/delivery-failed", json={"error": error}).raise_for_status()
            typer.secho(error, err=True)
            raise typer.Exit(code=1)

        _inject_tmux(target, _render_delivery_message(envelope))
        delivered = client.post(f"/messages/{envelope['envelope_id']}/delivered")
        delivered.raise_for_status()
        return dict(delivered.json())
    except (OSError, subprocess.CalledProcessError) as exc:
        error = f"tmux injection failed: {exc}"
        client.post(f"/messages/{envelope['envelope_id']}/delivery-failed", json={"error": error}).raise_for_status()
        typer.secho(error, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    db_path: Path = Path("var/relay.db"),
    node_id: str = "local",
) -> None:
    """Run the relay API and observability UI."""
    uvicorn.run(create_app(db_path=db_path, node_id=node_id), host=host, port=port)


@app.command()
def init(base_url: str = DEFAULT_BASE_URL) -> None:
    """Create local relay workspace state in the current directory."""
    config = _load_config(required=False)
    config["base_url"] = base_url.rstrip("/")
    _save_config(config)
    typer.echo(f"initialized relay workspace at {CONFIG_PATH}")


def _finalize_config(config_data: dict[str, Any]) -> dict[str, Any]:
    agent = _require_agent_config(config_data)
    channels = list(config_data.get("channels") or [])
    if not channels:
        raise typer.BadParameter("at least one `--channel` is required.")
    if config_data.get("active_channel") not in channels:
        config_data["active_channel"] = channels[0]

    with _client(str(config_data["base_url"])) as client:
        _register_participant(client, agent)
        for channel_id in channels:
            _join_channel(client, channel_id, agent)

    _save_config(config_data)
    return config_data


@config_app.callback()
def config(
    ctx: typer.Context,
    agent_id: str | None = typer.Option(None, "--agent-id", "-a"),
    channel: list[str] | None = typer.Option(None, "--channel", "-c"),
    base_url: str | None = typer.Option(None, "--base-url"),
    active_channel: str | None = typer.Option(None, "--active-channel"),
) -> None:
    """Configure the local agent identity and register it to channel memberships."""
    if ctx.invoked_subcommand is not None:
        return
    config_data = _load_config(required=True)
    config_data = _apply_config_updates(
        config_data,
        agent_id=agent_id,
        channels=channel,
        base_url=base_url,
        active_channel=active_channel,
    )
    updated = _finalize_config(config_data)
    _print(updated)


@config_app.command("show")
def config_show() -> None:
    """Show the local relay configuration for orientation."""
    config_data = _load_config(required=True)
    _print(
        {
            "agent_id": config_data.get("agent_id"),
            "active_channel": config_data.get("active_channel"),
            "channels": config_data.get("channels") or [],
            "base_url": config_data.get("base_url") or DEFAULT_BASE_URL,
        }
    )


@app.command()
def register(
    agent: str = typer.Option(..., "--agent", "-a"),
    channel: str = typer.Option(..., "--channel", "-c"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """Register an agent and subscribe it to a channel without changing local config."""
    config_data = _load_config(required=False)
    base = (base_url or config_data.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    with _client(base) as client:
        _register_participant(client, agent)
        _join_channel(client, channel, agent)
    _print({"agent_id": agent, "channel_id": channel, "base_url": base, "registered": True})


@app.command()
def send(
    message: str = typer.Option(..., "--message", "-m"),
    agent: str = typer.Option(..., "--agent", "-a"),
    channel: str | None = typer.Option(None, "--channel", "-c"),
) -> None:
    """Send a message to a recipient agent using the configured relay workspace."""
    message = _normalize_message_text(message)
    config_data = _load_config(required=True)
    base_url, sender_agent_id, channel_id = _ensure_config_registration(config_data, channel_override=channel)
    with _client(base_url) as client:
        _ensure_recipient_exists(client, channel_id, agent)
        thread_id = _ensure_direct_thread(client, channel_id, sender_agent_id, agent)
        response = client.post(
            "/messages",
            json={
                "envelope_id": f"env-{uuid4()}",
                "channel_id": channel_id,
                "thread_id": thread_id,
                "sender_agent_id": sender_agent_id,
                "recipient_agent_id": agent,
                "recipient_node": "local",
                "payload": message,
                "metadata": {},
            },
        )
        response.raise_for_status()
        delivered = _deliver_envelope_via_tmux(client, dict(response.json()))
        _print(delivered)


@app.command()
def respond(
    message: str = typer.Option(..., "--message", "-m"),
    thread: str = typer.Option(..., "--thread", "-t"),
    channel: str | None = typer.Option(None, "--channel", "-c"),
) -> None:
    """Reply to the peer on an existing direct bridge thread."""
    message = _normalize_message_text(message)
    config_data = _load_config(required=True)
    base_url, sender_agent_id, channel_id = _ensure_config_registration(config_data, channel_override=channel)
    with _client(base_url) as client:
        thread_data = _get_thread(client, thread)
        recipient_agent_id = _infer_thread_peer(thread_data, sender_agent_id, channel_id)
        _ensure_recipient_exists(client, channel_id, recipient_agent_id)
        response = client.post(
            "/messages",
            json={
                "envelope_id": f"env-{uuid4()}",
                "channel_id": channel_id,
                "thread_id": thread,
                "sender_agent_id": sender_agent_id,
                "recipient_agent_id": recipient_agent_id,
                "recipient_node": "local",
                "payload": message,
                "metadata": {},
            },
        )
        response.raise_for_status()
        _print(response.json())


@app.command("ls")
def list_agents(
    all_agents: bool = typer.Option(False, "--all", "-a"),
    channel: str | None = typer.Option(None, "--channel", "-c"),
) -> None:
    """List discoverable agents in the active channel, or all registered agents."""
    config_data = _load_config(required=True)
    base_url = str(config_data.get("base_url") or DEFAULT_BASE_URL)
    channel_id = None if all_agents else _require_active_channel(config_data, override=channel)
    with _client(base_url) as client:
        if all_agents:
            response = client.get("/participants")
        else:
            response = client.get(f"/channels/{channel_id}/participants")
        response.raise_for_status()
        _print(response.json())


@app.command()
def history(
    thread_id: str,
    channel: str | None = typer.Option(None, "--channel", "-c"),
) -> None:
    """Read message history for a thread."""
    config_data = _load_config(required=True)
    base_url, _, _ = _ensure_config_registration(config_data, channel_override=channel)
    with _client(base_url) as client:
        response = client.get(f"/threads/{thread_id}/messages")
        response.raise_for_status()
        _print(response.json())


@app.command()
def tail(
    limit: int = 25,
    channel: str | None = typer.Option(None, "--channel", "-c"),
) -> None:
    """Read recent protocol events."""
    config_data = _load_config(required=True)
    base_url, _, channel_id = _ensure_config_registration(config_data, channel_override=channel)
    with _client(base_url) as client:
        response = client.get("/events", params={"limit": limit, "channel_id": channel_id})
        response.raise_for_status()
        _print(response.json())


if __name__ == "__main__":
    app()
