---
name: use-relay
description: Use this skill when an agent needs to communicate over rnd.devtools.relay through the relay CLI, including initializing local relay state, configuring agent identity and channels, sending messages, and reading thread or event history.
---

# Use Relay

Use this skill when your job requires coordination through the local relay service instead of ad hoc terminal output or private assumptions. This skill is CLI-only. Do not explain or use the HTTP API directly unless the user explicitly asks for lower-level transport details.

The relay CLI is workspace-scoped:

- `relay init` creates `.relay/` in the current working directory.
- `.relay/config.json` holds the local relay workspace state.
- `relay config` registers the agent identity, joins it to one or more channels, and records one or more tmux sessions.
- `relay config show` shows the local configured agent, channel, and session context.
- `relay ls` shows who is discoverable before you target `-a`.
- `relay send` opens or reuses a direct bridge thread, creates a new request turn, and delivers it into tmux immediately.
- `relay respond` continues an existing bridge thread when follow-up is needed.
- `relay ack` acknowledges the latest inbound message on a thread when the exchange is resolved.

## Core rules

1. Before sending anything, make sure the current directory has been initialized with `relay init`.
2. Before sending anything, make sure the agent has been configured with `relay config` for the relevant channel and session.
3. Before targeting `-a`, verify the recipient is discoverable in the channel with `relay ls`.
4. Keep messages short, explicit, and action-oriented.
5. Treat every message as operator-visible in the UI.
6. Prefer using the active configured channel instead of inventing new session boundaries casually.
7. When you receive a response, either continue the same thread with `relay respond` or end that exchange with `relay ack -t THREAD_ID`.

## Standard workflow

### 1. Initialize the local relay workspace

Run this once per working directory:

```bash
relay init
```

This creates `.relay/config.json` in the cwd.

### 2. Configure the agent and channel membership

Run:

```bash
relay config -a AGENT_ID -c CHANNEL_ID -s SESSION_ID
```

You may repeat `--channel` and `--session`:

```bash
relay config -a AGENT_ID -c alpha -c beta -s main -s debug
```

This command is responsible for:

- registering the local agent identity
- joining the configured channels
- recording the configured tmux sessions
- setting the active channel
- setting the active session

Show the local orientation state at any time:

```bash
relay config show
```

### 3. Send a message

The default send flow is:

```bash
relay ls
relay send -m "short request here" -a RECIPIENT_AGENT_ID
```

`relay send -a ...` should only target agents currently subscribed to the active channel.
The UI may render the recipient as an `@mention`, but that mention is derived from envelope metadata, not from the message string.
Delivery happens automatically on send when the receiver tmux session exists.
`relay send` opens a request turn and expects one response.

Reply on an existing bridge thread:

```bash
relay respond -m "response body here" -t THREAD_ID
```

`relay respond` uses the active configured channel, active configured session, and the local configured agent to infer the peer recipient from the thread metadata.
Use `relay respond` when the thread needs follow-up work, clarification, or another task turn.

If the latest inbound message resolves the exchange, acknowledge it:

```bash
relay ack -t THREAD_ID
```

## Message-writing guidance

Good relay CLI messages are:

- direct: state the ask in the first sentence
- scoped: mention the artifact, system, or task being discussed
- inspectable: easy for an engineer to understand from the UI alone

Preferred pattern:

```text
Short request. Context: relevant system or file. Output needed: exact deliverable.
```

Example:

```text
Inspect relay websocket warnings on localhost:8000. Output needed: root cause and fix recommendation.
```

Avoid:

- multi-paragraph wandering context
- hidden asks at the end of the message
- messages that depend on private memory instead of relay-visible context

## Reading history and observing traffic

Read thread history when you already know the thread ID:

```bash
relay history THREAD_ID
```

Read recent protocol events when you need delivery or lifecycle visibility:

```bash
relay tail --limit 25
```

List discoverable agents in the active channel:

```bash
relay ls
```

List all agents registered in the platform:

```bash
relay ls -a
```

## When replying

When you answer a request:

1. Use the current relay workspace in the cwd.
2. Send into the configured active channel unless told otherwise.
3. Keep the first sentence outcome-oriented.
4. If you are blocked, say exactly what is missing.
5. If the request is complete, state completion clearly and include the result or next handoff.
6. When you later receive a response, either continue on the same thread with `relay respond` or finish it with `relay ack -t THREAD_ID`.

Examples:

```text
Completed. The warning is caused by uvicorn running without a websocket backend; install `websockets` and restart the relay.
```

```text
Blocked. I need the target channel before I can send the delegation.
```

## Failure handling

- If `relay init` has not been run, stop and initialize the cwd first.
- If `relay config` has not been run, stop and configure the agent, channels, and sessions first.
- If `relay ls` does not show the target agent in the active channel, do not send to it.
- If the recipient agent is not registered or not subscribed to the active channel, do not guess a runtime; surface the failure clearly.
- If sending fails, surface the exact command and target that failed.
- If `relay ack` says there is no inbound unacknowledged message on the thread, inspect `relay history THREAD_ID` before retrying.

## Defaults

- CLI entrypoint: `relay`
- Local state directory: `.relay/`
- Base URL: `http://127.0.0.1:8000`
- Session shape: `relay config -a AGENT -c CHANNEL -s SESSION`
- Send shape: `relay send -m "..." -a recipient-agent`
- Turn rule: `send` opens work, `respond` continues work, `ack` resolves the latest inbound exchange
- Recipient mention: UI-only, derived from relay metadata
- Delivery: automatic on `relay send` through the receiver's tmux session
- Human-readable style: concise, explicit, operator-friendly
