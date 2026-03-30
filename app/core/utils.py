import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.types import Draft


def merge_draft(old: Draft, patch: Draft, clear_fields: set[str] | None = None) -> Draft:
    od = old.model_dump()
    pd = patch.model_dump()

    clear_fields = clear_fields or set()

    # limpiar explícitamente campos pedidos por el caller
    for field in clear_fields:
        if field in od:
            od[field] = None

    # si cambió el día, la hora exacta vieja ya no sirve
    new_day = pd.get("day_text")
    if new_day not in (None, "", od.get("day_text")):
        od["time_hhmm"] = None

    # si cambió el peluquero, la hora exacta vieja tampoco sirve
    new_barber = pd.get("barber")
    if new_barber not in (None, "", od.get("barber")):
        od["time_hhmm"] = None

    # si cambió el servicio, la hora exacta vieja puede dejar de servir
    new_service_key = pd.get("service_key")
    new_service_name = pd.get("service_name")
    if (
        new_service_key not in (None, "", od.get("service_key"))
        or new_service_name not in (None, "", od.get("service_name"))
    ):
        od["time_hhmm"] = None

    # merge normal
    for k, v in pd.items():
        if v is not None and v != "":
            od[k] = v

    return Draft(**od)


def safe_phone(phone_raw: str) -> str:
    s = (phone_raw or "").strip()

    # si viene "whatsapp:+549..." quedate solo con el número
    s = s.replace("whatsapp:", "").strip()

    # dejá solo dígitos
    digits = re.sub(r"\D+", "", s)

    # si ya viene con 54... lo dejamos así, solo agregamos '+'
    if digits.startswith("54"):
        return f"+{digits}"

    # fallback: si viniera sin país (raro), igual lo devolvemos con '+'
    return f"+{digits}" if digits else ""


_DOWS = r"(lunes|martes|miercoles|miércoles|jueves|viernes|sabado|sábado|domingo)"

_DOW_INDEX = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "miércoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "sábado": 5,
    "domingo": 6,
}

_DOW_CANON = {
    "lunes": "lunes",
    "martes": "martes",
    "miercoles": "miércoles",
    "miércoles": "miércoles",
    "jueves": "jueves",
    "viernes": "viernes",
    "sabado": "sábado",
    "sábado": "sábado",
    "domingo": "domingo",
}


def _now_local() -> datetime:
    tz_name = getattr(settings, "TIMEZONE", "America/Argentina/Buenos_Aires")
    return datetime.now(ZoneInfo(tz_name))


def _next_weekday_date(dow_text: str) -> datetime:
    """
    Devuelve el día de semana más cercano hacia adelante.
    Si hoy ya es ese día, devuelve hoy.
    """
    now = _now_local()
    target_idx = _DOW_INDEX[dow_text]
    delta = (target_idx - now.weekday()) % 7
    return now + timedelta(days=delta)


def extract_day_text(text: str) -> Optional[str]:
    s = (text or "").strip().lower()

    # Día solo o día + número
    # Ej:
    # "martes"
    # "martes 3"
    # "martes 03"
    # "martes 3/3"
    # "el martes 3"
    # "para el jueves"
    m = re.search(rf"\b{_DOWS}\b(?:\D*(\d{{1,2}}))?", s)
    if not m:
        return None

    dow_raw = m.group(1)
    num = m.group(2)

    dow = _DOW_CANON.get(dow_raw, dow_raw)

    # Si el usuario ya dijo número, se respeta explícitamente
    if num and num.isdigit():
        return f"{dow} {int(num)}"

    # Si dijo solo el día ("miércoles", "para el jueves"),
    # lo resolvemos al próximo más cercano hacia adelante
    target_date = _next_weekday_date(dow_raw)
    return f"{dow} {target_date.day}"


def extract_barber(text: str, barbers: list[str]) -> str | None:
    t = (text or "").lower()
    for b in barbers:
        if b.lower() in t:
            return b
    return None


def extract_time_hhmm(text: str) -> str | None:
    """
    Solo extrae hora exacta cuando realmente parece una hora pedida para reservar.
    Ejemplos válidos:
    - 15:30
    - 1530
    - 930
    - a las 15
    - tipo 15
    """
    t = (text or "").lower().strip()

    # HH:MM exacto
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        return f"{hh:02d}:{mm:02d}"

    # 3-4 dígitos tipo 930 / 1530
    m = re.search(r"\b(\d{3,4})\b", t)
    if m:
        num = m.group(1)
        if len(num) == 3:
            hh = int(num[0])
            mm = int(num[1:])
        else:
            hh = int(num[:2])
            mm = int(num[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"

    # hora entera, pero solo en contexto de pedido exacto
    m = re.search(r"\b(?:a\s+las|tipo|para\s+las)\s+([01]?\d|2[0-3])\b", t)
    if m:
        hh = int(m.group(1))
        return f"{hh:02d}:00"

    return None


def duration_to_blocks(duration_text: str) -> int:
    """
    Convierte textos como:
    - "30 minutos" -> 1
    - "1 Hora" / "1 hora" -> 2
    - "6 Horas" -> 12
    Regla: 1 block = 30 min
    """
    t = (duration_text or "").strip().lower()
    if not t:
        return 1

    # 30 minutos, 45 minutos, etc.
    m = re.search(r"(\d+)\s*min", t)
    if m:
        minutes = int(m.group(1))
        return max(1, (minutes + 29) // 30)

    # 1 hora, 6 horas
    h = re.search(r"(\d+)\s*hor", t)
    if h:
        hours = int(h.group(1))
        return max(1, (hours * 60) // 30)

    return 1