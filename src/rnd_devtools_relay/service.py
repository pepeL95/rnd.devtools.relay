from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    AckMessageRequest,
    Channel,
    CreateChannelRequest,
    CreateThreadRequest,
    Envelope,
    Event,
    EventKind,
    JoinChannelRequest,
    ParticipantIdentity,
    PresenceStatus,
    RegisterParticipantRequest,
    SendMessageRequest,
    Thread,
    utc_now,
)

RemoteDispatcher = Callable[[Envelope], Awaitable[None]]


class RelayService:
    def __init__(self, db_path: str | Path, node_id: str, remote_dispatcher: RemoteDispatcher | None = None):
        self.db_path = str(db_path)
        self.node_id = node_id
        self.remote_dispatcher = remote_dispatcher
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._init_db()

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._migrate_legacy_schema(conn)
            conn.executescript(
                """
                create table if not exists participants (
                    agent_id text not null,
                    home_node text not null,
                    presence text not null,
                    metadata_json text not null,
                    primary key (agent_id)
                );

                create table if not exists channels (
                    channel_id text primary key,
                    name text not null,
                    metadata_json text not null,
                    created_at text not null
                );

                create table if not exists channel_memberships (
                    channel_id text not null,
                    agent_id text not null,
                    joined_at text not null,
                    primary key (channel_id, agent_id)
                );

                create table if not exists threads (
                    thread_id text primary key,
                    channel_id text not null,
                    created_by text not null,
                    subject text,
                    metadata_json text not null,
                    created_at text not null
                );

                create table if not exists envelopes (
                    envelope_id text primary key,
                    channel_id text not null,
                    thread_id text not null,
                    sender_agent_id text not null,
                    recipient_agent_id text not null,
                    sender_node text not null,
                    recipient_node text not null,
                    payload text not null,
                    created_at text not null,
                    delivered_at text,
                    acked_at text,
                    metadata_json text not null
                );

                create table if not exists events (
                    event_id text primary key,
                    kind text not null,
                    channel_id text,
                    thread_id text,
                    envelope_id text,
                    actor text,
                    node_id text not null,
                    created_at text not null,
                    details_json text not null
                );

                create table if not exists peers (
                    node_id text primary key,
                    base_url text not null
                );
                """
            )

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "select name from sqlite_master where type = 'table' and name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        if not self._table_exists(conn, table_name):
            return set()
        rows = conn.execute(f"pragma table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    def _migrate_legacy_schema(self, conn: sqlite3.Connection) -> None:
        if "runtime_id" in self._table_columns(conn, "participants"):
            conn.executescript(
                """
                alter table participants rename to participants_legacy;
                create table participants (
                    agent_id text not null,
                    home_node text not null,
                    presence text not null,
                    metadata_json text not null,
                    primary key (agent_id)
                );
                insert or replace into participants (agent_id, home_node, presence, metadata_json)
                select agent_id, home_node, presence, metadata_json
                from participants_legacy;
                drop table participants_legacy;
                """
            )

        if "runtime_id" in self._table_columns(conn, "channel_memberships"):
            conn.executescript(
                """
                alter table channel_memberships rename to channel_memberships_legacy;
                create table channel_memberships (
                    channel_id text not null,
                    agent_id text not null,
                    joined_at text not null,
                    primary key (channel_id, agent_id)
                );
                insert or replace into channel_memberships (channel_id, agent_id, joined_at)
                select channel_id, agent_id, min(joined_at)
                from channel_memberships_legacy
                group by channel_id, agent_id;
                drop table channel_memberships_legacy;
                """
            )

        envelope_columns = self._table_columns(conn, "envelopes")
        if "sender_runtime_id" in envelope_columns or "recipient_runtime_id" in envelope_columns:
            conn.executescript(
                """
                alter table envelopes rename to envelopes_legacy;
                create table envelopes (
                    envelope_id text primary key,
                    channel_id text not null,
                    thread_id text not null,
                    sender_agent_id text not null,
                    recipient_agent_id text not null,
                    sender_node text not null,
                    recipient_node text not null,
                    payload text not null,
                    created_at text not null,
                    delivered_at text,
                    acked_at text,
                    metadata_json text not null
                );
                insert or replace into envelopes (
                    envelope_id, channel_id, thread_id, sender_agent_id, recipient_agent_id,
                    sender_node, recipient_node, payload, created_at, delivered_at, acked_at, metadata_json
                )
                select
                    envelope_id, channel_id, thread_id, sender_agent_id, recipient_agent_id,
                    sender_node, recipient_node, payload, created_at, delivered_at, acked_at, metadata_json
                from envelopes_legacy;
                drop table envelopes_legacy;
                """
            )

    async def _publish(self, payload: dict[str, Any]) -> None:
        stale: list[asyncio.Queue[dict[str, Any]]] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                stale.append(queue)
        for queue in stale:
            self._subscribers.discard(queue)

    async def _record_event(
        self,
        kind: EventKind,
        *,
        channel_id: str | None = None,
        thread_id: str | None = None,
        envelope_id: str | None = None,
        actor: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Event:
        event = Event(
            event_id=str(uuid4()),
            kind=kind,
            channel_id=channel_id,
            thread_id=thread_id,
            envelope_id=envelope_id,
            actor=actor,
            node_id=self.node_id,
            details=details or {},
        )
        with self._connect() as conn:
            conn.execute(
                """
                insert into events (event_id, kind, channel_id, thread_id, envelope_id, actor, node_id, created_at, details_json)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.kind.value,
                    event.channel_id,
                    event.thread_id,
                    event.envelope_id,
                    event.actor,
                    event.node_id,
                    event.created_at.isoformat(),
                    json.dumps(event.details),
                ),
            )
        await self._publish({"type": "event", "data": event.model_dump(mode="json")})
        return event

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    async def register_participant(self, request: RegisterParticipantRequest) -> ParticipantIdentity:
        participant = ParticipantIdentity(
            agent_id=request.agent_id,
            home_node=self.node_id,
            metadata=request.metadata,
            presence=PresenceStatus.ONLINE,
        )
        with self._connect() as conn:
            conn.execute(
                """
                insert into participants (agent_id, home_node, presence, metadata_json)
                values (?, ?, ?, ?)
                on conflict(agent_id) do update set
                    home_node=excluded.home_node,
                    presence=excluded.presence,
                    metadata_json=excluded.metadata_json
                """,
                (
                    participant.agent_id,
                    participant.home_node,
                    participant.presence.value,
                    json.dumps(participant.metadata),
                ),
            )
        await self._record_event(
            EventKind.PARTICIPANT_REGISTERED,
            actor=participant.agent_id,
            details={"metadata": participant.metadata},
        )
        return participant

    async def update_presence(self, agent_id: str, presence: PresenceStatus) -> ParticipantIdentity:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update participants
                set presence = ?
                where agent_id = ?
                returning *
                """,
                (presence.value, agent_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError("participant not found")
        participant = self._participant_from_row(row)
        await self._record_event(
            EventKind.PRESENCE_UPDATED,
            actor=agent_id,
            details={"presence": presence.value},
        )
        return participant

    def list_participants(self) -> list[ParticipantIdentity]:
        with self._connect() as conn:
            rows = conn.execute("select * from participants order by agent_id").fetchall()
        return [self._participant_from_row(row) for row in rows]

    def list_channel_participants(self, channel_id: str) -> list[ParticipantIdentity]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select p.*
                from channel_memberships cm
                join participants p
                  on p.agent_id = cm.agent_id
                where cm.channel_id = ?
                order by p.agent_id
                """,
                (channel_id,),
            ).fetchall()
        return [self._participant_from_row(row) for row in rows]

    async def create_channel(self, request: CreateChannelRequest) -> Channel:
        channel = Channel(channel_id=request.channel_id, name=request.name, metadata=request.metadata)
        with self._connect() as conn:
            conn.execute(
                """
                insert into channels (channel_id, name, metadata_json, created_at)
                values (?, ?, ?, ?)
                """,
                (channel.channel_id, channel.name, json.dumps(channel.metadata), channel.created_at.isoformat()),
            )
        await self._record_event(
            EventKind.CHANNEL_CREATED,
            channel_id=channel.channel_id,
            details={"name": channel.name},
        )
        return channel

    def list_channels(self) -> list[Channel]:
        with self._connect() as conn:
            rows = conn.execute("select * from channels order by created_at desc").fetchall()
        return [self._channel_from_row(row) for row in rows]

    async def join_channel(self, channel_id: str, request: JoinChannelRequest) -> None:
        joined_at = utc_now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                insert into channel_memberships (channel_id, agent_id, joined_at)
                values (?, ?, ?)
                on conflict(channel_id, agent_id) do nothing
                """,
                (channel_id, request.agent_id, joined_at),
            )
        await self._record_event(
            EventKind.CHANNEL_JOINED,
            channel_id=channel_id,
            actor=request.agent_id,
        )

    async def create_thread(self, request: CreateThreadRequest) -> Thread:
        thread = Thread(
            thread_id=request.thread_id,
            channel_id=request.channel_id,
            created_by=request.created_by_agent_id,
            subject=request.subject,
            metadata=request.metadata,
        )
        with self._connect() as conn:
            conn.execute(
                """
                insert into threads (thread_id, channel_id, created_by, subject, metadata_json, created_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    thread.thread_id,
                    thread.channel_id,
                    thread.created_by,
                    thread.subject,
                    json.dumps(thread.metadata),
                    thread.created_at.isoformat(),
                ),
            )
        await self._record_event(
            EventKind.THREAD_CREATED,
            channel_id=thread.channel_id,
            thread_id=thread.thread_id,
            actor=thread.created_by,
            details={"subject": thread.subject},
        )
        return thread

    def list_threads(self, channel_id: str | None = None) -> list[Thread]:
        query = "select * from threads"
        params: tuple[Any, ...] = ()
        if channel_id:
            query += " where channel_id = ?"
            params = (channel_id,)
        query += " order by created_at desc"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._thread_from_row(row) for row in rows]

    async def send_message(self, request: SendMessageRequest) -> Envelope:
        envelope = Envelope(
            envelope_id=request.envelope_id,
            channel_id=request.channel_id,
            thread_id=request.thread_id,
            sender_agent_id=request.sender_agent_id,
            recipient_agent_id=request.recipient_agent_id,
            sender_node=self.node_id,
            recipient_node=request.recipient_node,
            payload=request.payload,
            metadata=request.metadata,
        )
        with self._connect() as conn:
            conn.execute(
                """
                insert into envelopes (
                    envelope_id, channel_id, thread_id, sender_agent_id,
                    recipient_agent_id, sender_node, recipient_node,
                    payload, created_at, delivered_at, acked_at, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.envelope_id,
                    envelope.channel_id,
                    envelope.thread_id,
                    envelope.sender_agent_id,
                    envelope.recipient_agent_id,
                    envelope.sender_node,
                    envelope.recipient_node,
                    envelope.payload,
                    envelope.created_at.isoformat(),
                    envelope.delivered_at,
                    envelope.acked_at,
                    json.dumps(envelope.metadata),
                ),
            )
        await self._record_event(
            EventKind.MESSAGE_SENT,
            channel_id=envelope.channel_id,
            thread_id=envelope.thread_id,
            envelope_id=envelope.envelope_id,
            actor=envelope.sender_agent_id,
            details={"recipient": envelope.recipient_agent_id},
        )
        await self._publish({"type": "envelope", "data": envelope.model_dump(mode="json")})
        if envelope.recipient_node == self.node_id:
            return await self.mark_delivered(envelope.envelope_id)
        if self.remote_dispatcher:
            await self.remote_dispatcher(envelope)
        return envelope

    async def receive_federated_envelope(self, envelope: Envelope) -> Envelope:
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into envelopes (
                    envelope_id, channel_id, thread_id, sender_agent_id,
                    recipient_agent_id, sender_node, recipient_node,
                    payload, created_at, delivered_at, acked_at, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.envelope_id,
                    envelope.channel_id,
                    envelope.thread_id,
                    envelope.sender_agent_id,
                    envelope.recipient_agent_id,
                    envelope.sender_node,
                    envelope.recipient_node,
                    envelope.payload,
                    envelope.created_at.isoformat(),
                    envelope.delivered_at.isoformat() if envelope.delivered_at else None,
                    envelope.acked_at.isoformat() if envelope.acked_at else None,
                    json.dumps(envelope.metadata),
                ),
            )
        await self._publish({"type": "envelope", "data": envelope.model_dump(mode="json")})
        return await self.mark_delivered(envelope.envelope_id)

    async def mark_delivered(self, envelope_id: str) -> Envelope:
        delivered_at = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update envelopes
                set delivered_at = coalesce(delivered_at, ?)
                where envelope_id = ?
                returning *
                """,
                (delivered_at.isoformat(), envelope_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError("envelope not found")
        envelope = self._envelope_from_row(row)
        await self._record_event(
            EventKind.MESSAGE_DELIVERED,
            channel_id=envelope.channel_id,
            thread_id=envelope.thread_id,
            envelope_id=envelope.envelope_id,
            actor=envelope.recipient_agent_id,
        )
        await self._publish({"type": "envelope", "data": envelope.model_dump(mode="json")})
        return envelope

    async def ack_message(self, request: AckMessageRequest) -> Envelope:
        acked_at = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update envelopes
                set acked_at = ?
                where envelope_id = ?
                  and recipient_agent_id = ?
                returning *
                """,
                (acked_at.isoformat(), request.envelope_id, request.agent_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError("envelope not found")
        envelope = self._envelope_from_row(row)
        await self._record_event(
            EventKind.MESSAGE_ACKED,
            channel_id=envelope.channel_id,
            thread_id=envelope.thread_id,
            envelope_id=envelope.envelope_id,
            actor=request.agent_id,
        )
        await self._publish({"type": "envelope", "data": envelope.model_dump(mode="json")})
        return envelope

    def list_thread_messages(self, thread_id: str) -> list[Envelope]:
        with self._connect() as conn:
            rows = conn.execute(
                "select * from envelopes where thread_id = ? order by created_at asc", (thread_id,)
            ).fetchall()
        return [self._envelope_from_row(row) for row in rows]

    def list_events(self, *, channel_id: str | None = None, thread_id: str | None = None, limit: int = 200) -> list[Event]:
        query = "select * from events where 1=1"
        params: list[Any] = []
        if channel_id:
            query += " and channel_id = ?"
            params.append(channel_id)
        if thread_id:
            query += " and thread_id = ?"
            params.append(thread_id)
        query += " order by created_at desc limit ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._event_from_row(row) for row in rows]

    async def register_peer(self, node_id: str, base_url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into peers (node_id, base_url) values (?, ?) on conflict(node_id) do update set base_url=excluded.base_url",
                (node_id, base_url.rstrip("/")),
            )
        await self._record_event(EventKind.PEER_REGISTERED, details={"node_id": node_id, "base_url": base_url.rstrip("/")})

    def get_peer(self, node_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("select base_url from peers where node_id = ?", (node_id,)).fetchone()
        return None if row is None else str(row["base_url"])

    def _participant_from_row(self, row: sqlite3.Row) -> ParticipantIdentity:
        return ParticipantIdentity(
            agent_id=row["agent_id"],
            home_node=row["home_node"],
            presence=PresenceStatus(row["presence"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def _channel_from_row(self, row: sqlite3.Row) -> Channel:
        return Channel(
            channel_id=row["channel_id"],
            name=row["name"],
            metadata=json.loads(row["metadata_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _thread_from_row(self, row: sqlite3.Row) -> Thread:
        return Thread(
            thread_id=row["thread_id"],
            channel_id=row["channel_id"],
            created_by=row["created_by"],
            subject=row["subject"],
            metadata=json.loads(row["metadata_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _envelope_from_row(self, row: sqlite3.Row) -> Envelope:
        return Envelope(
            envelope_id=row["envelope_id"],
            channel_id=row["channel_id"],
            thread_id=row["thread_id"],
            sender_agent_id=row["sender_agent_id"],
            recipient_agent_id=row["recipient_agent_id"],
            sender_node=row["sender_node"],
            recipient_node=row["recipient_node"],
            payload=row["payload"],
            created_at=datetime.fromisoformat(row["created_at"]),
            delivered_at=datetime.fromisoformat(row["delivered_at"]) if row["delivered_at"] else None,
            acked_at=datetime.fromisoformat(row["acked_at"]) if row["acked_at"] else None,
            metadata=json.loads(row["metadata_json"]),
        )

    def _event_from_row(self, row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            kind=EventKind(row["kind"]),
            channel_id=row["channel_id"],
            thread_id=row["thread_id"],
            envelope_id=row["envelope_id"],
            actor=row["actor"],
            node_id=row["node_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            details=json.loads(row["details_json"]),
        )
