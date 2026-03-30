from __future__ import annotations

import time

from app.ai.dialogue import respond
from app.core.config import settings
from app.core.session_store import load_session, save_session
from app.flows.common import apply_ai_result, draft_snapshot
from app.flows.double_booking import (
    handle_active_double_booking,
    handle_double_booking_router,
    is_double_booking_active,
)
from app.flows.handlers_booking import handle_booking_main, handle_pending_booking
from app.flows.handlers_cancel import handle_cancel_action, handle_cancel_entry, handle_pending_cancel
from app.flows.handlers_late import handle_late_arrival
from app.flows.handlers_reschedule import handle_pending_reschedule, handle_reschedule_entry
from app.flows.post_success import (
    clear_soft_post_success_context,
    consume_soft_post_success_message,
    soft_post_success_context_expired,
)
from app.flows.replying import reply_async, safe_reply_text


async def process_consolidated_message(phone: str, text: str) -> None:
    session = load_session(phone)

    try:
        if soft_post_success_context_expired(session):
            clear_soft_post_success_context(session)
    except Exception:
        pass

    session.last_user_message_at = time.time()

    try:
        if session.pending and session.pending.type != "none":
            session.followup_sent_at = None
    except Exception:
        pass

    print("\n[DBG START] phone=", phone, "text=", repr(text))
    print("[DBG START] pending=", (session.pending.model_dump() if session.pending else None))
    print("[DBG START] draft=", session.draft.model_dump())
    print("[DBG START] intent=", session.intent)

    # ─── Fast path: subflujo double booking activo ───────────────────────────
    # Si ya hay un doble booking en curso, bypassamos la IA general por completo
    # para evitar que parchee el draft de sesión o interfiera con el subflujo.
    if is_double_booking_active(session):
        print("[DBG ROUTER] double_booking activo → bypasa IA general")
        handle_active_double_booking(phone, session, ai=None, text=text)
        return

    # ─── Flujo normal: llamada a IA general ──────────────────────────────────
    try:
        ai = respond(text, session)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print("[ERR AI]", err)
        save_session(phone, session)
        reply_async(phone, "Estoy con un problema técnico en este momento 😕 ¿Podés intentar de nuevo en 1 minuto?")
        return

    print("[DBG AI] intent=", ai.intent)
    print("[DBG AI] action=", (ai.action.type if ai.action else None))
    print("[DBG AI] draft_patch=", (ai.draft_patch.model_dump() if ai.draft_patch else None))

    prev_draft = draft_snapshot(session.draft)
    apply_ai_result(session, ai)

    if handle_late_arrival(phone, session, ai, text):
        return

    # ─── Detección y entrada al subflujo double booking ──────────────────────
    # Va antes de los handlers de booking simple para que la detección sea limpia.
    if handle_double_booking_router(phone, session, ai, text):
        return

    if handle_pending_cancel(phone, session, ai, text):
        return

    if handle_pending_reschedule(phone, session, ai, text):
        return

    if handle_pending_booking(phone, session, ai, text, prev_draft):
        return

    if handle_cancel_entry(phone, session, ai, text):
        return

    if handle_reschedule_entry(phone, session, ai, text):
        return

    if handle_cancel_action(phone, session, ai, text):
        return

    if handle_booking_main(phone, session, ai, text):
        return

    save_session(phone, session)
    reply_async(phone, safe_reply_text(ai.reply_text, session, text))

    try:
        consume_soft_post_success_message(session)
        if soft_post_success_context_expired(session):
            clear_soft_post_success_context(session)
        save_session(phone, session)
    except Exception:
        pass