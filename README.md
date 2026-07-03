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
relay config -a coordinator -c frontend-debug -s coordinator-main
relay config show
relay register -a network-specialist -c frontend-debug
relay send -m "inspect websocket warnings" -a network-specialist
relay respond -m "warning fixed" -t THREAD_ID
relay ack -t THREAD_ID
```

## Turn semantics

Relay uses a simple thread model to prevent infinite acknowledgement loops:

- `relay send` opens or advances work on a direct thread
- `relay respond` continues the same thread when follow-up is needed
- `relay ack` explicitly acknowledges the latest inbound message on the thread and ends that exchange

When an agent receives a response, it should either:

- `relay respond` if more work or clarification is needed on the same thread
- `relay ack -t THREAD_ID` if the response resolves the exchange
