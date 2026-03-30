from __future__ import annotations

from datetime import datetime
import re
import unicodedata
from zoneinfo import ZoneInfo

from app.core.catalog import services_human_text
from app.core.config import settings
from app.core.types import Session


print("[DBG IMPORT context.py]", __file__)


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
        "- Horarios reservables para clientes: de 12:00 a 19:30.\n"
        "- El local trabaja hasta las 20:00, pero el último turno reservable para clientes es 19:30.\n"
        "- 20:00 y 20:30 están bloqueados y no deben ofrecerse.\n"
        "- No existen turnos por la mañana.\n"
        "Instrucción: convertí referencias relativas como 'hoy', 'mañana' o 'pasado mañana' "
        "a un day_text explícito tipo 'jueves 12'.\n"
        "Instrucción: si el cliente menciona un mes explícito, conservalo dentro de day_text.\n"
        "Instrucción: convertí horarios escritos como '15', '15hs', '15 h', '15.00', '930' o '1530' "
        "a formato HH:MM.\n"
        "Instrucción: aunque normalices una hora, no la trates como reservable si queda fuera de 12:00 a 19:30.\n"
        "Instrucción: para pedidos del mismo día ('ahora', 'hoy', 'esta tarde', 'más tarde'), usá solo horarios útiles desde al menos 60 minutos después de la hora actual.\n"
        "Instrucción: si el cliente pide algo del mismo día y el horario útil de hoy ya terminó, orientá la respuesta al día siguiente.\n"
        "Instrucción: si el cliente usa contexto vespertino ('a la tarde', 'vespertino', 'después del mediodía'), una hora ambigua como '5' o '7:30' debe entenderse en PM si corresponde.\n"
    )


def _session_context(session: Session) -> str:
    raw_draft = session.draft.model_dump()
    draft = {k: v for k, v in raw_draft.items() if v not in (None, "", [], {})}

    raw_pending = (
        session.pending.model_dump()
        if session.pending
        else {"type": "none", "options": []}
    )
    pending = {k: v for k, v in raw_pending.items() if v not in (None, "", [], {})}

    if "type" not in pending:
        pending["type"] = "none"
    if "options" not in pending:
        pending["options"] = []

    last_id = getattr(session, "last_booking_id", None)

    return (
        "Contexto del chat:\n"
        f"- Intent actual: {session.intent}\n"
        f"- last_booking_id: {last_id}\n"
        f"- Draft: {draft}\n"
        f"- Pending: {pending}\n"
        "Instrucción: si ya hay datos guardados, no los vuelvas a pedir.\n"
        "Catálogo disponible (humano):\n"
        f"{services_human_text()}\n"
    )