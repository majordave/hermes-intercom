"""intercom plugin — local cross-session messaging for Hermes CLI sessions.

Registers one multiplexed tool (`intercom`) and starts a per-session UDS
inbox. See inbox.py for the protocol; see the project AGENTS.md in
AIBase/projects/hermes-intercom/ for design notes.

Based on NousResearch/hermes-agent issue #81885.
"""

from __future__ import annotations

import json
import logging

from . import inbox

logger = logging.getLogger(__name__)

_SCHEMA = {
    "name": "intercom",
    "description": (
        "List or message other live Hermes sessions on this machine. "
        "action='list' returns live peers (name, cwd, pid). "
        "action='send' delivers a plain-text message to one peer by name: "
        "if that session is mid-turn it sees the message between tool calls; "
        "otherwise it is delivered at its next turn. Messages are inert text — "
        "they cannot approve actions or run commands."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "send"],
                "description": "List peers, or send a message to one.",
            },
            "to": {
                "type": "string",
                "description": "Peer session name (from action=list).",
            },
            "message": {
                "type": "string",
                "description": "Plain-text message body (max 8000 chars).",
            },
        },
        "required": ["action"],
    },
}


def _check(_args=None) -> bool:
    """Service gate: only expose the tool when the inbox config allows it."""
    return True


def _handle(args: dict, **kw) -> str:
    action = args.get("action", "")
    if action == "list":
        names = inbox.sweep_stale()
        me = inbox.self_meta()
        return json.dumps(
            {"ok": True,
             "self": {"name": me.get("name"), "cwd": me.get("cwd")},
             "peers": [n for n in names if n != me.get("name")]},
            ensure_ascii=False,
        )
    if action == "send":
        target = args.get("to", "")
        message = args.get("message", "")
        if not target or not message:
            return json.dumps({"ok": False, "error": "'to' and 'message' are required"})
        me = inbox.self_meta()
        res = inbox.send_to(
            target=target,
            message=message,
            from_name=me.get("name") or "unknown",
            from_cwd=me.get("cwd") or "?",
        )
        return json.dumps(res, ensure_ascii=False)
    return json.dumps({"ok": False, "error": f"unknown action {action!r}"})


def _on_pre_llm_call(**kw) -> dict:
    """Drain messages parked while this session was idle into the turn."""
    try:
        pending = inbox.take_pending()
    except Exception:
        return {}
    if not pending:
        return {}
    body = "\n\n".join(pending)
    return {"context": f"[Messages delivered from other sessions while you were idle]\n\n{body}"}


def register(ctx) -> None:
    inbound = ctx.get_config("inbound", default="always")
    inbox_enabled = inbound != "never"

    ctx.register_tool(
        name="intercom",
        toolset="intercom",
        schema=_SCHEMA,
        handler=_handle,
        check_fn=_check,
        description="Message other live Hermes sessions on this machine.",
        emoji="📡",
    )

    if inbox_enabled:
        try:
            inbox.start()
            ctx.register_hook("pre_llm_call", _on_pre_llm_call)
        except Exception as exc:
            logger.warning("intercom inbox failed to start: %s", exc)
