from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import sqlite3

from rnd_devtools_relay.api import create_app


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "relay.db", node_id="node-a")
    return TestClient(app)


def bootstrap_thread(client: TestClient) -> None:
    client.post("/participants", json={"agent_id": "agent-a", "metadata": {}})
    client.post("/participants", json={"agent_id": "agent-b", "metadata": {}})
    client.post("/channels", json={"channel_id": "ops", "name": "Operations", "metadata": {}})
    client.post(
        "/threads",
        json={
            "thread_id": "thread-1",
            "channel_id": "ops",
            "created_by_agent_id": "agent-a",
            "subject": "Investigate",
            "metadata": {},
        },
    )


def test_send_message_is_durable_and_replayable(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    bootstrap_thread(client)

    response = client.post(
        "/messages",
        json={
            "envelope_id": "env-1",
            "channel_id": "ops",
            "thread_id": "thread-1",
            "sender_agent_id": "agent-a",
            "recipient_agent_id": "agent-b",
            "recipient_node": "node-a",
            "payload": "please inspect the logs",
            "metadata": {},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["delivery_status"] == "pending"
    assert payload["delivered_at"] is None

    history = client.get("/threads/thread-1/messages")
    assert history.status_code == 200
    messages = history.json()
    assert len(messages) == 1
    assert messages[0]["payload"] == "please inspect the logs"


def test_ack_marks_message_and_records_event(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    bootstrap_thread(client)
    client.post(
        "/messages",
        json={
            "envelope_id": "env-1",
            "channel_id": "ops",
            "thread_id": "thread-1",
            "sender_agent_id": "agent-a",
            "recipient_agent_id": "agent-b",
            "recipient_node": "node-a",
            "payload": "please inspect the logs",
            "metadata": {},
        },
    )
    client.post("/messages/env-1/delivered")

    ack = client.post("/messages/ack", json={"envelope_id": "env-1", "agent_id": "agent-b"})
    assert ack.status_code == 200
    assert ack.json()["acked_at"] is not None

    events = client.get("/events", params={"thread_id": "thread-1"}).json()
    assert any(event["kind"] == "message_acked" for event in events)


def test_websocket_stream_receives_live_envelopes_and_events(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    bootstrap_thread(client)

    with client.websocket_connect("/ws/events") as websocket:
        client.post(
            "/messages",
            json={
                "envelope_id": "env-1",
                "channel_id": "ops",
                "thread_id": "thread-1",
                "sender_agent_id": "agent-a",
                "recipient_agent_id": "agent-b",
                "recipient_node": "node-a",
                "payload": "live stream this",
                "metadata": {},
            },
        )

        seen_types = set()
        for _ in range(6):
            message = websocket.receive_json()
            seen_types.add(message["type"])
            if {"event", "envelope"} <= seen_types:
                break

    assert "event" in seen_types
    assert "envelope" in seen_types


def test_pending_messages_and_delivery_transitions(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    bootstrap_thread(client)
    client.post(
        "/messages",
        json={
            "envelope_id": "env-1",
            "channel_id": "ops",
            "thread_id": "thread-1",
            "sender_agent_id": "agent-a",
            "recipient_agent_id": "agent-b",
            "recipient_node": "node-a",
            "payload": "deliver this",
            "metadata": {},
        },
    )

    pending = client.get("/messages/pending", params={"recipient_agent_id": "agent-b", "channel_id": "ops"}).json()
    assert len(pending) == 1
    assert pending[0]["delivery_status"] == "pending"

    delivered = client.post("/messages/env-1/delivered")
    assert delivered.status_code == 200
    assert delivered.json()["delivery_status"] == "delivered"

    failed = client.post("/messages/env-1/delivery-failed", json={"error": "ignored"})
    assert failed.status_code == 200
    assert failed.json()["delivery_status"] == "failed"


def test_ui_exposes_channels_messages_and_observability_tabs(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/ui")
    assert response.status_code == 200
    assert "Channels" in response.text
    assert "Messages" in response.text
    assert "Observability" in response.text
    assert "Filter channels" in response.text


def test_get_thread_returns_metadata(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    bootstrap_thread(client)
    response = client.get("/threads/thread-1")
    assert response.status_code == 200
    assert response.json()["thread_id"] == "thread-1"


def test_open_request_lookup_and_response_closes_turn(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    bootstrap_thread(client)
    request = client.post(
        "/messages",
        json={
            "envelope_id": "env-1",
            "channel_id": "ops",
            "thread_id": "thread-1",
            "sender_agent_id": "agent-a",
            "recipient_agent_id": "agent-b",
            "recipient_node": "node-a",
            "payload": "please inspect the logs",
            "metadata": {"kind": "request", "expects_response": True},
        },
    )
    assert request.status_code == 200

    open_request = client.get("/threads/thread-1/open-request", params={"agent_id": "agent-b"})
    assert open_request.status_code == 200
    assert open_request.json()["envelope_id"] == "env-1"

    response = client.post(
        "/messages",
        json={
            "envelope_id": "env-2",
            "channel_id": "ops",
            "thread_id": "thread-1",
            "sender_agent_id": "agent-b",
            "recipient_agent_id": "agent-a",
            "recipient_node": "node-a",
            "payload": "done",
            "metadata": {
                "kind": "response",
                "expects_response": False,
                "reply_to_envelope_id": "env-1",
            },
        },
    )
    assert response.status_code == 200

    closed = client.get("/threads/thread-1/open-request", params={"agent_id": "agent-b"})
    assert closed.status_code == 404

    second_response = client.post(
        "/messages",
        json={
            "envelope_id": "env-3",
            "channel_id": "ops",
            "thread_id": "thread-1",
            "sender_agent_id": "agent-b",
            "recipient_agent_id": "agent-a",
            "recipient_node": "node-a",
            "payload": "one more thing",
            "metadata": {
                "kind": "response",
                "expects_response": False,
                "reply_to_envelope_id": "env-1",
            },
        },
    )
    assert second_response.status_code == 400
    assert "already has a response" in second_response.json()["detail"]


def test_legacy_runtime_schema_is_migrated_on_startup(tmp_path: Path) -> None:
    db_path = tmp_path / "relay.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        create table participants (
            agent_id text not null,
            runtime_id text not null,
            home_node text not null,
            presence text not null,
            metadata_json text not null,
            primary key (agent_id, runtime_id)
        );

        create table channel_memberships (
            channel_id text not null,
            agent_id text not null,
            runtime_id text not null,
            joined_at text not null,
            primary key (channel_id, agent_id, runtime_id)
        );

        create table envelopes (
            envelope_id text primary key,
            channel_id text not null,
            thread_id text not null,
            sender_agent_id text not null,
            sender_runtime_id text not null,
            recipient_agent_id text not null,
            recipient_runtime_id text not null,
            sender_node text not null,
            recipient_node text not null,
            payload text not null,
            created_at text not null,
            delivered_at text,
            acked_at text,
            metadata_json text not null
        );
        """
    )
    conn.execute(
        "insert into participants (agent_id, runtime_id, home_node, presence, metadata_json) values (?, ?, ?, ?, ?)",
        ("worker", "worker-01", "local", "online", "{}"),
    )
    conn.commit()
    conn.close()

    client = TestClient(create_app(db_path=db_path, node_id="local"))
    response = client.post("/participants", json={"agent_id": "coordinator", "metadata": {}})
    assert response.status_code == 200

    participants = client.get("/participants").json()
    assert {item["agent_id"] for item in participants} == {"worker", "coordinator"}
