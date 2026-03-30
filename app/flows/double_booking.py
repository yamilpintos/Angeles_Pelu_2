from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

from app.actions.double_booking import reserve_double_plan
from app.ai.double_booking import respond_double_booking
from app.core.catalog import format_price
from app.core.config import settings
from app.core.session_store import save_session
from app.flows.pending_helpers import set_pending
from app.flows.replying import reply_async
from app.flows.double_booking_flow import (
    apply_candidate_plans_to_state,
    format_plan_option,
    get_selected_plan,
    has_minimum_double_booking_data,
    missing_double_booking_fields,
    plans_to_pending_options,
)
from app.flows.double_booking_types import (
    DoubleBookingItem,
    DoubleBookingPlan,
    DoubleBookingSession,
    build_initial_double_booking_state,
)

DOUBLE_BOOKING_START_TEXT = (
    "Perfecto 😊 te ayudo con el turno doble.\n\n"
    "Pasame idealmente en un solo mensaje:\n"
    "1) nombre y edad de cada persona\n"
    "2) qué servicio quiere cada una\n"
    "3) si prefieren ir a la misma hora o uno atrás del otro\n"
    "4) día y horario aproximado\n"
    "5) si quieren algún peluquero en particular o cualquiera"
)

DOUBLE_BOOKING_EXIT_TEXT = (
    "Perfecto, salimos del flujo de turno doble 😊 "
    "Seguimos con la atención normal."
)


def _normalize_text(value: str) -> str:
    raw = str(value or "").strip().lower()
    normalized = unicodedata.normalize("NFD", raw)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _get_raw_double_booking_state(session) -> Any:
    try:
        raw = getattr(session, "double_booking", None)
    except Exception:
        raw = None

    if raw is not None:
        return raw

    try:
        return session.__dict__.get("double_booking")
    except Exception:
        return None


def get_double_booking_state(session) -> DoubleBookingSession | None:
    raw = _get_raw_double_booking_state(session)
    if raw is None:
        return None

    if isinstance(raw, DoubleBookingSession):
        return raw

    if isinstance(raw, dict):
        try:
            return DoubleBookingSession.model_validate(raw)
        except Exception:
            return None

    try:
        return DoubleBookingSession.model_validate(raw)
    except Exception:
        return None


def _set_double_booking_state(session, state: DoubleBookingSession | dict | None) -> None:
    if state is None:
        try:
            session.__dict__.pop("double_booking", None)
        except Exception:
            pass
        return

    if isinstance(state, DoubleBookingSession):
        payload = state.model_dump()
    elif isinstance(state, dict):
        payload = state
    else:
        payload = DoubleBookingSession.model_validate(state).model_dump()

    session.__dict__["double_booking"] = payload


def clear_double_booking_state(session) -> None:
    _set_double_booking_state(session, None)


def is_double_booking_active(session) -> bool:
    state = get_double_booking_state(session)
    return bool(state and state.active and state.stage not in {"completed", "cancelled"})


def _looks_like_double_booking(text: str, ai=None, session=None) -> bool:
    low = _normalize_text(text)

    booking_hints = (
        "turno",
        "turnos",
        "reservar",
        "reserva",
        "agendar",
        "agenda",
        "corte",
        "cortes",
        "peluquero",
        "peluqueros",
    )

    double_hints = (
        "turno doble",
        "dos turnos",
        "2 turnos",
        "somos dos",
        "los dos",
        "queremos ir juntos",
        "queremos ir los dos",
        "uno para mi y otro para",
        "uno para mi hijo y otro para mi",
        "uno para mi hija y otro para mi",
        "dos cortes",
        "dos personas",
        "dos servicios",
        "mi hijo y yo",
        "mi hija y yo",
        "mi amigo y yo",
        "mi pareja y yo",
    )

    if any(hint in low for hint in double_hints):
        has_booking_context = (
            any(hint in low for hint in booking_hints)
            or str(getattr(ai, "intent", "") or "").strip().lower() == "book"
            or str(getattr(session, "intent", "") or "").strip().lower() == "book"
        )
        if has_booking_context:
            return True

    patterns = [
        r"\buno\s+para\s+mi\b.*\botro\s+para\b",
        r"\bpara\s+mi\b.*\by\b.*\bpara\s+mi\s+(hijo|hija|amigo|amiga|pareja)\b",
        r"\bmi\s+(hijo|hija|amigo|amiga|pareja)\s+y\s+yo\b",
        r"\bdos\s+(turnos|cortes|servicios|personas)\b",
    ]

    has_pattern = any(re.search(pattern, low) for pattern in patterns)
    if not has_pattern:
        return False

    has_booking_context = (
        any(hint in low for hint in booking_hints)
        or str(getattr(ai, "intent", "") or "").strip().lower() == "book"
        or str(getattr(session, "intent", "") or "").strip().lower() == "book"
    )
    return has_booking_context


def should_enter_double_booking(ai, session, text: str) -> bool:
    if is_double_booking_active(session):
        return False

    action_type = str(getattr(getattr(ai, "action", None), "type", "none") or "none").strip().lower()
    if action_type == "route_double_booking":
        return True

    return _looks_like_double_booking(text, ai=ai, session=session)


def _wants_to_exit_double_booking(text: str) -> bool:
    low = _normalize_text(text)

    exit_phrases = (
        "mejor no",
        "dejalo",
        "dejalo asi",
        "deja nomas",
        "olvidate",
        "cancelalo",
        "cancelar eso",
        "quiero un turno normal",
        "mejor uno solo",
        "solo un turno",
        "ya no quiero dos",
    )
    return any(phrase in low for phrase in exit_phrases)


def start_double_booking_flow(phone: str, session, text: str, ai=None) -> bool:
    state = build_initial_double_booking_state(entry_text=text)
    _set_double_booking_state(session, state)

    try:
        session.intent = "book"
    except Exception:
        pass

    try:
        set_pending(session, "collect_double_booking_data", [])
    except Exception:
        pass

    save_session(phone, session)
    reply_async(phone, DOUBLE_BOOKING_START_TEXT)
    return True


def _safe_int(value, default=None):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _merge_item_patch(item: DoubleBookingItem, patch) -> DoubleBookingItem:
    updated = item.model_copy(deep=True)

    for field in ("person_label", "customer_name", "service_name", "service_key", "barber"):
        value = getattr(patch, field, None)
        if value is not None and str(value).strip() != "":
            setattr(updated, field, value)

    age = getattr(patch, "age", None)
    if age is not None:
        updated.age = _safe_int(age, default=updated.age)

    return updated


def _apply_ai_patch_to_state(state: DoubleBookingSession, ai_reply) -> DoubleBookingSession:
    patch = getattr(ai_reply, "draft_patch", None)
    if patch is None:
        return state

    updated = state.model_copy(deep=True)

    for field in ("holder_name", "day_text", "preferred_time_hhmm", "mode_preference"):
        value = getattr(patch, field, None)
        if value is not None and str(value).strip() != "":
            setattr(updated, field, value)

    items = list(updated.items or [])
    while len(items) < 2:
        items.append(DoubleBookingItem(slot_id="A" if len(items) == 0 else "B"))

    item_a_patch = getattr(patch, "item_a", None)
    if item_a_patch is not None:
        items[0] = _merge_item_patch(items[0], item_a_patch)

    item_b_patch = getattr(patch, "item_b", None)
    if item_b_patch is not None:
        items[1] = _merge_item_patch(items[1], item_b_patch)

    updated.items = items
    return updated


def _render_plan_list(plans: list[DoubleBookingPlan]) -> str:
    lines: list[str] = []
    for idx, plan in enumerate(plans, start=1):
        label = format_plan_option(plan)
        lines.append(f"{idx}. {label}")
    return "\n".join(lines)


def _render_missing_identity_fields(state: DoubleBookingSession) -> list[str]:
    missing: list[str] = []
    items = list(state.items or [])

    if len(items) >= 1:
        if not (items[0].customer_name or "").strip():
            missing.append("nombre de la primera persona")
        if items[0].age is None:
            missing.append("edad de la primera persona")

    if len(items) >= 2:
        if not (items[1].customer_name or "").strip():
            missing.append("nombre de la segunda persona")
        if items[1].age is None:
            missing.append("edad de la segunda persona")

    return missing


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

    idx = int(match.group(1)) - 1
    if idx < 0 or idx >= len(options):
        return None

    return options[idx]


def _resolve_selected_plan_from_ai_or_text(
    session,
    state: DoubleBookingSession,
    ai_reply,
    text: str,
) -> DoubleBookingPlan | None:
    pending_resolution = getattr(ai_reply, "pending_resolution", None)
    plan_id = str(getattr(pending_resolution, "plan_id", "") or "").strip()

    if not plan_id:
        action = getattr(ai_reply, "action", None)
        plan_id = str(getattr(action, "plan_id", "") or "").strip()

    if plan_id:
        return get_selected_plan(state, plan_id)

    chosen = _resolve_numeric_pending_choice(session, text)
    if chosen and isinstance(chosen, dict):
        chosen_plan_id = str(chosen.get("plan_id") or "").strip()
        if chosen_plan_id:
            return get_selected_plan(state, chosen_plan_id)

    return None


def _build_confirm_text(plan: DoubleBookingPlan) -> str:
    label = format_plan_option(plan)
    return (
        f"{label}\n\n"
        "Si querés, te lo dejo reservado así. "
        "Respondeme sí para confirmar o no para cambiar la opción."
    )


def _result_booking_label(result_item: dict) -> str:
    person_ref = str(result_item.get("person_ref") or result_item.get("customer_name") or "").strip()
    service_name = str(result_item.get("service_name") or "").strip()
    barber = str(result_item.get("barber") or "").strip()
    time_hhmm = str(result_item.get("time_hhmm") or "").strip()
    day_text = str(result_item.get("day_text") or "").strip()
    price = result_item.get("price")
    formatted_price = result_item.get("formatted_price")

    base = f"{person_ref} - {day_text} a las {time_hhmm} con {barber}"
    if service_name:
        base += f" ({service_name})"

    if formatted_price:
        base += f" - {formatted_price}"
    elif price is not None:
        try:
            base += f" - {format_price(int(price))}"
        except Exception:
            pass

    return base


def _build_reserve_ok_text(state: DoubleBookingSession, result) -> str:
    bookings = list(getattr(result, "bookings", None) or [])
    if not bookings and isinstance(result, dict):
        bookings = list(result.get("bookings") or [])

    bundle_id = getattr(result, "bundle_id", None)
    if bundle_id is None and isinstance(result, dict):
        bundle_id = result.get("bundle_id")

    lines = ["Perfecto 😊 ya quedó reservado el turno doble:"]
    for item in bookings:
        if isinstance(item, dict):
            lines.append(f"- {_result_booking_label(item)}")

    if bundle_id:
        lines.append("")
        lines.append(f"Referencia interna del combo: {bundle_id}")

    return "\n".join(lines)


def _build_reserve_error_text(result) -> str:
    error = getattr(result, "error", None)
    if error is None and isinstance(result, dict):
        error = result.get("error")

    reason = getattr(result, "reason", None)
    if reason is None and isinstance(result, dict):
        reason = result.get("reason")

    error_txt = str(error or "").strip()
    if error_txt:
        return error_txt

    if reason == "slot_taken":
        return "Justo una de esas opciones se ocupó recién 😕 Si querés, te busco nuevas combinaciones."

    return "No pude reservar ese turno doble. Probemos con otra opción."


def _offer_plan_selection(phone: str, session, state: DoubleBookingSession) -> bool:
    if not state.offered_plans:
        set_pending(session, "collect_double_booking_data", [])
        save_session(phone, session)
        reply_async(
            phone,
            "No encontré una combinación disponible con esos datos 😕 "
            "Si querés, probemos otro horario, otro día o la modalidad en serie/en paralelo.",
        )
        return True

    set_pending(session, "choose_double_plan", plans_to_pending_options(state.offered_plans))
    save_session(phone, session)
    reply_async(
        phone,
        "Encontré estas opciones para el turno doble:\n\n"
        f"{_render_plan_list(list(state.offered_plans or []))}\n\n"
        "Decime el número de la opción que prefieras.",
    )
    return True


def _maybe_build_plans(phone: str, session, state: DoubleBookingSession) -> bool:
    if not has_minimum_double_booking_data(state):
        return False

    updated = apply_candidate_plans_to_state(
        state,
        all_barbers=list(getattr(settings, "BARBERS", []) or []),
        max_plans=5,
    )
    _set_double_booking_state(session, updated)
    return _offer_plan_selection(phone, session, updated)


def _handle_choose_plan(phone: str, session, state: DoubleBookingSession, ai_reply, text: str) -> bool:
    selected = _resolve_selected_plan_from_ai_or_text(session, state, ai_reply, text)
    if not selected:
        save_session(phone, session)
        reply_async(phone, getattr(ai_reply, "reply_text", "") or "Decime qué opción querés elegir.")
        return True

    updated = state.model_copy(deep=True)
    updated.selected_plan_id = selected.plan_id
    updated.stage = "confirming"
    _set_double_booking_state(session, updated)

    missing_identity = _render_missing_identity_fields(updated)
    if missing_identity:
        set_pending(session, "collect_double_booking_data", [])
        save_session(phone, session)
        reply_async(
            phone,
            "Antes de reservarlo me faltan estos datos:\n- " + "\n- ".join(missing_identity),
        )
        return True

    set_pending(session, "confirm_double_booking", [])
    save_session(phone, session)
    reply_async(phone, _build_confirm_text(selected))
    return True


def _handle_confirm_reservation(phone: str, session, state: DoubleBookingSession, ai_reply, text: str) -> bool:
    confirmation_state = str(getattr(ai_reply, "confirmation_state", "none") or "none").strip().lower()
    action_type = str(getattr(getattr(ai_reply, "action", None), "type", "none") or "none").strip().lower()

    if confirmation_state == "reject":
        updated = state.model_copy(deep=True)
        updated.stage = "choose_plan"
        updated.selected_plan_id = None
        _set_double_booking_state(session, updated)
        return _offer_plan_selection(phone, session, updated)

    if confirmation_state != "confirm" and action_type != "confirm_double_booking":
        save_session(phone, session)
        reply_async(
            phone,
            getattr(ai_reply, "reply_text", "")
            or "Si querés reservarlo así, respondeme sí. Si no, elegimos otra opción.",
        )
        return True

    selected = get_selected_plan(state, state.selected_plan_id or "")
    if not selected:
        updated = state.model_copy(deep=True)
        updated.stage = "choose_plan"
        _set_double_booking_state(session, updated)
        return _offer_plan_selection(phone, session, updated)

    result = reserve_double_plan(
        phone=phone,
        provider="meta",
        state=state,
        plan=selected,
    )

    ok = bool(getattr(result, "ok", None))
    if not ok and isinstance(result, dict):
        ok = bool(result.get("ok"))

    if ok:
        updated = state.model_copy(deep=True)
        updated.active = False
        updated.stage = "completed"
        updated.created_bundle_id = getattr(result, "bundle_id", None) or (
            result.get("bundle_id") if isinstance(result, dict) else None
        )
        _set_double_booking_state(session, updated)
        set_pending(session, "none", [])
        save_session(phone, session)
        reply_async(phone, _build_reserve_ok_text(updated, result))
        return True

    updated = state.model_copy(deep=True)
    updated.stage = "choose_plan"
    updated.selected_plan_id = None
    _set_double_booking_state(session, updated)
    set_pending(session, "choose_double_plan", plans_to_pending_options(updated.offered_plans))
    save_session(phone, session)
    reply_async(phone, _build_reserve_error_text(result))
    return True


def handle_active_double_booking(phone: str, session, ai, text: str) -> bool:
    state = get_double_booking_state(session)
    if not state or not state.active:
        return False

    if _wants_to_exit_double_booking(text):
        state.active = False
        state.stage = "cancelled"
        _set_double_booking_state(session, state)
        try:
            set_pending(session, "none", [])
        except Exception:
            pass
        save_session(phone, session)
        reply_async(phone, DOUBLE_BOOKING_EXIT_TEXT)
        return True

    ai_reply = respond_double_booking(text, session)
    action_type = str(getattr(getattr(ai_reply, "action", None), "type", "none") or "none").strip().lower()

    if action_type == "exit_double_booking":
        state.active = False
        state.stage = "cancelled"
        _set_double_booking_state(session, state)
        set_pending(session, "none", [])
        save_session(phone, session)
        reply_async(phone, getattr(ai_reply, "reply_text", "") or DOUBLE_BOOKING_EXIT_TEXT)
        return True

    if action_type == "fallback_to_general":
        state.active = False
        state.stage = "cancelled"
        _set_double_booking_state(session, state)
        set_pending(session, "none", [])
        save_session(phone, session)
        reply_async(
            phone,
            getattr(ai_reply, "reply_text", "") or "Perfecto, seguimos con tu otra consulta.",
        )
        return True

    updated = _apply_ai_patch_to_state(state, ai_reply)
    pending_type = session.pending.type if session.pending else "none"

    if pending_type == "choose_double_plan" or action_type == "choose_plan":
        _set_double_booking_state(session, updated)
        return _handle_choose_plan(phone, session, updated, ai_reply, text)

    if pending_type == "confirm_double_booking":
        _set_double_booking_state(session, updated)
        return _handle_confirm_reservation(phone, session, updated, ai_reply, text)

    if has_minimum_double_booking_data(updated):
        _set_double_booking_state(session, updated)
        return _maybe_build_plans(phone, session, updated)

    updated.stage = "collecting"
    _set_double_booking_state(session, updated)
    set_pending(session, "collect_double_booking_data", [])
    save_session(phone, session)

    missing = missing_double_booking_fields(updated)
    if missing and not (getattr(ai_reply, "reply_text", "") or "").strip():
        reply_async(
            phone,
            "Para seguir con el turno doble me falta:\n- " + "\n- ".join(missing),
        )
        return True

    reply_async(
        phone,
        getattr(ai_reply, "reply_text", "") or "Contame un poco más y te ayudo a armar ese turno doble 😊",
    )
    return True


def handle_double_booking_router(phone: str, session, ai, text: str) -> bool:
    if is_double_booking_active(session):
        return handle_active_double_booking(phone, session, ai, text)

    if should_enter_double_booking(ai, session, text):
        return start_double_booking_flow(phone, session, text, ai=ai)

    return False