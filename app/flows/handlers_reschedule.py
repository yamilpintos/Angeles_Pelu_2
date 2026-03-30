from __future__ import annotations

import re

from app.actions.booking import rgb_from_draft
from app.actions.reschedule import reschedule_booking
from app.core.session_store import save_session
from app.flows.booking_flow import safe_service_blocks, validate_requested_slot_for_chat
from app.flows.common import ai_confirmed, ai_rejected
from app.flows.pending_helpers import set_pending
from app.flows.post_success import enter_soft_post_success_context, reset_session_after_success
from app.flows.replying import reply_async, reply_with_event, safe_reply_text
from app.flows.reschedule_flow import build_selected_reschedule_anchor, start_reschedule_flow


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


def _pending_selected_reschedule_anchor(session) -> dict | None:
    options = list(getattr(getattr(session, "pending", None), "options", None) or [])
    for opt in options:
        if isinstance(opt, dict) and opt.get("__selected_booking__"):
            return opt
    return None


def _build_reschedule_anchor_from_session(session) -> dict:
    current = _pending_selected_reschedule_anchor(session)
    if current:
        return dict(current)

    return build_selected_reschedule_anchor(
        {
            "id": session.last_booking_id,
            "customer_name": session.draft.customer_name,
            "barber": session.draft.barber,
            "day_text": session.draft.day_text,
            "date_text": session.draft.day_text,
            "time_hhmm": session.draft.time_hhmm,
            "service_name": session.draft.service_name,
            "service_key": session.draft.service_key,
            "age": session.draft.age,
            "tab": getattr(session.draft, "tab", None),
            "sheet_id": getattr(session.draft, "sheet_id", None),
            "row": getattr(session.draft, "row", None),
            "col": getattr(session.draft, "col", None),
            "blocks": getattr(session.draft, "blocks", None),
        }
    )


def _sync_selected_anchor_with_draft(selected_anchor: dict | None, session) -> dict:
    base = dict(selected_anchor or {})

    base["customer_name"] = session.draft.customer_name or base.get("customer_name")
    base["barber"] = session.draft.barber or base.get("barber")
    base["day_text"] = session.draft.day_text or base.get("day_text")
    base["date_text"] = session.draft.day_text or base.get("date_text")
    base["time_hhmm"] = session.draft.time_hhmm or base.get("time_hhmm")
    base["service_name"] = session.draft.service_name or base.get("service_name")
    base["service_key"] = session.draft.service_key or base.get("service_key")
    base["age"] = session.draft.age or base.get("age")

    if "__selected_booking__" not in base:
        base["__selected_booking__"] = True

    return base


def _extract_explicit_time_hhmm(text: str) -> str | None:
    raw = str(text or "").strip().lower()

    m = re.search(r"\b(1[2-9]|2[0-3])[:\.]([0-5]\d)\b", raw)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        return f"{hh:02d}:{mm:02d}"

    m = re.search(r"\b(?:a\s+las|tipo)\s+(1[2-9]|2[0-3])\b", raw)
    if m:
        hh = int(m.group(1))
        return f"{hh:02d}:00"

    m = re.search(r"\b(1[2-9]|2[0-3])\s*(?:hs|h)\b", raw)
    if m:
        hh = int(m.group(1))
        return f"{hh:02d}:00"

    return None


def _apply_explicit_time_guard(session, text: str, selected_anchor: dict | None) -> None:
    explicit_time = _extract_explicit_time_hhmm(text)
    if not explicit_time:
        return

    session.draft.time_hhmm = explicit_time

    if not (session.draft.day_text or "").strip():
        anchor_day = None
        if isinstance(selected_anchor, dict):
            anchor_day = selected_anchor.get("day_text") or selected_anchor.get("date_text")
        if anchor_day:
            session.draft.day_text = str(anchor_day).strip()

    print(
        "[DBG RESCHEDULE EXPLICIT TIME GUARD]",
        {
            "text": text,
            "applied_time_hhmm": session.draft.time_hhmm,
            "applied_day_text": session.draft.day_text,
        },
    )


def _selected_anchor_day_text(selected_anchor: dict | None) -> str:
    if not isinstance(selected_anchor, dict):
        return ""
    return str(
        selected_anchor.get("day_text")
        or selected_anchor.get("date_text")
        or ""
    ).strip()


def _selected_anchor_time_hhmm(selected_anchor: dict | None) -> str:
    if not isinstance(selected_anchor, dict):
        return ""
    return str(selected_anchor.get("time_hhmm") or "").strip()


def _selected_anchor_barber(selected_anchor: dict | None) -> str:
    if not isinstance(selected_anchor, dict):
        return "el peluquero indicado"
    return str(selected_anchor.get("barber") or "el peluquero indicado").strip()


def _selected_anchor_service(selected_anchor: dict | None) -> str:
    if not isinstance(selected_anchor, dict):
        return "el servicio indicado"
    return str(selected_anchor.get("service_name") or "el servicio indicado").strip()


def _reschedule_intro_reply(selected_anchor: dict | None) -> str:
    barber = _selected_anchor_barber(selected_anchor)
    service = _selected_anchor_service(selected_anchor)
    day_text = _selected_anchor_day_text(selected_anchor) or "el día indicado"
    time_hhmm = _selected_anchor_time_hhmm(selected_anchor) or "el horario indicado"

    return (
        f'Tu turno actual con {barber} para "{service}" es el '
        f"{day_text} a las {time_hhmm}. "
        "Para reprogramarlo, indicame el nuevo día con fecha y el horario. "
        'Por ejemplo: "jueves 9 a las 18:30". '
        f"Si querés mantener el mismo día ({day_text}), podés pasarme solo la nueva hora."
    )


def _reschedule_missing_fields_reply(session, selected_anchor: dict | None) -> str:
    has_day = bool((session.draft.day_text or "").strip())
    has_time = bool((session.draft.time_hhmm or "").strip())

    current_day = str(session.draft.day_text or "").strip()
    current_time = str(session.draft.time_hhmm or "").strip()
    anchor_day = _selected_anchor_day_text(selected_anchor)

    if not has_day and not has_time:
        if anchor_day:
            return (
                "Perfecto. Para reprogramar tu turno necesito que me indiques "
                "el nuevo día con fecha y el horario. "
                'Por ejemplo: "jueves 9 a las 18:30". '
                f"Si querés mantener el mismo día ({anchor_day}), podés pasarme solo la nueva hora."
            )
        return (
            "Perfecto. Para reprogramar tu turno necesito que me indiques "
            "el nuevo día con fecha y el horario. "
            'Por ejemplo: "jueves 9 a las 18:30".'
        )

    if has_day and not has_time:
        return (
            f"Perfecto, ya tengo el nuevo día ({current_day}). "
            "Ahora indicame el horario al que querés mover el turno."
        )

    if not has_day and has_time:
        return (
            f"Perfecto, ya tengo el horario ({current_time}). "
            "Ahora indicame el nuevo día con fecha al que querés mover el turno."
        )

    return "Perfecto. Confirmame si querés avanzar con esa reprogramación."


def _continue_reschedule_with_current_draft(phone: str, session, text: str, selected_anchor: dict | None) -> bool:
    has_day = bool((session.draft.day_text or "").strip())
    has_time = bool((session.draft.time_hhmm or "").strip())

    if has_day and has_time:
        invalid_reason = validate_requested_slot_for_chat(session)
        if invalid_reason:
            from app.flows.booking_flow import validation_error_sys_event

            selected_anchor = _sync_selected_anchor_with_draft(selected_anchor, session)
            set_pending(session, "choose_new_slot", [selected_anchor])
            reply_with_event(phone, session, validation_error_sys_event(invalid_reason, session), text)
            return True

        selected_anchor = _sync_selected_anchor_with_draft(selected_anchor, session)
        set_pending(session, "confirm_reschedule", [selected_anchor])
        reply_with_event(phone, session, "SISTEMA_RESCHEDULE_CONFIRM", text)
        return True

    selected_anchor = _sync_selected_anchor_with_draft(selected_anchor, session)
    set_pending(session, "choose_new_slot", [selected_anchor])
    save_session(phone, session)
    reply_async(phone, _reschedule_missing_fields_reply(session, selected_anchor))
    return True


def handle_pending_reschedule(phone: str, session, ai, text: str) -> bool:
    pending_type = session.pending.type if session.pending else "none"

    if pending_type == "choose_reschedule":
        chosen = _resolve_numeric_pending_choice(session, text)

        if chosen:
            selected_anchor = build_selected_reschedule_anchor(chosen)

            session.intent = "reschedule"
            session.last_booking_id = str(chosen.get("id") or "")
            session.draft = session.draft.__class__(
                customer_name=chosen.get("customer_name"),
                barber=chosen.get("barber"),
                day_text=None,
                time_hhmm=None,
                service_name=chosen.get("service_name"),
                service_key=chosen.get("service_key") or chosen.get("service_canonical"),
                age=(chosen.get("metadata") or {}).get("age") or chosen.get("age"),
                latest_finish_hhmm=None,
            )

            set_pending(session, "choose_new_slot", [selected_anchor])
            save_session(phone, session)

            reply_async(phone, _reschedule_intro_reply(selected_anchor))
            return True

        save_session(phone, session)
        reply_async(phone, safe_reply_text(ai.reply_text, session, text))
        return True

    if pending_type == "choose_new_slot":
        session.intent = "reschedule"
        selected_anchor = _build_reschedule_anchor_from_session(session)

        _apply_explicit_time_guard(session, text, selected_anchor)
        return _continue_reschedule_with_current_draft(phone, session, text, selected_anchor)

    if pending_type == "confirm_reschedule":
        selected_anchor = _build_reschedule_anchor_from_session(session)

        if ai_confirmed(ai):
            blocks = safe_service_blocks(session.draft)
            rgb = rgb_from_draft(session.draft)
            bid = int(session.last_booking_id or "0")

            if not bid:
                reset_session_after_success(session)
                reply_with_event(phone, session, "SISTEMA_RESCHEDULE_ERROR: missing booking_id", text)
                return True

            res = reschedule_booking(
                booking_id=bid,
                draft=session.draft,
                phone=phone,
                provider="meta",
                blocks=blocks,
                rgb=rgb,
            )
            if res.get("ok"):
                enter_soft_post_success_context(session, "reschedule")
                set_pending(session, "none", [])
                session.last_booking_id = ""
                reply_with_event(phone, session, "SISTEMA_RESCHEDULE_OK", text)
            else:
                reset_session_after_success(session)
                reply_with_event(phone, session, f"SISTEMA_RESCHEDULE_ERROR: {res.get('error')}", text)
            return True

        if ai_rejected(ai):
            reset_session_after_success(session)
            reply_with_event(phone, session, "SISTEMA_RESCHEDULE_CANCELLED", text)
            return True

        session.intent = "reschedule"
        _apply_explicit_time_guard(session, text, selected_anchor)
        return _continue_reschedule_with_current_draft(phone, session, text, selected_anchor)

    return False


def handle_reschedule_entry(phone: str, session, ai, text: str) -> bool:
    pending_type = session.pending.type if session.pending else "none"
    if ai.intent == "reschedule" and pending_type == "none":
        return start_reschedule_flow(phone, session, text)
    return False