from __future__ import annotations

import time

from app.core.types import Pending
from app.flows.common import ensure_pending_option_ids


def pending_options_text(session) -> str:
    try:
        return f"SISTEMA_PENDING_OPTIONS: {session.pending.model_dump()}"
    except Exception:
        return f"SISTEMA_PENDING_OPTIONS: {session.pending}"


def mark_pending_started(session) -> None:
    if getattr(session, "pending_started_at", None) is None:
        session.pending_started_at = time.time()
    session.followup_sent_at = None


def clear_pending_timers(session) -> None:
    session.pending_started_at = None
    session.followup_sent_at = None


def set_pending(session, pending_type: str, options: list[dict] | None = None) -> None:
    if session.pending is None:
        session.pending = Pending()

    session.pending.type = pending_type
    session.pending.options = ensure_pending_option_ids(list(options or []))

    if pending_type == "none":
        clear_pending_timers(session)
    else:
        mark_pending_started(session)