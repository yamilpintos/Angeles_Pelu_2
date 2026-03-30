from __future__ import annotations

import re

from app.actions.cancel import cancel_booking
from app.core.utils import merge_draft
from app.flows.cancel_flow import cancel_confirm_sys_event, start_cancel_flow
from app.flows.common import ai_confirmed, ai_rejected, resolve_pending_option
from app.flows.pending_helpers import set_pending
from app.flows.post_success import enter_soft_post_success_context, reset_session_after_success
from app.flows.replying import reply_async, reply_with_event, safe_reply_text


def _resolve_numeric_pending_choice(session, text: str) -> dict | None:
    options = list(getattr(getattr(session, "pending", None), "options", None) or [])
    if not options:
        return None

    raw = str(text or "").strip().lower()

    if raw in {"ultimo", "último", "el ultimo", "el último"}:
        return options[-1]

    match = re.fullmatch(r"(?:el\s+)?(\d{1,2})", raw)
    if not match:
        return None

    idx = int(match.group(1))
    if 1 <= idx <= len(options):
        return options[idx - 1]
    return None


def handle_pending_cancel(phone: str, session, ai, text: str) -> bool:
    pending_type = session.pending.type if session.pending else "none"

    if pending_type == "confirm_cancel":
        if ai_confirmed(ai):
            bid = int(session.last_booking_id or "0")
            if not bid:
                reset_session_after_success(session)
                reply_with_event(phone, session, "SISTEMA_CANCEL_ERROR: missing booking_id", text)
                return True

            result = cancel_booking(bid)
            if result.get("ok"):
                enter_soft_post_success_context(session, "cancel")
                reply_with_event(phone, session, "SISTEMA_CANCEL_OK", text)
            else:
                reset_session_after_success(session)
                reply_with_event(phone, session, f"SISTEMA_CANCEL_ERROR: {result.get('error')}", text)
            return True

        if ai_rejected(ai):
            reset_session_after_success(session)
            reply_with_event(phone, session, "SISTEMA_CONFIRMATION_CANCELLED", text)
            return True

        reply_with_event(
            phone,
            session,
            cancel_confirm_sys_event(
                {
                    "customer_name": session.draft.customer_name,
                    "barber": session.draft.barber,
                    "date_text": session.draft.day_text,
                    "time_hhmm": session.draft.time_hhmm,
                    "service_name": session.draft.service_name,
                }
            ),
            text,
        )
        return True

    if pending_type == "choose_cancel":
        chosen = _resolve_numeric_pending_choice(session, text) or resolve_pending_option(session, ai)
        if chosen:
            session.last_booking_id = str(chosen.get("id") or "")
            patch = session.draft.__class__(
                customer_name=chosen.get("customer_name"),
                barber=chosen.get("barber"),
                day_text=chosen.get("date_text") or chosen.get("day_text"),
                time_hhmm=chosen.get("time_hhmm"),
                service_name=chosen.get("service_name"),
                service_key=chosen.get("service_key") or chosen.get("service_canonical"),
                age=(chosen.get("metadata") or {}).get("age") or chosen.get("age"),
                latest_finish_hhmm=None,
            )
            session.draft = merge_draft(session.draft, patch)
            set_pending(session, "confirm_cancel", [])
            reply_with_event(phone, session, cancel_confirm_sys_event(chosen), text)
            return True

        reply_async(phone, safe_reply_text(ai.reply_text, session, text))
        return True

    return False


def handle_cancel_entry(phone: str, session, ai, text: str) -> bool:
    pending_type = session.pending.type if session.pending else "none"
    if ai.intent == "cancel" and pending_type == "none":
        return start_cancel_flow(phone, session, text)
    return False


def handle_cancel_action(phone: str, session, ai, text: str) -> bool:
    if not ai.action or ai.action.type != "cancel_booking":
        return False

    bid = int(session.last_booking_id or getattr(ai.action, "booking_id", 0) or 0)
    if not bid:
        reset_session_after_success(session)
        reply_with_event(phone, session, "SISTEMA_CANCEL_ERROR: missing booking_id", text)
        return True

    result = cancel_booking(bid)
    if result.get("ok"):
        enter_soft_post_success_context(session, "cancel")
        reply_with_event(phone, session, "SISTEMA_CANCEL_OK", text)
    else:
        reset_session_after_success(session)
        reply_with_event(phone, session, f"SISTEMA_CANCEL_ERROR: {result.get('error')}", text)
    return True