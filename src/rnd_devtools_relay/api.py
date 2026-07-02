from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response

from .commands import (
    AckMessageCommand,
    CreateChannelCommand,
    CreateThreadCommand,
    JoinChannelCommand,
    RegisterParticipantCommand,
    RegisterPeerCommand,
    SendMessageCommand,
    UpdatePresenceCommand,
)
from .domain import Envelope
from .service import RelayService, RelayValidationError


def create_app(*, db_path: str | Path = "var/relay.db", node_id: str = "local") -> FastAPI:
    app = FastAPI(title="rnd.devtools.relay", version="0.1.0")
    relay: RelayService

    async def remote_dispatch(envelope: Envelope) -> None:
        peer_url = relay.get_peer(envelope.recipient_node)
        if not peer_url:
            raise HTTPException(status_code=404, detail=f"unknown peer node: {envelope.recipient_node}")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{peer_url}/federation/envelopes", json=envelope.model_dump(mode="json"))
            response.raise_for_status()

    relay = RelayService(db_path=db_path, node_id=node_id, remote_dispatcher=remote_dispatch)
    app.state.relay = relay

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "node_id": node_id}

    @app.post("/participants")
    async def register_participant(command: RegisterParticipantCommand):
        return await relay.register_participant(command)

    @app.get("/participants")
    async def list_participants():
        return relay.list_participants()

    @app.post("/participants/{agent_id}/presence")
    async def update_presence(agent_id: str, command: UpdatePresenceCommand):
        try:
            return await relay.update_presence(agent_id, command.presence)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/channels")
    async def create_channel(command: CreateChannelCommand):
        return await relay.create_channel(command)

    @app.get("/channels")
    async def list_channels():
        return relay.list_channels()

    @app.post("/channels/{channel_id}/join")
    async def join_channel(channel_id: str, command: JoinChannelCommand):
        await relay.join_channel(channel_id, command)
        return {"ok": True}

    @app.get("/channels/{channel_id}/participants")
    async def list_channel_participants(channel_id: str):
        return relay.list_channel_participants(channel_id)

    @app.post("/threads")
    async def create_thread(command: CreateThreadCommand):
        return await relay.create_thread(command)

    @app.get("/threads")
    async def list_threads(channel_id: str | None = None):
        return relay.list_threads(channel_id=channel_id)

    @app.get("/threads/{thread_id}")
    async def get_thread(thread_id: str):
        thread = relay.get_thread(thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="thread not found")
        return thread

    @app.get("/threads/{thread_id}/messages")
    async def list_thread_messages(thread_id: str):
        return relay.list_thread_messages(thread_id)

    @app.get("/threads/{thread_id}/open-request")
    async def get_open_request(thread_id: str, agent_id: str):
        envelope = relay.get_open_request(thread_id, agent_id)
        if envelope is None:
            raise HTTPException(status_code=404, detail="open request not found")
        return envelope

    @app.get("/messages/pending")
    async def list_pending_messages(
        recipient_agent_id: str | None = None,
        channel_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        return relay.list_pending_messages(recipient_agent_id=recipient_agent_id, channel_id=channel_id, limit=limit)

    @app.post("/messages")
    async def send_message(command: SendMessageCommand):
        try:
            return await relay.send_message(command)
        except RelayValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/messages/{envelope_id}/delivered")
    async def mark_delivered(envelope_id: str):
        try:
            return await relay.mark_delivered(envelope_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/messages/{envelope_id}/delivery-failed")
    async def mark_delivery_failed(envelope_id: str, error: str = Body(embed=True)):
        try:
            return await relay.mark_delivery_failed(envelope_id, error)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/messages/ack")
    async def ack_message(command: AckMessageCommand):
        try:
            return await relay.ack_message(command)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/events")
    async def list_events(
        channel_id: str | None = None,
        thread_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        return relay.list_events(channel_id=channel_id, thread_id=thread_id, limit=limit)

    @app.post("/peers")
    async def register_peer(command: RegisterPeerCommand):
        await relay.register_peer(command.node_id, command.base_url)
        return {"ok": True}

    @app.post("/federation/envelopes")
    async def receive_federated_envelope(envelope: Envelope):
        return await relay.receive_federated_envelope(envelope)

    @app.get("/ui", response_class=HTMLResponse)
    async def ui():
        return HTMLResponse(_UI_HTML)

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket):
        await websocket.accept()
        queue = relay.subscribe()
        try:
            # Seed the stream with recent events so the UI has immediate context.
            for event in reversed(relay.list_events(limit=50)):
                await websocket.send_json({"type": "event", "data": event.model_dump(mode="json")})
            while True:
                payload = await queue.get()
                await websocket.send_json(payload)
        except WebSocketDisconnect:
            pass
        finally:
            relay.unsubscribe(queue)

    return app


_UI_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Relay Console</title>
    <style>
      :root {
        --bg: #f5f1e8;
        --panel: #fffaf2;
        --panel-strong: #f3e7d4;
        --text: #1a1a1a;
        --muted: #6a645b;
        --line: #d5c7af;
        --accent: #b24c2d;
        --accent-soft: #f1d6be;
        --accent-deep: #8a351b;
        --ink-soft: #2f2a24;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "IBM Plex Sans", "Avenir Next", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, #f8dfc7 0, transparent 35%),
          linear-gradient(135deg, #f5f1e8 0%, #efe6d6 100%);
      }
      header {
        padding: 22px 28px 18px;
        border-bottom: 1px solid var(--line);
        position: sticky;
        top: 0;
        z-index: 10;
        backdrop-filter: blur(12px);
        background: rgba(245, 241, 232, 0.88);
      }
      .topbar {
        display: flex;
        align-items: end;
        justify-content: space-between;
        gap: 20px;
        flex-wrap: wrap;
      }
      .title h1 {
        margin: 0;
        font-size: 30px;
      }
      .title p {
        margin: 8px 0 0;
        color: var(--muted);
      }
      .filters {
        display: flex;
        gap: 10px;
        align-items: center;
        flex-wrap: wrap;
      }
      main {
        display: grid;
        grid-template-columns: 320px 1fr;
        gap: 20px;
        padding: 20px;
        min-height: calc(100vh - 108px);
      }
      section {
        background: color-mix(in oklab, var(--panel) 92%, white 8%);
        border: 1px solid var(--line);
        border-radius: 18px;
        padding: 16px;
        box-shadow: 0 18px 50px rgba(80, 50, 10, 0.08);
      }
      h2 {
        margin-top: 0;
        font-size: 14px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: var(--muted);
      }
      input, select {
        min-width: 150px;
        padding: 10px 12px;
        border-radius: 10px;
        border: 1px solid var(--line);
        background: white;
      }
      .workspace {
        display: grid;
        grid-template-rows: auto 1fr;
        gap: 14px;
      }
      .tabs {
        display: inline-flex;
        gap: 8px;
        padding: 6px;
        border-radius: 14px;
        background: rgba(255,255,255,0.5);
        border: 1px solid var(--line);
      }
      .tab {
        border: 0;
        border-radius: 10px;
        background: transparent;
        color: var(--muted);
        padding: 10px 14px;
        font: inherit;
        cursor: pointer;
      }
      .tab.active {
        background: var(--accent);
        color: white;
      }
      .panel {
        display: none;
        min-height: 0;
      }
      .panel.active {
        display: grid;
      }
      .channels {
        display: grid;
        gap: 10px;
        align-content: start;
        overflow: auto;
        max-height: calc(100vh - 180px);
      }
      .channel-card {
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 14px;
        background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(248,241,231,0.96));
        cursor: pointer;
        text-align: left;
        font: inherit;
        color: inherit;
      }
      .channel-card.active {
        border-color: var(--accent);
        background: linear-gradient(180deg, rgba(242,214,190,0.95), rgba(255,247,240,0.98));
      }
      .channel-title {
        font-weight: 600;
      }
      .channel-subtitle {
        margin-top: 4px;
        color: var(--muted);
        font-size: 13px;
      }
      .chat-layout {
        display: grid;
        grid-template-rows: auto 1fr;
        min-height: 0;
      }
      .chat-header {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        align-items: end;
        padding-bottom: 14px;
        border-bottom: 1px solid var(--line);
      }
      .chat-header h2 {
        margin-bottom: 6px;
      }
      .chat-meta {
        color: var(--muted);
        font-size: 13px;
      }
      .message-stream,
      .event-stream {
        display: grid;
        gap: 12px;
        overflow: auto;
        padding-top: 14px;
        max-height: calc(100vh - 280px);
      }
      .message-stream {
        align-content: start;
        padding-right: 6px;
      }
      .chat-day {
        justify-self: center;
        padding: 6px 12px;
        border-radius: 999px;
        background: rgba(255,255,255,0.72);
        border: 1px solid var(--line);
        color: var(--muted);
        font-size: 12px;
        letter-spacing: 0.04em;
        text-transform: uppercase;
      }
      .message-card {
        width: 100%;
        border: 1px solid var(--line);
        border-radius: 20px;
        padding: 12px 14px 12px 12px;
        background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(252,247,240,0.96));
        box-shadow: 0 10px 24px rgba(110, 72, 24, 0.08);
      }
      .message-card.grouped {
        margin-top: -4px;
      }
      .message-shell {
        display: grid;
        grid-template-columns: 42px 1fr;
        gap: 12px;
        align-items: start;
      }
      .avatar {
        width: 42px;
        height: 42px;
        border-radius: 14px;
        display: grid;
        place-items: center;
        background: linear-gradient(135deg, var(--accent) 0%, #dc7c46 100%);
        color: white;
        font-size: 13px;
        font-weight: 700;
        letter-spacing: 0.04em;
        box-shadow: inset 0 0 0 1px rgba(255,255,255,0.24);
      }
      .message-shell.grouped .avatar {
        visibility: hidden;
      }
      .message-stack {
        display: grid;
        gap: 6px;
      }
      .message-head {
        display: flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
      }
      .message-author {
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--accent-deep);
        font-weight: 700;
      }
      .message-route {
        display: inline-block;
        margin-right: 8px;
        font-size: 13px;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 999px;
        background: rgba(178, 76, 45, 0.1);
        color: var(--accent-deep);
      }
      .message-thread {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 999px;
        background: var(--panel-strong);
        color: var(--muted);
        font-size: 11px;
      }
      .message-body {
        color: var(--ink-soft);
        font-size: 15px;
        line-height: 1.45;
        white-space: pre-wrap;
      }
      .message-meta {
        color: var(--muted);
        font-size: 12px;
      }
      .event-card {
        border: 1px solid var(--line);
        border-radius: 14px;
        padding: 12px;
        background: linear-gradient(180deg, rgba(255,255,255,0.95), rgba(252,247,240,0.95));
      }
      .tag {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 999px;
        background: var(--accent-soft);
        color: var(--accent);
        font-size: 12px;
        margin-right: 6px;
      }
      .meta {
        color: var(--muted);
        font-size: 12px;
      }
      .empty {
        display: grid;
        place-items: center;
        min-height: 240px;
        text-align: center;
        color: var(--muted);
        border: 1px dashed var(--line);
        border-radius: 18px;
        background: rgba(255,255,255,0.45);
      }
      @media (max-width: 880px) {
        main { grid-template-columns: 1fr; }
        .filters { width: 100%; }
        input { flex: 1 1 180px; }
        .message-shell { grid-template-columns: 36px 1fr; gap: 10px; }
        .avatar { width: 36px; height: 36px; border-radius: 12px; }
      }
    </style>
  </head>
  <body>
    <header>
      <div class="topbar">
        <div class="title">
          <h1>Relay Console</h1>
          <p>Sessions on the left, live conversation on the right, raw protocol observability in its own tab.</p>
        </div>
        <div class="filters">
          <input id="channel-filter" placeholder="Filter channels">
          <input id="thread-filter" placeholder="Filter thread ID">
          <input id="participant-filter" placeholder="Filter participant">
        </div>
      </div>
    </header>
    <main>
      <section>
        <h2>Channels</h2>
        <div id="channels" class="channels"></div>
      </section>
      <section class="workspace">
        <div class="tabs">
          <button id="tab-chat" class="tab active" type="button">Messages</button>
          <button id="tab-observability" class="tab" type="button">Observability</button>
        </div>
        <div id="panel-chat" class="panel active chat-layout">
          <div class="chat-header">
            <div>
              <h2 id="chat-title">Select a channel</h2>
              <div id="chat-meta" class="chat-meta">Choose a session on the left to load its thread history.</div>
            </div>
          </div>
          <div id="messages" class="message-stream"></div>
        </div>
        <div id="panel-observability" class="panel">
          <div id="events" class="event-stream"></div>
        </div>
      </section>
    </main>
    <script>
      const channelsEl = document.getElementById("channels");
      const messagesEl = document.getElementById("messages");
      const eventsEl = document.getElementById("events");
      const chatTitleEl = document.getElementById("chat-title");
      const chatMetaEl = document.getElementById("chat-meta");
      const filters = {
        channel: document.getElementById("channel-filter"),
        thread: document.getElementById("thread-filter"),
        participant: document.getElementById("participant-filter"),
      };
      const tabs = {
        chat: document.getElementById("tab-chat"),
        observability: document.getElementById("tab-observability"),
      };
      const panels = {
        chat: document.getElementById("panel-chat"),
        observability: document.getElementById("panel-observability"),
      };
      const state = {
        channels: [],
        threads: [],
        messages: [],
        events: [],
        activeChannelId: null,
      };

      function escapeHtml(value) {
        return String(value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
      }

      function setTab(name) {
        Object.entries(tabs).forEach(([key, button]) => {
          button.classList.toggle("active", key === name);
        });
        Object.entries(panels).forEach(([key, panel]) => {
          panel.classList.toggle("active", key === name);
        });
      }

      function activeThreads() {
        if (!state.activeChannelId) return [];
        return state.threads.filter((thread) => thread.channel_id === state.activeChannelId);
      }

      function visibleMessages() {
        const threadFilter = filters.thread.value.trim();
        const participantFilter = filters.participant.value.trim().toLowerCase();
        const threadIds = new Set(activeThreads().map((thread) => thread.thread_id));
        return state.messages.filter((message) => {
          if (!threadIds.has(message.thread_id)) return false;
          if (threadFilter && message.thread_id !== threadFilter) return false;
          if (participantFilter) {
            const haystack = JSON.stringify(message).toLowerCase();
            if (!haystack.includes(participantFilter)) return false;
          }
          return true;
        });
      }

      function visibleEvents() {
        const channelFilter = filters.channel.value.trim().toLowerCase();
        const threadFilter = filters.thread.value.trim();
        const participantFilter = filters.participant.value.trim().toLowerCase();
        return state.events.filter((event) => {
          if (state.activeChannelId && event.channel_id && event.channel_id !== state.activeChannelId) return false;
          if (channelFilter) {
            const haystack = `${event.channel_id || ""} ${event.kind || ""}`.toLowerCase();
            if (!haystack.includes(channelFilter)) return false;
          }
          if (threadFilter && event.thread_id !== threadFilter) return false;
          if (participantFilter) {
            const haystack = JSON.stringify(event).toLowerCase();
            if (!haystack.includes(participantFilter)) return false;
          }
          return true;
        });
      }

      function renderChannels() {
        const channelFilter = filters.channel.value.trim().toLowerCase();
        const channels = state.channels.filter((channel) => {
          if (!channelFilter) return true;
          const haystack = `${channel.channel_id} ${channel.name}`.toLowerCase();
          return haystack.includes(channelFilter);
        });

        if (!channels.length) {
          channelsEl.innerHTML = `<div class="empty">No channels match the current filter.</div>`;
          return;
        }

        channelsEl.innerHTML = "";
        channels.forEach((channel) => {
          const card = document.createElement("button");
          card.type = "button";
          card.className = "channel-card";
          card.classList.toggle("active", channel.channel_id === state.activeChannelId);
          const threadCount = state.threads.filter((thread) => thread.channel_id === channel.channel_id).length;
          card.innerHTML = `
            <div class="channel-title">${escapeHtml(channel.name)}</div>
            <div class="channel-subtitle">${escapeHtml(channel.channel_id)} · ${threadCount} threads</div>
          `;
          card.addEventListener("click", async () => {
            state.activeChannelId = channel.channel_id;
            await loadMessagesForActiveChannel();
            render();
          });
          channelsEl.appendChild(card);
        });
      }

      function renderMessages() {
        if (!state.activeChannelId) {
          chatTitleEl.textContent = "Select a channel";
          chatMetaEl.textContent = "Choose a session on the left to load its thread history.";
          messagesEl.innerHTML = `<div class="empty">Channels are your session list. Select one to view the conversation timeline.</div>`;
          return;
        }

        const threads = activeThreads();
        const messages = visibleMessages();
        chatTitleEl.textContent = state.channels.find((channel) => channel.channel_id === state.activeChannelId)?.name || state.activeChannelId;
        chatMetaEl.textContent = `${state.activeChannelId} · ${threads.length} threads · ${messages.length} messages`;

        if (!messages.length) {
          messagesEl.innerHTML = `<div class="empty">No messages match the active filters for this channel.</div>`;
          return;
        }

        messagesEl.innerHTML = "";
        let previousAuthor = null;
        let previousDay = null;
        messages
          .sort((a, b) => a.created_at.localeCompare(b.created_at))
          .forEach((message) => {
            const author = `${message.sender_agent_id}`;
            const createdAt = new Date(message.created_at);
            const dayLabel = createdAt.toLocaleDateString([], {
              month: "short",
              day: "numeric",
              year: "numeric",
            });
            if (dayLabel !== previousDay) {
              const divider = document.createElement("div");
              divider.className = "chat-day";
              divider.textContent = dayLabel;
              messagesEl.appendChild(divider);
              previousDay = dayLabel;
              previousAuthor = null;
            }

            const card = document.createElement("article");
            card.className = "message-card";
            const grouped = previousAuthor === author;
            if (grouped) {
              card.classList.add("grouped");
            }
            const initials = message.sender_agent_id
              .split(/[^A-Za-z0-9]+/)
              .filter(Boolean)
              .slice(0, 2)
              .map((part) => part[0].toUpperCase())
              .join("") || "AG";
            const timeLabel = createdAt.toLocaleTimeString([], {
              hour: "numeric",
              minute: "2-digit",
            });
            card.innerHTML = `
              <div class="message-shell ${grouped ? "grouped" : ""}">
                <div class="avatar">${escapeHtml(initials)}</div>
                <div class="message-stack">
                  ${grouped ? "" : `<div class="message-head">
                    <div class="message-author">${escapeHtml(message.sender_agent_id)}</div>
                    <div class="message-thread">${escapeHtml(message.thread_id)}</div>
                  </div>`}
                  <div class="message-body"><span class="message-route">@${escapeHtml(message.recipient_agent_id)}</span>${escapeHtml(message.payload)}</div>
                  <div class="message-meta">${escapeHtml(timeLabel)} · sender node ${escapeHtml(message.sender_node)}</div>
                </div>
              </div>
            `;
            messagesEl.appendChild(card);
            previousAuthor = author;
          });
      }

      function renderEvents() {
        const events = visibleEvents();
        if (!events.length) {
          eventsEl.innerHTML = `<div class="empty">No protocol events match the current filters.</div>`;
          return;
        }

        eventsEl.innerHTML = "";
        events
          .sort((a, b) => b.created_at.localeCompare(a.created_at))
          .slice(0, 250)
          .forEach((event) => {
            const card = document.createElement("article");
            card.className = "event-card";
            card.innerHTML = `
              <div><span class="tag">event</span><span class="tag">${escapeHtml(event.kind)}</span></div>
              <div class="meta">${escapeHtml(event.created_at)} · channel=${escapeHtml(event.channel_id || "-")} · thread=${escapeHtml(event.thread_id || "-")}</div>
              <div class="meta">actor=${escapeHtml(event.actor || "-")} · node=${escapeHtml(event.node_id || "-")}</div>
            `;
            eventsEl.appendChild(card);
          });
      }

      function render() {
        renderChannels();
        renderMessages();
        renderEvents();
      }

      async function fetchJson(path) {
        const response = await fetch(path);
        if (!response.ok) {
          throw new Error(`Request failed: ${path}`);
        }
        return response.json();
      }

      async function loadMessagesForActiveChannel() {
        const threads = activeThreads();
        const threadIds = new Set(threads.map((thread) => thread.thread_id));
        const retained = state.messages.filter((message) => !threadIds.has(message.thread_id));
        const loaded = await Promise.all(
          threads.map((thread) => fetchJson(`/threads/${encodeURIComponent(thread.thread_id)}/messages`))
        );
        state.messages = retained.concat(loaded.flat());
      }

      async function bootstrap() {
        const [channels, threads, events] = await Promise.all([
          fetchJson("/channels"),
          fetchJson("/threads"),
          fetchJson("/events?limit=200"),
        ]);
        state.channels = channels;
        state.threads = threads;
        state.events = events;
        if (!state.activeChannelId && channels.length) {
          state.activeChannelId = channels[0].channel_id;
          await loadMessagesForActiveChannel();
        }
        render();
      }

      Object.values(filters).forEach((input) => input.addEventListener("input", render));
      tabs.chat.addEventListener("click", () => setTab("chat"));
      tabs.observability.addEventListener("click", () => setTab("observability"));

      bootstrap().catch((error) => {
        messagesEl.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
      });

      const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/events`);
      ws.onmessage = async (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "event") {
          state.events.push(msg.data);
        } else if (msg.type === "envelope") {
          const existing = state.messages.findIndex((item) => item.envelope_id === msg.data.envelope_id);
          if (existing >= 0) {
            state.messages[existing] = msg.data;
          } else {
            state.messages.push(msg.data);
          }
          if (!state.channels.find((channel) => channel.channel_id === msg.data.channel_id)) {
            state.channels = await fetchJson("/channels");
          }
          if (!state.threads.find((thread) => thread.thread_id === msg.data.thread_id)) {
            state.threads = await fetchJson("/threads");
          }
        }
        render();
      };
      ws.onclose = () => {
        eventsEl.insertAdjacentHTML("afterbegin", `<div class="empty">Live stream disconnected. Refresh the page to reconnect.</div>`);
      };
    </script>
  </body>
</html>
"""
