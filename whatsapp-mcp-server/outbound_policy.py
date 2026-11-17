"""Opt-in limits on what an MCP client can send out through the bridge.

Incoming WhatsApp text is untrusted input that the agent reads verbatim, and the
same agent can send messages and files. A prompt-injected agent is therefore an
exfiltration path (the "lethal trifecta"). Two off-by-default switches shrink
that blast radius:

* ``WHATSAPP_MCP_READ_ONLY`` - nothing is sent to WhatsApp at all (messages,
  files, audio, reactions, read receipts).
* ``WHATSAPP_ALLOWED_RECIPIENTS`` - comma-separated phone numbers and/or JIDs;
  outbound actions are refused for any other chat.

Both are enforced where the MCP server calls the bridge (whatsapp.py), so no
tool can bypass them. They do not restrict direct callers of the bridge REST
API, which stays protected by its own bearer token.

Env vars are read on every call (cheap) so they can be changed in tests, and a
malformed allowlist fails closed.
"""

import os
import re
from collections.abc import Callable

READ_ONLY_ENV = "WHATSAPP_MCP_READ_ONLY"
ALLOWED_RECIPIENTS_ENV = "WHATSAPP_ALLOWED_RECIPIENTS"

# MCP tools that send something to WhatsApp; removed from the tool list in read-only mode.
OUTBOUND_TOOLS = ("send_message", "send_reaction", "mark_messages_read", "send_file", "send_audio_message")

_TRUE = {"1", "true", "yes", "on"}
_USER_SERVERS = {"s.whatsapp.net", "c.us", "lid"}

AliasesFn = Callable[[str], list[str]]


def is_read_only() -> bool:
    return os.getenv(READ_ONLY_ENV, "").strip().lower() in _TRUE


def _allowlist_entries() -> list[str] | None:
    """Parsed allowlist entries, or None when no allowlist is configured."""
    raw = os.getenv(ALLOWED_RECIPIENTS_ENV, "")
    if not raw.strip():
        return None
    return [entry.strip() for entry in re.split(r"[,\s]+", raw) if entry.strip()]


def _forms(value: str, aliases_fn: AliasesFn) -> set[str]:
    """Every spelling of one chat identifier that should compare equal."""
    value = value.strip().lower()
    if "@" in value:
        user, _, server = value.partition("@")
        if server in _USER_SERVERS and user:
            # Phone and LID forms of one person: expand via the LID map.
            return {a.lower() for a in aliases_fn(user)} | {value}
        return {value}  # group (@g.us) and anything else: exact JID only
    digits = re.sub(r"\D", "", value)
    if not digits:
        return set()
    return {a.lower() for a in aliases_fn(digits)}


def denial_reason(target: str, aliases_fn: AliasesFn) -> str | None:
    """Why an outbound action toward ``target`` must be refused, or None if allowed."""
    if is_read_only():
        return f"Read-only mode is enabled ({READ_ONLY_ENV}): sending to WhatsApp is disabled."

    entries = _allowlist_entries()
    if entries is None:
        return None

    target_forms = _forms(target, aliases_fn)
    if target_forms:
        for entry in entries:
            if target_forms & _forms(entry, aliases_fn):
                return None
    return f"Recipient is not in {ALLOWED_RECIPIENTS_ENV}; refusing to send."


def remove_outbound_tools(server) -> list[str]:
    """Unregister the send-capable tools so a read-only client never sees them.

    Returns the names removed. Enforcement in whatsapp.py still applies even if
    a tool were somehow left registered.
    """
    removed = []
    for name in OUTBOUND_TOOLS:
        try:
            server.remove_tool(name)
            removed.append(name)
        except Exception:  # noqa: BLE001 - already absent: nothing to hide
            continue
    return removed
