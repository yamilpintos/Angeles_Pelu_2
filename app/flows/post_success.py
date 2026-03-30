from __future__ import annotations
import time
from app.core.types import Pending


POST_SUCCESS_CONTEXT_MAX_MESSAGES = 3
POST_SUCCESS_CONTEXT_TTL_SECONDS = 1 * 60


def clear_soft_post_success_context(session) -> None:
    if session.pending is None:
        session.pending = Pending()

    if session.pending.type == "none":
        session.pending.options = []

    session.pending_started_at = None
    session.followup_sent_at = None


def enter_soft_post_success_context(session, operation: str) -> None:
    if session.pending is None:
        session.pending = Pending()

    session.intent = "unknown"
    session.last_booking_id = ""
    session.last_user_message_at = None
    session.pending.type = "none"
    session.pending.options = [
        {
            "__soft_post_success__": True,
            "operation": str(operation or "").strip().lower(),
            "messages_left": POST_SUCCESS_CONTEXT_MAX_MESSAGES,
            "started_at": time.time(),
        }
    ]
    session.pending_started_at = time.time()
    session.followup_sent_at = None
    session.draft = session.draft.__class__(
        customer_name=None,
        barber=None,
        day_text=None,
        time_hhmm=None,
        service_name=None,
        service_key=None,
        age=None,
        latest_finish_hhmm=None,
    )


def get_soft_post_success_meta(session) -> dict:
    try:
        if not session.pending or session.pending.type != "none":
            return {}
        opts = session.pending.options or []
        if not opts or not isinstance(opts[0], dict):
            return {}
        meta = opts[0]
        if meta.get("__soft_post_success__") is True:
            return meta
    except Exception:
        pass
    return {}


def soft_post_success_context_expired(session) -> bool:
    meta = get_soft_post_success_meta(session)
    if not meta:
        return True

    started_at = meta.get("started_at") or getattr(session, "pending_started_at", None)
    messages_left = meta.get("messages_left", 0)

    try:
        started_at_f = float(started_at or 0)
    except Exception:
        started_at_f = 0.0

    elapsed = time.time() - started_at_f if started_at_f else (POST_SUCCESS_CONTEXT_TTL_SECONDS + 1)
    return elapsed >= POST_SUCCESS_CONTEXT_TTL_SECONDS or int(messages_left or 0) <= 0


def consume_soft_post_success_message(session) -> None:
    meta = get_soft_post_success_meta(session)
    if not meta:
        return

    try:
        left = int(meta.get("messages_left", 0))
    except Exception:
        left = 0

    meta["messages_left"] = max(0, left - 1)
    if session.pending is None:
        session.pending = Pending()
    session.pending.type = "none"
    session.pending.options = [meta]


def reset_session_after_success(session) -> None:
    clear_soft_post_success_context(session)

    session.intent = "unknown"
    session.last_booking_id = ""
    session.last_user_message_at = None

    if session.pending is None:
        session.pending = Pending()
    session.pending.type = "none"
    session.pending.options = []

    session.pending_started_at = None
    session.followup_sent_at = None

    session.draft = session.draft.__class__(
        customer_name=None,
        barber=None,
        day_text=None,
        time_hhmm=None,
        service_name=None,
        service_key=None,
        age=None,
        latest_finish_hhmm=None,
    )