---
name: relay-simple
description: Use this skill when an agent needs to communicate through the relay CLI in an environment where relay is already installed and available on PATH. Focus on sending, replying, listing agents, and inspecting relay state without explaining or performing bootstrap setup.
---

# Relay Simple

Use this skill when you need to coordinate with another agent through `relay` and the relay CLI is already available for use. Do not spend time explaining bootstrapping, installation, or environment setup unless the user explicitly asks for that.

If command usage is unclear, check:

- `relay --help`
- `relay <command> --help`

## Core rules

1. Use the relay CLI instead of ad hoc terminal chatter when agent-to-agent coordination is needed.
2. Assume relay is already available on `PATH`.
3. Assume the current workspace is already initialized and configured with an active channel and active session unless the user says otherwise or the command fails.
4. Keep relay messages short, explicit, and action-oriented.
5. Treat every relay message as visible to the engineer in the UI.
6. Prefer replying on the existing thread instead of opening a new conversation when continuing prior work.
7. Treat `relay respond` as the end of the current turn. If more work is needed after a response, open a new request with `relay send`.

## Default workflow

Check who is available in the current channel:

```bash
relay ls
```

Send a new request:

```bash
relay send -m "short request here" -a RECIPIENT_AGENT_ID
```

`relay send` opens a request and expects one response.

Reply on an existing thread:

```bash
relay respond -m "response body here" -t THREAD_ID
```

`relay respond` answers the current open request and closes that turn. Do not use it to acknowledge a response.

Inspect thread history:

```bash
relay history THREAD_ID
```

Inspect recent protocol events:

```bash
relay tail --limit 25
```

Show the current local relay orientation:

```bash
relay config show
```

If configuration is missing, the expected shape is:

```bash
relay config -a AGENT_ID -c CHANNEL_ID -s SESSION_ID
```

## Message guidance

Good relay messages are:

- direct
- scoped
- easy to understand from the UI alone

Preferred pattern:

```text
Short request. Context: relevant system or file. Output needed: exact deliverable.
```

Example:

```text
Inspect websocket warnings on localhost:8000. Output needed: root cause and concrete fix.
```

## Reply guidance

When responding on a thread:

1. Put the outcome in the first sentence.
2. If blocked, say exactly what is missing.
3. If complete, state completion clearly and include the result or next handoff.
4. If you need more work after receiving a response, use a new `relay send` instead of another `relay respond`.

Examples:

```text
Completed. Root cause is a missing websocket backend in uvicorn. Install `websockets` and restart the relay server.
```

```text
Blocked. I need the target log file or reproduction steps to continue.
```

## Failure handling

- If a relay command fails, read the error and correct the command instead of guessing.
- If the recipient does not appear in `relay ls`, do not send to that agent.
- If you are unsure about flags or subcommands, use `relay --help` or `relay <command> --help`.
- Do not explain the HTTP API unless the user explicitly asks for it.
- Do not send `relay respond` to a response. Responses are terminal by default.
