# hermes-intercom

Local cross-session messaging for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — let your interactive CLI/TUI sessions discover each other and exchange messages automatically, Claude Code style.

Based on the design proposed in [NousResearch/hermes-agent#81885](https://github.com/NousResearch/hermes-agent/issues/81885) ("Local cross-session messaging (intercom) — discovery + peer inbox for interactive sessions"). Credit for the architecture — broker-less registry, single multiplexed tool, steer-based delivery — goes to that issue's proposal; this repo is a working implementation with field-tested refinements.

## What it does

Each Hermes session running the plugin:

1. **Registers itself** — writes `~/.hermes/intercom/sessions/<session_id>.json` (name, pid, cwd) and binds a `0600` Unix socket. Dead sessions are swept automatically.
2. **Discovers peers** — the agent gets an `intercom` tool: `action="list"` returns live sessions with name, cwd and busy state.
3. **Messages them** — `action="send"` delivers plain text to a named peer; `action="ask"` sends and blocks until the peer replies (or timeout), with the reply routed back automatically.

Every send returns a receipt: `delivered` (receiver starts a turn or was steered mid-turn) / `held` (accepted, surfaces at its next boundary) / `refused`. Messages parked while the receiver has no reachable agent spill to an owner-only file for that endpoint's lifetime.

On the receiving side, arrival is **immediate and automatic**:

- If the receiving session is **idle**, the message is submitted into its input queue — the session **starts a turn by itself**, no user action needed.
- If it's **mid-turn**, the message is steered into the running turn (drained between tool calls) or parked for its next turn via the `pre_llm_call` plugin hook.
- The message renders once, as part of that turn, in a compact frame:

```
📡 hermes@<session-name> says:
<message body>
```

- Echo suppression strips any intercom headers the sending agent quoted back, so the receiver never sees a duplicated header.
- The delivered text is inert: it cannot approve actions or run slash commands.

## Demo

```
📡 hermes@researcher says:
The schema migration just landed on main; rebase your branch before continuing with the payments work.
```

Validated end-to-end (2026-08): CLI ↔ TUI round trip where the receiver auto-started a turn, replied using the `intercom` tool on its own initiative, and the reply arrived in the original sender's next turn. Mid-turn steering also verified live.

## Install

```bash
git clone https://github.com/majordave/hermes-intercom ~/.hermes/plugins/intercom
hermes plugins enable intercom
```

Then just run two `hermes chat` sessions. Ask either one:

> use intercom to list the live sessions and say hi to the other one

## Configuration

In `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - intercom
  entries:
    intercom:
      settings:
        inbound: always   # always | never (never disables the inbox)
```

## Safety

- Socket is `0600`, and the receiver verifies the peer UID — no TCP listener, ever
- Inbound messages are inert text; the frame identifies the sending session so the model never mistakes it for user instructions
- Per-peer rate limit (6/min), duplicate suppression (120 s window), pending queue cap with owner-only disk spill
- Sessions publish their busy/idle state (`turn_active`) to the registry so senders can pick idle peers

## Implementation notes

Findings from building this against the real Hermes codebase are in [`docs/findings.md`](docs/findings.md) — most notably that `HermesCLI` is a local of `main()`, so the live instance must be found via GC, and that `HermesCLI._pending_input` is the wake channel that makes idle sessions start turns automatically.

## Roadmap

Implemented in v2/v3:

- ✅ Blocking `ask`/`reply` (request-response with explicit request IDs and timeout)
- ✅ Delivery receipts (`delivered` / `held` / `refused`)
- ✅ Busy/idle state published to the registry and surfaced via `action="list"`
- ✅ Owner-only inbox spool for parked messages during an endpoint's lifetime
- ✅ Frame-forgery defenses (legacy tag neutralization + header spoofing broken via zero-width space)
- ✅ Stable profile-based session naming
- ✅ Single-render compact frame (`📡 hermes@<session> says:`) — no duplicate banner/turn rendering
- ✅ Echo-header suppression on send

Still ahead:

- First-class TUI/desktop peers via the tui_gateway
- Stable `agent_id` per profile (decoupling identity from pid)
- Durable mailbox identity for recovery across process restarts
- Migrate delivery to the official host seam if upstream #70406 merges
- Windows named pipes

Cross-machine messaging stays out of scope — that's the A2A platform plugin's lane.

## License

MIT
