# TMUX Quickstart

This guide is for engineers operating `rnd.devtools.relay` with tmux.

The current relay delivery convention is:

- tmux session = relay workspace
- tmux window = relay channel
- tmux pane title = relay agent name

Relay delivery currently resolves a recipient by:

1. looking up the recipient's configured tmux session from relay config and participant metadata
2. targeting the tmux window whose name matches the relay channel
3. targeting the tmux pane whose title matches the recipient agent name

If the session, window, or pane title do not match this convention, delivery will fail or land in the wrong place.

## Create and name a session

Create a detached tmux session named `relay`:

```bash
tmux new-session -d -s relay
```

Create and attach immediately instead:

```bash
tmux new-session -s relay
```

## Create and name a window

Rename the first window in the `relay` session to match the relay channel:

```bash
tmux rename-window -t relay:0 frontend-debug
```

Create another named channel window:

```bash
tmux new-window -t relay -n backend-debug
```

## Create and name panes

Split the `frontend-debug` window into two panes:

```bash
tmux split-window -h -t relay:frontend-debug
```

Or split vertically:

```bash
tmux split-window -v -t relay:frontend-debug
```

Set pane titles to match relay agent names:

```bash
tmux select-pane -t relay:frontend-debug.0 -T codex
tmux select-pane -t relay:frontend-debug.1 -T quasipilot
```

Relay uses pane titles for routing. Pane indexes like `.0` and `.1` are only for local tmux control and should not be treated as stable routing identifiers.

## Inspect the layout

List windows in the session:

```bash
tmux list-windows -t relay
```

List panes in the channel window with pane IDs and pane titles:

```bash
tmux list-panes -t relay:frontend-debug -F '#{pane_id} #{pane_title}'
```

Expected shape:

```text
%12 codex
%13 quasipilot
```

## Attach and detach

Attach to the tmux session:

```bash
tmux attach -t relay
```

Detach from tmux:

```text
Ctrl-b d
```

Switch between windows after attaching:

```text
Ctrl-b n
Ctrl-b p
```

Or jump directly to a specific window:

```bash
tmux select-window -t relay:frontend-debug
```

## Relay-aligned example

Create a relay workspace session, a `frontend-debug` channel window, and two agent panes:

```bash
tmux new-session -d -s relay
tmux rename-window -t relay:0 frontend-debug
tmux split-window -h -t relay:frontend-debug
tmux select-pane -t relay:frontend-debug.0 -T codex
tmux select-pane -t relay:frontend-debug.1 -T quasipilot
tmux attach -t relay
```

Then configure relay from each agent workspace:

```bash
relay init
relay config -a codex -c frontend-debug -s relay
```

And from the other agent workspace:

```bash
relay init
relay config -a quasipilot -c frontend-debug -s relay
```

## Common failures

Wrong pane receives the message:

- the recipient pane title does not match the agent name
- the message was sent to the active pane in the session before pane-title routing was configured

Delivery fails with session not found:

- the configured relay session does not exist in tmux
- the agent configured the wrong `-s/--session` value

Delivery fails with pane not found:

- the channel window name does not match the relay channel
- no pane title matches the recipient agent name

Useful corrections:

```bash
tmux rename-window -t relay:0 frontend-debug
tmux select-pane -t relay:frontend-debug.0 -T codex
tmux select-pane -t relay:frontend-debug.1 -T quasipilot
tmux list-panes -t relay:frontend-debug -F '#{pane_id} #{pane_title}'
relay config show
```
