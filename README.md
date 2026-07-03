# rnd.devtools.relay

Minimal MVP for an agent-agnostic communications relay:

- FastAPI relay service with SQLite-backed durability
- threaded string-message envelopes with event history
- WebSocket live stream for observability
- cwd-scoped relay CLI for agent/operator workflows

## Quick start

```bash
conda activate /Users/pepelopez/Documents/Programming/rnd.devtools.relay/.conda
pip install -e .
relay serve --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/ui` for the observability UI.

## CLI workflow

```bash
relay init
relay config -a coordinator -d "Coordinates frontend debugging" -c frontend-debug -s coordinator-main
relay config show
relay register -a network-specialist -d "Investigates websocket failures" -c frontend-debug -s relay-main
relay create -s relay -c frontend-debug
relay add-channel -s relay -c backend-debug
relay add-agent -s relay -c frontend-debug -a codex
relay ls
relay send -m "inspect websocket warnings" -a network-specialist
relay respond -m "warning fixed" -t THREAD_ID
relay ack -t THREAD_ID
```

`relay ls` now returns discovery-oriented entries for agents on the caller's active channel and active session. Use `relay ls -a` to expand discovery to all agents on the caller's active session across channels.

Each entry includes:

- `agent_id`
- `description`
- `comms` metadata such as home node, presence, active channel, and active session

## Engineer tmux helpers

The relay CLI now includes thin tmux lifecycle helpers:

- `relay create -s SESSION -c CHANNEL`
- `relay add-channel -s SESSION -c CHANNEL`
- `relay add-agent -s SESSION -c CHANNEL -a AGENT`
- `relay delete-session SESSION`
- `relay delete-channel -s SESSION -c CHANNEL`
- `relay delete-agent -s SESSION -c CHANNEL -a AGENT`

Each create/add command executes the tmux operation and returns the next attach/select command to jump into the new layout.

## Turn semantics

Relay uses a simple thread model to prevent infinite acknowledgement loops:

- `relay send` opens or advances work on a direct thread
- `relay respond` continues the same thread when follow-up is needed
- `relay ack` explicitly acknowledges the latest inbound message on the thread and ends that exchange

When an agent receives a response, it should either:

- `relay respond` if more work or clarification is needed on the same thread
- `relay ack -t THREAD_ID` if the response resolves the exchange
