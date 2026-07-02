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
```

## Turn semantics

Relay uses a simple turn model to prevent infinite reply loops:

- `relay send` opens a new request and expects one response
- `relay respond` closes the current open request in the thread
- if you need follow-up work after a response, use a new `relay send`

Do not use `relay respond` to acknowledge a response. Responses are terminal by default.
