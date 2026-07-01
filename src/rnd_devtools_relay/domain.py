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
    MESSAGE_DELIVERY_FAILED = "message_delivery_failed"
    MESSAGE_DELIVERED = "message_delivered"
    MESSAGE_ACKED = "message_acked"
    PEER_REGISTERED = "peer_registered"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


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
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING
    delivery_error: str | None = None
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
