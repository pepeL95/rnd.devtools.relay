from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PresenceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    IDLE = "idle"


class EventKind(str, Enum):
    PARTICIPANT_REGISTERED = "participant_registered"
    PRESENCE_UPDATED = "presence_updated"
    CHANNEL_CREATED = "channel_created"
    CHANNEL_JOINED = "channel_joined"
    THREAD_CREATED = "thread_created"
    MESSAGE_SENT = "message_sent"
    MESSAGE_DELIVERED = "message_delivered"
    MESSAGE_ACKED = "message_acked"
    PEER_REGISTERED = "peer_registered"


class ParticipantIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    home_node: str = Field(min_length=1)
    presence: PresenceStatus = PresenceStatus.ONLINE
    metadata: dict[str, Any] = Field(default_factory=dict)


class Channel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class Thread(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    subject: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    sender_agent_id: str = Field(min_length=1)
    recipient_agent_id: str = Field(min_length=1)
    sender_node: str = Field(min_length=1)
    recipient_node: str = Field(min_length=1)
    payload: str
    created_at: datetime = Field(default_factory=utc_now)
    delivered_at: datetime | None = None
    acked_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    kind: EventKind
    channel_id: str | None = None
    thread_id: str | None = None
    envelope_id: str | None = None
    actor: str | None = None
    node_id: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    details: dict[str, Any] = Field(default_factory=dict)


class RegisterParticipantRequest(BaseModel):
    agent_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class PresenceUpdateRequest(BaseModel):
    presence: PresenceStatus


class CreateChannelRequest(BaseModel):
    channel_id: str
    name: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class JoinChannelRequest(BaseModel):
    agent_id: str


class CreateThreadRequest(BaseModel):
    thread_id: str
    channel_id: str
    created_by_agent_id: str
    subject: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SendMessageRequest(BaseModel):
    envelope_id: str
    channel_id: str
    thread_id: str
    sender_agent_id: str
    recipient_agent_id: str
    recipient_node: str
    payload: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AckMessageRequest(BaseModel):
    envelope_id: str
    agent_id: str


class RegisterPeerRequest(BaseModel):
    node_id: str
    base_url: str
