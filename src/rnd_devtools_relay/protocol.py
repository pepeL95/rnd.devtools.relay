from __future__ import annotations

import hashlib
import re
from typing import Any

import typer


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "default"


def session_bridge_thread_id(session_id: str, channel_id: str, agent_a: str, agent_b: str) -> str:
    pair = sorted([agent_a, agent_b])
    digest = hashlib.sha1(f"{session_id}:{channel_id}:{pair[0]}:{pair[1]}".encode()).hexdigest()[:10]
    return f"bridge-{slugify(session_id)}-{slugify(channel_id)}-{slugify(pair[0])}-{slugify(pair[1])}-{digest}"


def infer_thread_peer(thread: dict[str, Any], local_agent_id: str, channel_id: str, session_id: str) -> str:
    if thread.get("channel_id") != channel_id:
        raise typer.BadParameter(f"thread `{thread['thread_id']}` does not belong to active channel `{channel_id}`.")
    if (thread.get("metadata") or {}).get("session_id") != session_id:
        raise typer.BadParameter(f"thread `{thread['thread_id']}` does not belong to active session `{session_id}`.")
    participants = list((thread.get("metadata") or {}).get("participants") or [])
    if len(participants) != 2:
        raise typer.BadParameter(f"thread `{thread['thread_id']}` is not a direct a2a bridge.")
    if local_agent_id not in participants:
        raise typer.BadParameter(f"agent `{local_agent_id}` is not a participant in thread `{thread['thread_id']}`.")
    return participants[0] if participants[1] == local_agent_id else participants[1]
