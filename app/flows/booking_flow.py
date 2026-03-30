from __future__ import annotations

from app.actions.booking import (
    _slot_allowed,
    find_offers,
    recheck_slot_live,
    service_blocks,
)
from app.core.utils import merge_draft
from app.flows.common import (
    allowed_barbers_for_session,
    draft_snapshot,
    maybe_resolve_any_barber,
    norm,
    service_barber_error_for_chat,
)
from app.flows.day_context import (
    build_day_context_payload,
    day_availability_sys_event,
    pending_choose_slot_options,
)
from app.flows.pending_helpers import set_pending
from app.flows.replying import reply_with_event


def draft_has_all(session) -> bool:
    required = ["customer_name", "age", "barber", "day_text", "time_hhmm", "service_key"]
    for k in required:
        v = getattr(session.draft, k, None)
        if v is None:
            return False
        if isinstance(v, str) and v.strip() == "":
            return False
    return True


def missing_booking_fields(session) -> list[str]:
    missing: list[str] = []
    if not getattr(session.draft, "customer_name", None):
        missing.append("nombre y apellido")
    if getattr(session.draft, "age", None) is None:
        missing.append("edad")
    if not getattr(session.draft, "service_key", None):
        missing.append("servicio")
    return missing


def missing_fields_sys_event(session) -> str:
    missing = missing_booking_fields(session)
    payload = {
        "missing_fields": missing,
        "service_name": getattr(session.draft, "service_name", None),
        "service_key": getattr(session.draft, "service_key", None),
        "allowed_barbers": allowed_barbers_for_session(session),
        "draft": draft_snapshot(session.draft),
    }
    return f"SISTEMA_MISSING_BOOKING_FIELDS: {payload}"


def validation_error_sys_event(reason: str, session=None) -> str:
    payload = {
        "reason": str(reason or "").strip(),
        "service_name": getattr(getattr(session, "draft", None), "service_name", None) if session is not None else None,
        "service_key": getattr(getattr(session, "draft", None), "service_key", None) if session is not None else None,
        "barber": getattr(getattr(session, "draft", None), "barber", None) if session is not None else None,
        "day_text": getattr(getattr(session, "draft", None), "day_text", None) if session is not None else None,
        "time_hhmm": getattr(getattr(session, "draft", None), "time_hhmm", None) if session is not None else None,
    }
    return f"SISTEMA_VALIDATION_ERROR: {payload}"


def offers_sys_event(
    offers_result,
    session=None,
    *,
    event_name: str = "SISTEMA_OFFERS",
    all_barbers: list[str] | None = None,
) -> str:
    offers = getattr(offers_result, "offers", []) or []
    reason = getattr(offers_result, "reason", "") or ""
    requested_barber = getattr(offers_result, "requested_barber", None)
    requested_day = getattr(offers_result, "requested_day", None)
    next_same = getattr(offers_result, "next_same_barber_offers", None) or []

    service_name = None
    service_key = None
    allowed_barbers = []
    day_context = {}

    if session is not None:
        service_name = getattr(session.draft, "service_name", None)
        service_key = getattr(session.draft, "service_key", None)
        allowed_barbers = allowed_barbers_for_session(session)
        day_context = build_day_context_payload(
            session,
            requested_day=requested_day,
            requested_barber=requested_barber,
            force_refresh=True,
            all_barbers=all_barbers,
        )

    selectable_slots = day_context.get("selectable_slots", []) if isinstance(day_context, dict) else []

    payload = {
        "reason": reason,
        "requested_barber": requested_barber,
        "requested_day": requested_day,
        "requested_time": getattr(session.draft, "time_hhmm", None) if session is not None else None,
        "offers_preview": offers,
        "offers": offers,
        "selectable_slots": selectable_slots,
        "day_context": day_context,
        "next_same_barber_offers": next_same,
        "service_name": service_name,
        "service_key": service_key,
        "allowed_barbers": allowed_barbers,
    }
    return f"{event_name}: {payload}"


def safe_service_blocks(draft) -> int:
    try:
        blocks = int(service_blocks(draft) or 1)
    except Exception:
        blocks = 1
    return max(1, blocks)


def offer_to_draft_patch(session, payload: dict):
    meta = payload.get("metadata") if isinstance(payload, dict) else {}
    if not isinstance(meta, dict):
        meta = {}

    age = meta.get("age")
    if age is None and isinstance(payload, dict):
        age = payload.get("age")

    return session.draft.__class__(
        customer_name=(payload.get("customer_name") if isinstance(payload, dict) else None),
        barber=(payload.get("barber") if isinstance(payload, dict) else None),
        day_text=((payload.get("day_text") or payload.get("date_text")) if isinstance(payload, dict) else None),
        time_hhmm=(payload.get("time_hhmm") if isinstance(payload, dict) else None),
        service_name=(payload.get("service_name") if isinstance(payload, dict) else None),
        service_key=((payload.get("service_key") or payload.get("service_canonical")) if isinstance(payload, dict) else None),
        age=age,
    )


def validate_requested_slot_for_chat(session) -> str | None:
    try:
        if not draft_has_all(session):
            return None

        barber_service_error = service_barber_error_for_chat(session)
        if barber_service_error:
            return barber_service_error

        blocks = safe_service_blocks(session.draft)
        ok, reason = _slot_allowed(session.draft.day_text, session.draft.time_hhmm, blocks)
        if not ok:
            return reason or "Ese horario no se puede otorgar."

        return None
    except Exception as e:
        print("[ERR VALIDATE REQUESTED SLOT]", type(e).__name__, str(e))
        return "Estoy teniendo un problema para validar ese horario. Probá de nuevo en un momento."


def validate_min_slot_for_chat(session) -> str | None:
    try:
        barber = (session.draft.barber or "").strip()
        day_text = (session.draft.day_text or "").strip()
        time_hhmm = (session.draft.time_hhmm or "").strip()
        service_key = (session.draft.service_key or "").strip()

        if not barber or not day_text or not time_hhmm:
            return None

        if not service_key:
            return None

        barber_service_error = service_barber_error_for_chat(session)
        if barber_service_error:
            return barber_service_error

        blocks = safe_service_blocks(session.draft)
        ok, reason = _slot_allowed(day_text, time_hhmm, blocks)
        if not ok:
            return reason or "Ese horario no se puede otorgar."

        return None
    except Exception as e:
        print("[ERR VALIDATE MIN SLOT]", type(e).__name__, str(e))
        return "Estoy teniendo un problema para validar ese horario. Probá de nuevo en un momento."


def should_release_choose_slot_for_new_query(prev_draft: dict, session, ai) -> bool:
    if not session.pending or session.pending.type != "choose_slot":
        return False

    action_type = ai.action.type if ai.action else "none"

    if action_type == "resolve_pending_choice":
        return False

    if action_type in {"find_offers", "check_day_availability"}:
        return True

    relevant_fields = (
        "day_text",
        "time_hhmm",
        "barber",
        "service_key",
        "latest_finish_hhmm",
    )

    for field in relevant_fields:
        prev_val = norm(prev_draft.get(field))
        curr_val = norm(getattr(session.draft, field, None))
        if curr_val and curr_val != prev_val:
            return True

    return False


def prompt_missing_fields(phone: str, session, text: str) -> None:
    set_pending(session, "collect_booking_data", [])
    reply_with_event(phone, session, missing_fields_sys_event(session), text)


def handle_booking_progress(phone: str, session, ai, text: str, *, all_barbers: list[str]) -> bool:
    if session.intent != "book":
        return False

    action_type = ai.action.type if ai.action else "none"

    maybe_resolve_any_barber(session)

    barber = (session.draft.barber or "").strip()
    day_text = (session.draft.day_text or "").strip()
    time_hhmm = (session.draft.time_hhmm or "").strip()
    service_key = (session.draft.service_key or "").strip()

    has_barber = bool(barber)
    has_day = bool(day_text)
    has_time = bool(time_hhmm)
    has_service = bool(service_key)

    if not has_day:
        return False

    if has_barber and has_day and has_time and not has_service:
        prompt_missing_fields(phone, session, text)
        return True

    if has_barber and has_day and has_time and has_service:
        invalid_reason = validate_min_slot_for_chat(session)
        if invalid_reason:
            set_pending(session, "collect_booking_data", [])
            reply_with_event(phone, session, validation_error_sys_event(invalid_reason, session), text)
            return True

        blocks = safe_service_blocks(session.draft)
        ok = recheck_slot_live(session.draft, time_hhmm=session.draft.time_hhmm, blocks=blocks)
        if ok:
            missing = missing_booking_fields(session)
            if missing:
                prompt_missing_fields(phone, session, text)
                return True

            set_pending(session, "confirm_booking", [])
            reply_with_event(phone, session, "SISTEMA_CONFIRM_BOOKING", text)
            return True

        offers_result = find_offers(session.draft, blocks=blocks, barbers=all_barbers, max_offers=3)
        offers = offers_result.offers or []
        if offers:
            set_pending(session, "choose_slot", pending_choose_slot_options(offers_result, session, all_barbers=all_barbers))
            reply_with_event(
                phone,
                session,
                offers_sys_event(
                    offers_result,
                    session,
                    event_name="SISTEMA_EARLY_SLOT_UNAVAILABLE_OFFERS",
                    all_barbers=all_barbers,
                ),
                text,
            )
            return True

        set_pending(session, "none", [])
        reply_with_event(
            phone,
            session,
            f"SISTEMA_SLOT_UNAVAILABLE_NO_OFFERS: {draft_snapshot(session.draft)}",
            text,
        )
        return True

    if has_day and has_service and (action_type == "find_offers" or has_barber or has_time):
        if has_time and has_barber:
            invalid_reason = validate_min_slot_for_chat(session)
            if invalid_reason:
                set_pending(session, "collect_booking_data", [])
                reply_with_event(phone, session, validation_error_sys_event(invalid_reason, session), text)
                return True

        blocks = safe_service_blocks(session.draft)
        offers_result = find_offers(session.draft, blocks=blocks, barbers=all_barbers, max_offers=3)
        offers = offers_result.offers or []

        if offers:
            set_pending(session, "choose_slot", pending_choose_slot_options(offers_result, session, all_barbers=all_barbers))
            reply_with_event(phone, session, offers_sys_event(offers_result, session, all_barbers=all_barbers), text)
            return True

        set_pending(session, "none", [])
        reply_with_event(phone, session, "SISTEMA_OFFERS_EMPTY", text)
        return True

    if has_day and action_type == "check_day_availability" and not has_time:
        day_payload = build_day_context_payload(
            session,
            requested_day=day_text,
            requested_barber=barber or None,
            force_refresh=True,
            all_barbers=all_barbers,
        )

        selectable_slots = day_payload.get("selectable_slots", []) if isinstance(day_payload, dict) else []
        requested_barber = (barber or "").strip()

        if selectable_slots:
            set_pending(session, "choose_slot", selectable_slots)
            reply_with_event(
                phone,
                session,
                day_availability_sys_event(
                    session,
                    requested_day=day_text,
                    requested_barber=requested_barber or None,
                    all_barbers=all_barbers,
                ),
                text,
            )
            return True

        blocks = safe_service_blocks(session.draft)
        offers_result = find_offers(
            session.draft,
            blocks=blocks,
            barbers=all_barbers,
            max_offers=3,
        )

        merged_options = pending_choose_slot_options(offers_result, session, all_barbers=all_barbers)
        if merged_options:
            set_pending(session, "choose_slot", merged_options)
            reply_with_event(
                phone,
                session,
                offers_sys_event(
                    offers_result,
                    session,
                    event_name="SISTEMA_DAY_AVAILABILITY",
                    all_barbers=all_barbers,
                ),
                text,
            )
            return True

        set_pending(session, "none", [])
        reply_with_event(
            phone,
            session,
            day_availability_sys_event(
                session,
                requested_day=day_text,
                requested_barber=requested_barber or None,
                all_barbers=all_barbers,
            ),
            text,
        )
        return True

    return False