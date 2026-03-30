from __future__ import annotations

from datetime import datetime
import re
import unicodedata
from zoneinfo import ZoneInfo

from app.core.catalog import services_human_text
from app.core.config import settings
from app.core.types import Session
from app.flows.double_booking_types import DoubleBookingSession


print("[DBG IMPORT double_booking/context.py]", __file__)


def _norm_text(s: str) -> str:
    s = str(s or "").replace("\u00a0", " ").strip().lower()
    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _current_now() -> datetime:
    tz_name = getattr(settings, "TIMEZONE", "America/Argentina/Buenos_Aires")
    return datetime.now(ZoneInfo(tz_name))


def _now_context() -> str:
    tz_name = getattr(settings, "TIMEZONE", "America/Argentina/Buenos_Aires")
    now = datetime.now(ZoneInfo(tz_name))

    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]

    fecha_corta = f"{dias[now.weekday()]} {now.day}"
    fecha_larga = f"{dias[now.weekday()]} {now.day} de {meses[now.month - 1]} de {now.year}"
    hora_actual = now.strftime("%H:%M")

    return (
        "Contexto temporal real:\n"
        f"- Zona horaria: {tz_name}\n"
        f"- Fecha actual: {fecha_larga}\n"
        f"- Día corto actual: {fecha_corta}\n"
        f"- Hora actual: {hora_actual}\n"
        "- Horarios reservables para clientes: 12:00 a 19:30.\n"
        "- Los turnos se manejan en grilla de 30 minutos.\n"
        "- Convertí referencias relativas como 'hoy', 'mañana' o 'pasado mañana' a un day_text explícito.\n"
        "- Convertí horarios escritos como '15', '15hs', '15.00', '930' o '1530' a HH:MM.\n"
        "- Aunque normalices una hora, no la trates como válida si cae fuera del rango 12:00 a 19:30.\n"
    )


def _get_raw_double_booking_state(session: Session):
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


def _double_booking_state(session: Session) -> DoubleBookingSession | None:
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


def _compact_dict(value: dict) -> dict:
    return {k: v for k, v in value.items() if v not in (None, "", [], {})}


def _double_booking_session_context(session: Session) -> str:
    state = _double_booking_state(session)

    if not state:
        return (
            "Estado del doble booking:\n"
            "- No hay estado activo de double_booking.\n"
        )

    state_payload = _compact_dict(state.model_dump())

    return (
        "Estado actual del subflujo double_booking:\n"
        f"{state_payload}\n"
        "Instrucción: no vuelvas a pedir los datos que ya están presentes.\n"
        "Instrucción: si solo falta una parte de la segunda persona, pedí solo eso.\n"
        "Instrucción: si ya están los datos mínimos, marcá action.type = 'build_candidate_plans'.\n"
    )


def _general_session_context(session: Session) -> str:
    raw_draft = session.draft.model_dump()
    draft = _compact_dict(raw_draft)

    raw_pending = (
        session.pending.model_dump()
        if session.pending
        else {"type": "none", "options": []}
    )
    pending = _compact_dict(raw_pending)

    if "type" not in pending:
        pending["type"] = "none"
    if "options" not in pending:
        pending["options"] = []

    last_id = getattr(session, "last_booking_id", None)

    return (
        "Contexto general del chat:\n"
        f"- Intent actual: {session.intent}\n"
        f"- last_booking_id: {last_id}\n"
        f"- Draft general: {draft}\n"
        f"- Pending general: {pending}\n"
        "Instrucción: este contexto es accesorio; el foco es el subflujo de turno doble.\n"
    )


def _catalog_context() -> str:
    return (
        "Catálogo humano disponible:\n"
        f"{services_human_text()}\n"
    )


def build_double_booking_prompt(user_text: str, session: Session) -> str:
    return (
        _now_context()
        + "\n"
        + _general_session_context(session)
        + "\n"
        + _double_booking_session_context(session)
        + "\n"
        + _catalog_context()
        + "\n"
        + f"Mensaje del cliente: {user_text}"
    )