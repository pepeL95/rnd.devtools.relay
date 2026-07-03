from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import click
import httpx
import typer
import uvicorn

from .api import create_app
from .protocol import infer_thread_peer, session_bridge_thread_id
from .tmux import (
    TmuxDeliveryError,
    inject_tmux,
    recipient_target_session,
    render_delivery_message,
    resolve_tmux_pane_target,
    tmux_session_exists,
)

app = typer.Typer(help="Relay CLI.")
config_app = typer.Typer(help="Configure the local relay workspace.", invoke_without_command=True)
app.add_typer(config_app, name="config")

RELAY_DIR = Path(".relay")
CONFIG_PATH = RELAY_DIR / "config.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

# Compatibility aliases keep the existing CLI test/mocking surface stable
# while tmux delivery logic lives in its own adapter module.
_tmux_session_exists = tmux_session_exists
_resolve_tmux_pane_target = resolve_tmux_pane_target
_inject_tmux = inject_tmux
_render_delivery_message = render_delivery_message
_recipient_target_session = recipient_target_session
_infer_thread_peer = infer_thread_peer


def _tmux_attach_command(session_id: str, channel_id: str | None = None, pane_target: str | None = None) -> str:
    command = f"tmux attach -t {session_id}"
    if channel_id:
        command += f" \\; select-window -t {session_id}:{channel_id}"
    if pane_target:
        command += f" \\; select-pane -t {pane_target}"
    return command


def _run_tmux(
    args: list[str],
    *,
    action: str,
    mitigation: str,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    try:
        return subprocess.run(
            ["tmux", *args],
            check=True,
            text=True,
            capture_output=capture_output,
            env=env,
        )
    except FileNotFoundError as exc:
        raise click.ClickException("tmux is not installed or not available on PATH. Install tmux and retry.") from exc
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        raise click.ClickException(f"could not {action}: {message}. {mitigation}") from exc


def _list_tmux_panes(session_id: str, channel_id: str) -> list[dict[str, str]]:
    result = _run_tmux(
        ["list-panes", "-t", f"{session_id}:{channel_id}", "-F", "#{pane_id}\t#{pane_title}"],
        action=f"list panes in `{session_id}:{channel_id}`",
        mitigation="Confirm the tmux session and window exist, then retry.",
        capture_output=True,
    )
    panes: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        pane_id, _, pane_title = line.partition("\t")
        panes.append({"pane_id": pane_id.strip(), "pane_title": pane_title.strip()})
    return panes


def _list_tmux_names(args: list[str], *, action: str, mitigation: str) -> list[str]:
    result = _run_tmux(args, action=action, mitigation=mitigation, capture_output=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _prepare_tmux_agent_pane(pane_id: str, agent_id: str, session_id: str, channel_id: str) -> None:
    _run_tmux(
        ["set-option", "-p", "-t", pane_id, "allow-set-title", "off"],
        action=f"disable shell-driven title changes for pane `{pane_id}` in `{session_id}:{channel_id}`",
        mitigation="Confirm the tmux target exists, then retry.",
    )
    _run_tmux(
        ["select-pane", "-t", pane_id, "-T", agent_id],
        action=f"title pane `{pane_id}` as `{agent_id}`",
        mitigation="Confirm the target window still exists, then retry.",
    )


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url.rstrip("/"), timeout=10.0)


def _print(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def _normalize_message_text(message: str) -> str:
    return message.replace("\\r\\n", "\n").replace("\\n", "\n")


def _response_detail(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if detail:
            return str(detail)
    return None


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    action: str,
    mitigation: str,
    allow_statuses: set[int] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    try:
        response = client.request(method, path, **kwargs)
    except httpx.ConnectError as exc:
        raise click.ClickException(
            f"could not reach relay at `{client.base_url}` while trying to {action}. "
            f"Start the server with `relay serve --host 127.0.0.1 --port 8000` or update `relay config --base-url ...`."
        ) from exc
    except httpx.TimeoutException as exc:
        raise click.ClickException(
            f"relay timed out while trying to {action}. Confirm the server is healthy and retry. "
            f"If needed, verify connectivity to `{client.base_url}`."
        ) from exc

    if response.is_success or (allow_statuses and response.status_code in allow_statuses):
        return response

    detail = _response_detail(response)
    message = detail or response.reason_phrase
    if response.status_code in {400, 404, 409, 422}:
        raise click.ClickException(f"could not {action}: {message}. {mitigation}")
    if response.status_code >= 500:
        raise click.ClickException(
            f"relay server error while trying to {action}: {message}. Check the relay server logs and retry."
        )
    raise click.ClickException(f"could not {action}: {message}. {mitigation}")


def _default_config() -> dict[str, Any]:
    return {
        "base_url": DEFAULT_BASE_URL,
        "agent_id": None,
        "description": None,
        "channels": [],
        "active_channel": None,
        "sessions": [],
        "active_session": None,
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
    description: str | None = None,
    channels: list[str] | None = None,
    sessions: list[str] | None = None,
    base_url: str | None = None,
    active_channel: str | None = None,
    active_session: str | None = None,
) -> dict[str, Any]:
    if agent_id is not None:
        config_data["agent_id"] = agent_id
    if description is not None:
        config_data["description"] = description.strip() or None
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
    if sessions:
        existing = list(config_data.get("sessions") or [])
        for item in sessions:
            if item not in existing:
                existing.append(item)
        config_data["sessions"] = existing
        if not config_data.get("active_session"):
            config_data["active_session"] = existing[0]
    if active_channel is not None:
        config_data["active_channel"] = active_channel
    if active_session is not None:
        config_data["active_session"] = active_session
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


def _require_active_session(config: dict[str, Any]) -> str:
    session_id = config.get("active_session")
    if not session_id:
        raise typer.BadParameter("no active session configured. Run `relay config --session ...` first.")
    return str(session_id)


def _participant_metadata_from_config(config: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "description": config.get("description"),
        "channels": list(config.get("channels") or []),
        "active_channel": config.get("active_channel"),
        "sessions": list(config.get("sessions") or []),
        "active_session": config.get("active_session"),
    }
    return {key: value for key, value in metadata.items() if value not in (None, [], "")}


def _participant_metadata(
    *,
    description: str | None = None,
    channels: list[str] | None = None,
    active_channel: str | None = None,
    sessions: list[str] | None = None,
    active_session: str | None = None,
) -> dict[str, Any]:
    metadata = {
        "description": description.strip() if description else None,
        "channels": list(channels or []),
        "active_channel": active_channel,
        "sessions": list(sessions or []),
        "active_session": active_session,
    }
    return {key: value for key, value in metadata.items() if value not in (None, [], "")}


def _format_participant_listing(participant: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(participant.get("metadata") or {})
    return {
        "agent_id": participant["agent_id"],
        "description": metadata.get("description"),
        "comms": {
            "home_node": participant.get("home_node"),
            "presence": participant.get("presence"),
            "active_channel": metadata.get("active_channel"),
            "channels": list(metadata.get("channels") or []),
            "active_session": metadata.get("active_session"),
            "sessions": list(metadata.get("sessions") or []),
        },
    }


def _participant_is_on_session(participant: dict[str, Any], session_id: str) -> bool:
    metadata = dict(participant.get("metadata") or {})
    active_session = metadata.get("active_session")
    if active_session:
        return str(active_session) == session_id
    return session_id in {str(item) for item in metadata.get("sessions") or []}


def _register_participant(client: httpx.Client, agent_id: str, metadata: dict[str, Any] | None = None) -> None:
    _request(
        client,
        "POST",
        "/participants",
        action=f"register agent `{agent_id}`",
        mitigation="Verify the agent id and local config, then retry.",
        json={"agent_id": agent_id, "metadata": metadata or {}},
    )


def _ensure_channel(client: httpx.Client, channel_id: str) -> None:
    response = _request(
        client,
        "GET",
        "/channels",
        action="list channels",
        mitigation="Confirm the relay server is running and your base URL is correct.",
    )
    channels = response.json()
    if any(channel["channel_id"] == channel_id for channel in channels):
        return
    _request(
        client,
        "POST",
        "/channels",
        action=f"create channel `{channel_id}`",
        mitigation="Check that the channel id is valid and retry.",
        json={"channel_id": channel_id, "name": channel_id, "metadata": {}},
    )


def _join_channel(client: httpx.Client, channel_id: str, agent_id: str) -> None:
    _ensure_channel(client, channel_id)
    _request(
        client,
        "POST",
        f"/channels/{channel_id}/join",
        action=f"join channel `{channel_id}` as `{agent_id}`",
        mitigation="Confirm the channel exists and the agent is registered, then retry.",
        json={"agent_id": agent_id},
    )


def _ensure_config_registration(config: dict[str, Any], channel_override: str | None = None) -> tuple[str, str, str, str]:
    agent_id = _require_agent_config(config)
    base_url = str(config.get("base_url") or DEFAULT_BASE_URL)
    channel_id = _require_active_channel(config, override=channel_override)
    session_id = _require_active_session(config)
    with _client(base_url) as client:
        _register_participant(client, agent_id, metadata=_participant_metadata_from_config(config))
        _join_channel(client, channel_id, agent_id)
    return base_url, agent_id, channel_id, session_id


def _list_channel_participants(client: httpx.Client, channel_id: str) -> list[dict[str, Any]]:
    response = _request(
        client,
        "GET",
        f"/channels/{channel_id}/participants",
        action=f"list participants in channel `{channel_id}`",
        mitigation="Confirm the channel exists and your config points at the right relay.",
    )
    return list(response.json())


def _ensure_recipient_exists(client: httpx.Client, channel_id: str, recipient_agent_id: str) -> None:
    participants = [item for item in _list_channel_participants(client, channel_id) if item["agent_id"] == recipient_agent_id]
    if not participants:
        raise typer.BadParameter(
            f"recipient agent `{recipient_agent_id}` is not subscribed to channel `{channel_id}`. "
            "Use `relay ls` to inspect channel members."
        )


def _ensure_direct_thread(
    client: httpx.Client, session_id: str, channel_id: str, sender_agent_id: str, recipient_agent_id: str
) -> str:
    pair = sorted([sender_agent_id, recipient_agent_id])
    thread_id = session_bridge_thread_id(session_id, channel_id, pair[0], pair[1])
    response = _request(
        client,
        "GET",
        f"/threads/{thread_id}",
        action=f"load thread `{thread_id}`",
        mitigation="Confirm the relay server is healthy and retry.",
        allow_statuses={404},
    )
    if response.status_code == 404:
        _request(
            client,
            "POST",
            "/threads",
            action=f"create thread `{thread_id}`",
            mitigation="Check the channel membership and retry.",
            json={
                "thread_id": thread_id,
                "channel_id": channel_id,
                "created_by_agent_id": sender_agent_id,
                "subject": f"{pair[0]} <-> {pair[1]}",
                "metadata": {
                    "kind": "direct_bridge",
                    "participants": pair,
                    "session_id": session_id,
                },
            },
        )
    return thread_id


def _get_thread(client: httpx.Client, thread_id: str) -> dict[str, Any]:
    response = _request(
        client,
        "GET",
        f"/threads/{thread_id}",
        action=f"load thread `{thread_id}`",
        mitigation=f"Check the id with `relay history {thread_id}` or start a new exchange with `relay send -m \"...\" -a RECIPIENT_AGENT_ID`.",
    )
    return dict(response.json())


def _list_thread_messages(client: httpx.Client, thread_id: str) -> list[dict[str, Any]]:
    response = _request(
        client,
        "GET",
        f"/threads/{thread_id}/messages",
        action=f"read history for thread `{thread_id}`",
        mitigation=f"Check the id with `relay history {thread_id}` or start a new exchange with `relay send -m \"...\" -a RECIPIENT_AGENT_ID`.",
    )
    return list(response.json())


def _get_open_request(client: httpx.Client, thread_id: str, agent_id: str) -> dict[str, Any]:
    response = _request(
        client,
        "GET",
        f"/threads/{thread_id}/open-request",
        action=f"find the open request on thread `{thread_id}` for `{agent_id}`",
        mitigation=f"Inspect `relay history {thread_id}` and use `relay ack -t {thread_id}` if the exchange is already resolved.",
        params={"agent_id": agent_id},
    )
    return dict(response.json())


def _deliver_envelope_via_tmux(client: httpx.Client, envelope: dict[str, Any], participant: dict[str, Any]) -> dict[str, Any]:
    session_id = _recipient_target_session(participant)
    try:
        if not _tmux_session_exists(session_id):
            error = f"tmux session `{session_id}` not found"
            _request(
                client,
                "POST",
                f"/messages/{envelope['envelope_id']}/delivery-failed",
                action=f"mark delivery failure for envelope `{envelope['envelope_id']}`",
                mitigation="Check the relay server logs and retry the send.",
                json={"error": error},
            )
            typer.secho(error, err=True)
            raise typer.Exit(code=1)
        target = _resolve_tmux_pane_target(session_id, envelope["channel_id"], envelope["recipient_agent_id"])

        _inject_tmux(target, _render_delivery_message(envelope))
        delivered = _request(
            client,
            "POST",
            f"/messages/{envelope['envelope_id']}/delivered",
            action=f"mark envelope `{envelope['envelope_id']}` as delivered",
            mitigation="Check the relay server logs and retry the send.",
        )
        return dict(delivered.json())
    except (OSError, subprocess.CalledProcessError, TmuxDeliveryError, typer.BadParameter) as exc:
        error = f"tmux injection failed: {exc}"
        _request(
            client,
            "POST",
            f"/messages/{envelope['envelope_id']}/delivery-failed",
            action=f"mark delivery failure for envelope `{envelope['envelope_id']}`",
            mitigation="Check the relay server logs and retry the send.",
            json={"error": error},
        )
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


@app.command("create")
def create_tmux_session(
    session: str = typer.Option(..., "--session", "-s"),
    channel: str = typer.Option(..., "--channel", "-c"),
    agent: str | None = typer.Option(None, "--agent", "-a"),
) -> None:
    """Create a detached tmux session with its first relay channel window."""
    _run_tmux(
        ["new-session", "-d", "-s", session, "-n", channel],
        action=f"create tmux session `{session}` with channel `{channel}`",
        mitigation="Check that the session name is not already in use, then retry.",
    )
    if agent:
        panes = _list_tmux_panes(session, channel)
        if len(panes) != 1 or not panes[0]["pane_id"]:
            raise click.ClickException(
                f"could not title the default pane for `{session}:{channel}` as `{agent}`. Inspect the tmux window and retry."
            )
        _prepare_tmux_agent_pane(panes[0]["pane_id"], agent, session, channel)
    typer.echo(_tmux_attach_command(session, channel))


@app.command("add-channel")
def add_tmux_channel(
    session: str = typer.Option(..., "--session", "-s"),
    channel: str = typer.Option(..., "--channel", "-c"),
) -> None:
    """Add a tmux window for a relay channel under an existing session."""
    _run_tmux(
        ["new-window", "-t", session, "-n", channel],
        action=f"add channel `{channel}` to tmux session `{session}`",
        mitigation="Confirm the tmux session exists and the window name is not already in use.",
    )
    typer.echo(_tmux_attach_command(session, channel))


@app.command("add-agent")
def add_tmux_agent(
    session: str = typer.Option(..., "--session", "-s"),
    channel: str = typer.Option(..., "--channel", "-c"),
    agent: str = typer.Option(..., "--agent", "-a"),
) -> None:
    """Add a titled tmux pane for an agent inside a channel window."""
    panes = _list_tmux_panes(session, channel)
    if len(panes) == 1 and not panes[0]["pane_title"]:
        pane_id = panes[0]["pane_id"]
    else:
        split = _run_tmux(
            ["split-window", "-h", "-t", f"{session}:{channel}", "-P", "-F", "#{pane_id}"],
            action=f"add agent `{agent}` to `{session}:{channel}`",
            mitigation="Confirm the tmux session and window exist, then retry.",
            capture_output=True,
        )
        pane_id = split.stdout.strip()
        if not pane_id:
            raise click.ClickException(
                f"could not add agent `{agent}` to `{session}:{channel}`: tmux did not return a new pane id. Retry the command."
            )
    _prepare_tmux_agent_pane(pane_id, agent, session, channel)
    typer.echo(_tmux_attach_command(session, channel, pane_id))


@app.command("delete-session")
def delete_tmux_session(session: str) -> None:
    """Delete a tmux session."""
    _run_tmux(
        ["kill-session", "-t", session],
        action=f"delete tmux session `{session}`",
        mitigation="Confirm the session exists with `tmux list-sessions` and retry.",
    )
    typer.echo(f"deleted tmux session `{session}`")


@app.command("delete-channel")
def delete_tmux_channel(
    session: str = typer.Option(..., "--session", "-s"),
    channel: str = typer.Option(..., "--channel", "-c"),
) -> None:
    """Delete a tmux window used as a relay channel."""
    _run_tmux(
        ["kill-window", "-t", f"{session}:{channel}"],
        action=f"delete channel `{channel}` from tmux session `{session}`",
        mitigation="Confirm the session and window exist with `tmux list-windows -t SESSION` and retry.",
    )
    typer.echo(f"deleted tmux channel `{session}:{channel}`")


@app.command("delete-agent")
def delete_tmux_agent(
    session: str = typer.Option(..., "--session", "-s"),
    channel: str = typer.Option(..., "--channel", "-c"),
    agent: str = typer.Option(..., "--agent", "-a"),
) -> None:
    """Delete a titled tmux pane used as a relay agent."""
    try:
        pane_id = _resolve_tmux_pane_target(session, channel, agent)
    except TmuxDeliveryError as exc:
        raise click.ClickException(
            f"could not delete agent `{agent}` from `{session}:{channel}`: {exc}. Confirm the pane title matches the agent name and retry."
        ) from exc
    _run_tmux(
        ["kill-pane", "-t", pane_id],
        action=f"delete agent `{agent}` from `{session}:{channel}`",
        mitigation="Confirm the pane title is unique in that window and retry.",
    )
    typer.echo(f"deleted tmux agent `{agent}` from `{session}:{channel}`")


@app.command("attach")
def attach_tmux_session(
    session: str = typer.Option(..., "--session", "-s"),
    channel: str = typer.Option(..., "--channel", "-c"),
    agent: str | None = typer.Option(None, "--agent", "-a"),
) -> None:
    """Attach to a tmux session and select a relay channel, optionally focusing an agent pane."""
    pane_target = None
    if agent:
        try:
            pane_target = _resolve_tmux_pane_target(session, channel, agent)
        except TmuxDeliveryError as exc:
            raise click.ClickException(
                f"could not attach to agent `{agent}` in `{session}:{channel}`: {exc}. Confirm the pane title matches the agent name and retry."
            ) from exc
    _run_tmux(
        ["attach", "-t", session, ";", "select-window", "-t", f"{session}:{channel}", *([] if pane_target is None else [";", "select-pane", "-t", pane_target])],
        action=f"attach to tmux session `{session}` and select channel `{channel}`",
        mitigation="Confirm the session and channel exist with `relay list-sessions` and `relay list-channels -s SESSION`, then retry.",
    )


@app.command("list-sessions")
def list_tmux_sessions() -> None:
    """List tmux session names."""
    sessions = _list_tmux_names(
        ["list-sessions", "-F", "#{session_name}"],
        action="list tmux sessions",
        mitigation="Confirm tmux is running and retry.",
    )
    typer.echo("\n".join(sessions))


@app.command("list-channels")
def list_tmux_channels(
    session: str = typer.Option(..., "--session", "-s"),
) -> None:
    """List tmux windows under a session."""
    channels = _list_tmux_names(
        ["list-windows", "-t", session, "-F", "#{window_name}"],
        action=f"list channels in tmux session `{session}`",
        mitigation="Confirm the session exists with `relay list-sessions` and retry.",
    )
    typer.echo("\n".join(channels))


def _finalize_config(config_data: dict[str, Any]) -> dict[str, Any]:
    agent = _require_agent_config(config_data)
    channels = list(config_data.get("channels") or [])
    sessions = list(config_data.get("sessions") or [])
    if not channels:
        raise typer.BadParameter("at least one `--channel` is required.")
    if not sessions:
        raise typer.BadParameter("at least one `--session` is required.")
    if config_data.get("active_channel") not in channels:
        config_data["active_channel"] = channels[0]
    if config_data.get("active_session") not in sessions:
        config_data["active_session"] = sessions[0]

    with _client(str(config_data["base_url"])) as client:
        _register_participant(client, agent, metadata=_participant_metadata_from_config(config_data))
        for channel_id in channels:
            _join_channel(client, channel_id, agent)

    _save_config(config_data)
    return config_data


@config_app.callback()
def config(
    ctx: typer.Context,
    agent_id: str | None = typer.Option(None, "--agent-id", "-a"),
    description: str | None = typer.Option(None, "--description", "-d"),
    channel: list[str] | None = typer.Option(None, "--channel", "-c"),
    session: list[str] | None = typer.Option(None, "--session", "-s"),
    base_url: str | None = typer.Option(None, "--base-url"),
    active_channel: str | None = typer.Option(None, "--active-channel"),
    active_session: str | None = typer.Option(None, "--active-session"),
    clear: bool = typer.Option(False, "--clear"),
) -> None:
    """Configure the local agent identity and register it to channel memberships."""
    if ctx.invoked_subcommand is not None:
        return
    config_data = _load_config(required=True)
    if clear:
        cleared = _default_config()
        cleared["base_url"] = (base_url or config_data.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        _save_config(cleared)
        _print(cleared)
        return
    config_data = _apply_config_updates(
        config_data,
        agent_id=agent_id,
        description=description,
        channels=channel,
        sessions=session,
        base_url=base_url,
        active_channel=active_channel,
        active_session=active_session,
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
            "description": config_data.get("description"),
            "active_channel": config_data.get("active_channel"),
            "active_session": config_data.get("active_session"),
            "channels": config_data.get("channels") or [],
            "sessions": config_data.get("sessions") or [],
            "base_url": config_data.get("base_url") or DEFAULT_BASE_URL,
        }
    )


@app.command()
def register(
    agent: str = typer.Option(..., "--agent", "-a"),
    channel: str = typer.Option(..., "--channel", "-c"),
    description: str | None = typer.Option(None, "--description", "-d"),
    session: list[str] | None = typer.Option(None, "--session", "-s"),
    active_session: str | None = typer.Option(None, "--active-session"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """Register an agent and subscribe it to a channel without changing local config."""
    config_data = _load_config(required=False)
    base = (base_url or config_data.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    active_session_value = active_session or (session[0] if session else None)
    with _client(base) as client:
        _register_participant(
            client,
            agent,
            metadata=_participant_metadata(
                description=description,
                channels=[channel],
                active_channel=channel,
                sessions=session,
                active_session=active_session_value,
            ),
        )
        _join_channel(client, channel, agent)
    _print(
        {
            "agent_id": agent,
            "description": description,
            "channel_id": channel,
            "sessions": session or [],
            "active_session": active_session_value,
            "base_url": base,
            "registered": True,
        }
    )


@app.command()
def send(
    message: str = typer.Option(..., "--message", "-m"),
    agent: str = typer.Option(..., "--agent", "-a"),
    channel: str | None = typer.Option(None, "--channel", "-c"),
) -> None:
    """Send a message to a recipient agent using the configured relay workspace."""
    message = _normalize_message_text(message)
    config_data = _load_config(required=True)
    base_url, sender_agent_id, channel_id, session_id = _ensure_config_registration(config_data, channel_override=channel)
    with _client(base_url) as client:
        participants = _list_channel_participants(client, channel_id)
        recipient = next((item for item in participants if item["agent_id"] == agent), None)
        if recipient is None:
            _ensure_recipient_exists(client, channel_id, agent)
            raise AssertionError("recipient lookup should have failed before reaching this point")
        thread_id = _ensure_direct_thread(client, session_id, channel_id, sender_agent_id, agent)
        response = _request(
            client,
            "POST",
            "/messages",
            action=f"send a message to `{agent}` on thread `{thread_id}`",
            mitigation=f"Verify `{agent}` is registered in the active channel with `relay ls`, then retry.",
            json={
                "envelope_id": f"env-{uuid4()}",
                "channel_id": channel_id,
                "thread_id": thread_id,
                "sender_agent_id": sender_agent_id,
                "recipient_agent_id": agent,
                "recipient_node": "local",
                "payload": message,
                "metadata": {"session_id": session_id, "kind": "request", "expects_response": True},
            },
        )
        delivered = _deliver_envelope_via_tmux(client, dict(response.json()), recipient)
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
    base_url, sender_agent_id, channel_id, session_id = _ensure_config_registration(config_data, channel_override=channel)
    with _client(base_url) as client:
        thread_data = _get_thread(client, thread)
        open_request = _get_open_request(client, thread, sender_agent_id)
        recipient_agent_id = _infer_thread_peer(thread_data, sender_agent_id, channel_id, session_id)
        participants = _list_channel_participants(client, channel_id)
        recipient = next((item for item in participants if item["agent_id"] == recipient_agent_id), None)
        if recipient is None:
            _ensure_recipient_exists(client, channel_id, recipient_agent_id)
            raise AssertionError("recipient lookup should have failed before reaching this point")
        response = _request(
            client,
            "POST",
            "/messages",
            action=f"respond on thread `{thread}` to `{recipient_agent_id}`",
            mitigation=f"Inspect `relay history {thread}` and confirm the thread still needs a follow-up.",
            json={
                "envelope_id": f"env-{uuid4()}",
                "channel_id": channel_id,
                "thread_id": thread,
                "sender_agent_id": sender_agent_id,
                "recipient_agent_id": recipient_agent_id,
                "recipient_node": "local",
                "payload": message,
                "metadata": {
                    "session_id": session_id,
                    "kind": "response",
                    "expects_response": False,
                    "reply_to_envelope_id": open_request["envelope_id"],
                },
            },
        )
        delivered = _deliver_envelope_via_tmux(client, dict(response.json()), recipient)
        _print(delivered)


@app.command()
def ack(
    thread: str = typer.Option(..., "--thread", "-t"),
    channel: str | None = typer.Option(None, "--channel", "-c"),
) -> None:
    """Acknowledge the latest inbound message on a thread for the configured agent."""
    config_data = _load_config(required=True)
    base_url, agent_id, _, _ = _ensure_config_registration(config_data, channel_override=channel)
    with _client(base_url) as client:
        _get_thread(client, thread)
        messages = _list_thread_messages(client, thread)
        envelope = next(
            (
                item
                for item in reversed(messages)
                if item["recipient_agent_id"] == agent_id and item["acked_at"] is None
            ),
            None,
        )
        if envelope is None:
            raise typer.BadParameter(
                f"no inbound unacknowledged message found for agent `{agent_id}` on thread `{thread}`."
            )
        response = _request(
            client,
            "POST",
            "/messages/ack",
            action=f"acknowledge thread `{thread}` for `{agent_id}`",
            mitigation=f"Inspect `relay history {thread}` and confirm the latest inbound message is addressed to `{agent_id}`.",
            json={"envelope_id": envelope["envelope_id"], "agent_id": agent_id},
        )
        _print(dict(response.json()))


@app.command("ls")
def list_agents(
    all_agents: bool = typer.Option(False, "--all", "-a"),
    channel: str | None = typer.Option(None, "--channel", "-c"),
) -> None:
    """List discoverable agents in the active channel, or all agents on the active session."""
    config_data = _load_config(required=True)
    base_url = str(config_data.get("base_url") or DEFAULT_BASE_URL)
    channel_id = None if all_agents else _require_active_channel(config_data, override=channel)
    active_session = _require_active_session(config_data)
    with _client(base_url) as client:
        if all_agents:
            response = _request(
                client,
                "GET",
                "/participants",
                action=f"list agents on active session `{active_session}`",
                mitigation="Confirm the relay server is running and your base URL is correct.",
            )
        else:
            response = _request(
                client,
                "GET",
                f"/channels/{channel_id}/participants",
                action=f"list agents in channel `{channel_id}`",
                mitigation="Confirm the channel exists and your config points at the right relay.",
            )
        participants = list(response.json())
        participants = [item for item in participants if _participant_is_on_session(item, active_session)]
        _print([_format_participant_listing(item) for item in participants])


@app.command()
def history(
    thread_id: str,
    channel: str | None = typer.Option(None, "--channel", "-c"),
) -> None:
    """Read message history for a thread."""
    config_data = _load_config(required=True)
    base_url, _, _, _ = _ensure_config_registration(config_data, channel_override=channel)
    with _client(base_url) as client:
        response = _request(
            client,
            "GET",
            f"/threads/{thread_id}/messages",
            action=f"read history for thread `{thread_id}`",
            mitigation=f"Check the id with `relay history {thread_id}` or start a new exchange with `relay send -m \"...\" -a RECIPIENT_AGENT_ID`.",
        )
        _print(response.json())


@app.command()
def tail(
    limit: int = 25,
    channel: str | None = typer.Option(None, "--channel", "-c"),
) -> None:
    """Read recent protocol events."""
    config_data = _load_config(required=True)
    base_url, _, channel_id, _ = _ensure_config_registration(config_data, channel_override=channel)
    with _client(base_url) as client:
        response = _request(
            client,
            "GET",
            "/events",
            action=f"read recent events for channel `{channel_id}`",
            mitigation="Confirm the relay server is running and your base URL is correct.",
            params={"limit": limit, "channel_id": channel_id},
        )
        _print(response.json())


if __name__ == "__main__":
    app()
