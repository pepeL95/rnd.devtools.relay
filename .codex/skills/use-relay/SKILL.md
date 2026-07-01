---
name: use-relay
description: Use this skill when an agent needs to communicate over rnd.devtools.relay through the relay CLI, including initializing local relay state, configuring agent identity and channels, sending messages, and reading thread or event history.
---

# Use Relay

Use this skill when your job requires coordination through the local relay service instead of ad hoc terminal output or private assumptions. This skill is CLI-only. Do not explain or use the HTTP API directly unless the user explicitly asks for lower-level transport details.

The relay CLI is workspace-scoped:

- `relay init` creates `.relay/` in the current working directory.
- `.relay/config.json` holds the local relay workspace state.
- `relay config` registers the agent identity and joins it to one or more channels.
- `relay config show` shows the local configured agent and channel context.
- `relay ls` shows who is discoverable before you target `-a`.
- `relay send` uses the configured identity and active channel.

## Core rules

1. Before sending anything, make sure the current directory has been initialized with `relay init`.
2. Before sending anything, make sure the agent has been configured with `relay config` for the relevant channel.
3. Before targeting `-a`, verify the recipient is discoverable in the channel with `relay ls`.
4. Keep messages short, explicit, and action-oriented.
5. Treat every message as operator-visible in the UI.
6. Prefer using the active configured channel instead of inventing new session boundaries casually.

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
relay config -a AGENT_ID -c CHANNEL_ID
```

You may repeat `--channel` to join multiple channels:

```bash
relay config -a AGENT_ID -c alpha -c beta
```

This command is responsible for:

- registering the local agent identity
- joining the configured channels
- setting the active channel

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

Examples:

```text
Completed. The warning is caused by uvicorn running without a websocket backend; install `websockets` and restart the relay.
```

```text
Blocked. I need the target channel before I can send the delegation.
```

## Failure handling

- If `relay init` has not been run, stop and initialize the cwd first.
- If `relay config` has not been run, stop and configure the agent and channels first.
- If `relay ls` does not show the target agent in the active channel, do not send to it.
- If the recipient agent is not registered or not subscribed to the active channel, do not guess a runtime; surface the failure clearly.
- If sending fails, surface the exact command and target that failed.

## Defaults

- CLI entrypoint: `relay`
- Local state directory: `.relay/`
- Base URL: `http://127.0.0.1:8000`
- Send shape: `relay send -m "..." -a recipient-agent`
- Recipient mention: UI-only, derived from relay metadata
- Human-readable style: concise, explicit, operator-friendly
