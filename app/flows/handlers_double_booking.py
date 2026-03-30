from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.core.session_store import save_session
from app.flows.pending_helpers import set_pending
from app.flows.replying import reply_async
from app.flows.double_booking_types import (
    DoubleBookingSession,
    build_initial_double_booking_state,
)


DOUBLE_BOOKING_START_TEXT = (
    "Perfecto 😊 te ayudo con el turno doble.\n\n"
    "Para armarlo bien, pasame idealmente en un solo mensaje:\n"
    "1) nombre y edad de cada persona\n"
    "2) qué servicio quiere cada una\n"
    "3) si prefieren ir a la misma hora o uno atrás del otro\n"
    "4) día y horario aproximado\n"
    "5) si quieren algún peluquero en particular o cualquiera\n\n"
    "Ejemplo:\n"
    '"Yamil, 43, corte + barba, y mi hijo Tomi, 12, corte niño. '
    'Queremos mañana tipo 18:00, si puede ser juntos y con cualquiera."'
)

DOUBLE_BOOKING_CONTINUE_TEXT = (
    "Dale 😊 sigo con el turno doble.\n\n"
    "Por ahora mandame juntos los datos de las dos personas: "
    "nombre, edad, servicio, día, horario aproximado y si quieren "
    "a la misma hora o uno atrás del otro."
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

    # Nota:
    # usamos __dict__ para que este archivo pueda existir
    # incluso antes de agregar formalmente el campo en Session.
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
        set_pending(session, "none", [])
    except Exception:
        pass

    save_session(phone, session)
    reply_async(phone, DOUBLE_BOOKING_START_TEXT)
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

    # Etapa 1:
    # todavía no entra la IA especializada ni el planner.
    # Solo mantenemos encapsulado el flujo y pedimos la data base.
    save_session(phone, session)
    reply_async(phone, DOUBLE_BOOKING_CONTINUE_TEXT)
    return True


def handle_double_booking_router(phone: str, session, ai, text: str) -> bool:
    if is_double_booking_active(session):
        return handle_active_double_booking(phone, session, ai, text)

    if should_enter_double_booking(ai, session, text):
        return start_double_booking_flow(phone, session, text, ai=ai)

    return False