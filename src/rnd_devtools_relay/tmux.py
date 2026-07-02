from __future__ import annotations

import subprocess
from typing import Any
from uuid import uuid4


class TmuxDeliveryError(RuntimeError):
    pass


def recipient_target_session(participant: dict[str, Any]) -> str:
    metadata = participant.get("metadata") or {}
    session_id = metadata.get("active_session")
    if not session_id:
        raise TmuxDeliveryError(
            f"recipient agent `{participant['agent_id']}` does not have an active session configured. "
            "Have that agent run `relay config --session ...` first."
        )
    return str(session_id)


def render_delivery_message(envelope: dict[str, Any]) -> str:
    thread_id = envelope["thread_id"]
    metadata = envelope.get("metadata") or {}
    session_id = metadata.get("session_id", "-")
    kind = str(metadata.get("kind") or "request")
    if kind == "response":
        return (
            "You received a relay response from another agent.\n"
            f"Sender: {envelope['sender_agent_id']}\n"
            f"Session: {session_id}\n"
            f"Channel: {envelope['channel_id']}\n"
            f"Thread: {thread_id}\n"
            "No reply is expected for this response.\n"
            "If you need more work, open a new request with:\n"
            f"relay send -m \"<follow-up request>\" -a {envelope['sender_agent_id']}\n"
            "\n"
            "Incoming message:\n"
            f"{envelope['payload']}"
        )
    return (
        "You received a relay message from another agent.\n"
        f"Sender: {envelope['sender_agent_id']}\n"
        f"Session: {session_id}\n"
        f"Channel: {envelope['channel_id']}\n"
        f"Thread: {thread_id}\n"
        "Reply command: "
        f"relay respond -m \"<your response>\" -t {thread_id}\n"
        "\n"
        "Incoming message:\n"
        f"{envelope['payload']}"
    )


def tmux_session_exists(target: str) -> bool:
    result = subprocess.run(["tmux", "has-session", "-t", target], capture_output=True, text=True)
    return result.returncode == 0


def resolve_tmux_pane_target(session_id: str, channel_id: str, agent_id: str) -> str:
    target_window = f"{session_id}:{channel_id}"
    result = subprocess.run(
        ["tmux", "list-panes", "-t", target_window, "-F", "#{pane_id}\t#{pane_title}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TmuxDeliveryError(
            f"tmux window `{target_window}` not found. Create it and title the recipient pane `{agent_id}`."
        )

    matches: list[str] = []
    for line in result.stdout.splitlines():
        pane_id, _, pane_title = line.partition("\t")
        if pane_title.strip() == agent_id:
            matches.append(pane_id.strip())

    if not matches:
        raise TmuxDeliveryError(
            f"no tmux pane titled `{agent_id}` found in window `{target_window}`. "
            "Set the pane title to the agent name with `tmux select-pane -T <agent>`."
        )
    if len(matches) > 1:
        raise TmuxDeliveryError(
            f"multiple tmux panes titled `{agent_id}` found in window `{target_window}`. "
            "Ensure pane titles are unique per channel window."
        )
    return matches[0]


def inject_tmux(target: str, text: str) -> None:
    buffer_name = f"relay-{uuid4()}"
    subprocess.run(["tmux", "set-buffer", "-b", buffer_name, text], check=True)
    subprocess.run(["tmux", "paste-buffer", "-d", "-p", "-b", buffer_name, "-t", target], check=True)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)
