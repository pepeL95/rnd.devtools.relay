from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .domain import PresenceStatus


class RegisterParticipantCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdatePresenceCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    presence: PresenceStatus


class CreateChannelCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class JoinChannelCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)


class CreateThreadCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    created_by_agent_id: str = Field(min_length=1)
    subject: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SendMessageCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    sender_agent_id: str = Field(min_length=1)
    recipient_agent_id: str = Field(min_length=1)
    recipient_node: str = Field(min_length=1)
    payload: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AckMessageCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)


class RegisterPeerCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
