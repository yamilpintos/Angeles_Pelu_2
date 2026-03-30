from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.core.catalog import allowed_barbers_for, blocks_for, rgb_for
from app.core.config import settings
from app.core.types import DayAvailability, Draft
from app.core.utils import duration_to_blocks
from app.repos.bookings_repo import (
    build_booking_payload_for_supabase,
    get_bookings_repo,
)
from app.repos.sheets_repo import get_sheets_repo


# =========================================================
# Duración de servicios -> blocks (fallback legacy)
# =========================================================
_SERVICE_DURATION_TEXT: Dict[str, str] = {
    "Corte Hombre/Niño": "30 minutos",
    "Solo Degrade": "30 minutos",
    "Corte c/ Lavado": "30 minutos",
    "Corte c/ Lavado ": "30 minutos",
    "Rapado Hombre": "30 minutos",
    "Barba": "30 minutos",
    "Barba con Paño": "30 minutos",
    "Corte + Barba": "1 Hora",
    "Corte + Paño": "1 Hora",
    "Rapado + Barba": "30 minutos",
    "Color (Mechas/Global) + corte": "6 Horas",
}

# =========================================================
# Reglas operativas
# =========================================================
SLOT_MINUTES = 30
LAST_START_BLOCKED_HHMM = "20:00"   # no se ofrece como inicio
END_LIMIT_HHMM = "20:30"            # ningún servicio puede pasar de acá
MIN_LEAD_MINUTES = 60
VACATION_STREAK_DAYS = 5

_DOW_INDEX_ES: Dict[str, int] = {
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

_DOW_NAME_ES: Dict[int, str] = {
    0: "lunes",
    1: "martes",
    2: "miércoles",
    3: "jueves",
    4: "viernes",
    5: "sábado",
    6: "domingo",
}

_MONTH_INDEX_ES: Dict[str, int] = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

# Cache simple en memoria
# key: (barber_lower, day_text_lower, blocks) -> (timestamp, free_times)
_AVAIL_CACHE: Dict[Tuple[str, str, int], Tuple[float, List[str]]] = {}
CACHE_TTL_SECONDS = int(getattr(settings, "AVAILABILITY_CACHE_TTL_SECONDS", 60) or 60)


# =========================================================
# Helpers base
# =========================================================
def _tz() -> ZoneInfo:
    return ZoneInfo(getattr(settings, "TIMEZONE", "America/Argentina/Buenos_Aires"))


def _now_local() -> datetime:
    return datetime.now(_tz())


def _cache_key(barber: str, day_text: str, blocks: int) -> Tuple[str, str, int]:
    return (barber.strip().lower(), day_text.strip().lower(), max(1, int(blocks or 1)))


def invalidate_day_cache(barber: str, day_text: str) -> None:
    """
    Invalida todas las variantes de blocks para ese barber/día.
    """
    barber_l = barber.strip().lower()
    day_l = day_text.strip().lower()
    keys = [k for k in _AVAIL_CACHE.keys() if k[0] == barber_l and k[1] == day_l]
    for k in keys:
        _AVAIL_CACHE.pop(k, None)


def service_blocks(draft: Draft) -> int:
    """
    Verdad principal: service_key -> catálogo.
    Fallback legacy: service_name -> texto duración.
    """
    sk = (getattr(draft, "service_key", None) or "").strip()
    if sk:
        try:
            return max(1, int(blocks_for(sk)))
        except Exception:
            pass

    s = (draft.service_name or "").strip()
    dur = _SERVICE_DURATION_TEXT.get(s)
    if not dur:
        return 1
    return max(1, int(duration_to_blocks(dur)))


def _is_corte_barba_service(draft: Draft) -> bool:
    """
    Servicio especial con fallback 2 -> 1 bloque.

    Verdad principal:
    - service_key canónica del catálogo

    Fallback defensivo:
    - service_name, solo si por algún motivo no vino bien la key
    """
    sk = (getattr(draft, "service_key", None) or "").strip().upper()
    if sk == "CORTE_MAS_BARBA":
        return True

    sn = (draft.service_name or "").strip().lower()
    return sn in {"corte + barba", "corte y barba"}


def _is_color_global_service(draft: Draft) -> bool:
    """
    Caso especial pedido:
    Color (Mechas/Global) + corte -> solo Franco y Sergio,
    y si no hay lugar el día pedido, comparar próximo disponible semanal de ambos.
    """
    sk = (getattr(draft, "service_key", None) or "").strip().upper()
    if sk == "COLOR_MECHAS_GLOBAL_MAS_CORTE":
        return True

    sn = (draft.service_name or "").strip().lower()
    return sn in {
        "color (mechas/global) + corte",
        "color mechas globales con corte",
        "color y mechas globales con corte",
        "colo y mechas globales con corte",
    }


def _allowed_barbers_for_draft(draft: Draft) -> Optional[List[str]]:
    sk = (getattr(draft, "service_key", None) or "").strip()
    if not sk:
        return None
    try:
        allowed = allowed_barbers_for(sk)
    except Exception:
        return None
    if not allowed:
        return None
    return [str(x).strip() for x in allowed if str(x).strip()]


def _filter_barbers_for_draft(draft: Draft, barbers: List[str]) -> List[str]:
    allowed = _allowed_barbers_for_draft(draft)
    clean = [str(b).strip() for b in (barbers or []) if str(b).strip()]
    if not allowed:
        return clean

    allowed_lower = {x.lower() for x in allowed}
    return [b for b in clean if b.lower() in allowed_lower]


def _is_requested_barber_allowed_for_service(draft: Draft, barber_name: str) -> bool:
    barber = (barber_name or "").strip()
    if not barber:
        return True

    allowed = _allowed_barbers_for_draft(draft)
    if not allowed:
        return True

    return barber.lower() in {x.lower() for x in allowed}


def _barber_service_error(draft: Draft, barber_name: str) -> str:
    service_name = (draft.service_name or "").strip() or "ese servicio"
    allowed = _allowed_barbers_for_draft(draft) or []
    if allowed:
        return f"{service_name} solo se puede reservar con {', '.join(allowed)}."
    return f"{service_name} no se puede reservar con {barber_name}."


def _resolve_effective_blocks_for_slot(
    draft: Draft,
    *,
    time_hhmm: str,
    requested_blocks: Optional[int] = None,
    ignore_range: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Regla:
    - por defecto usa requested_blocks / service_blocks
    - EXCEPCIÓN solo para Corte + Barba:
      si 2 no entra pero 1 sí entra, devuelve 1

    Prioridad:
    - si 2 entra => 2
    - si 2 no entra pero 1 sí => 1
    - si no entra ninguno => devuelve base_blocks
      (el caller terminará rechazando normalmente)
    """
    barber = (draft.barber or "").strip()
    day_text = (draft.day_text or "").strip()
    hhmm = (time_hhmm or "").strip()

    base_blocks = max(1, int(requested_blocks or service_blocks(draft)))

    if not barber or not day_text or not hhmm:
        return base_blocks

    if not _is_corte_barba_service(draft):
        return base_blocks

    repo = get_sheets_repo()
    norm_hhmm = _normalize_hhmm(hhmm)

    ok_base, _ = _slot_allowed(day_text, norm_hhmm, base_blocks)
    if ok_base:
        try:
            if repo.is_slot_free(
                barber=barber,
                day_text=day_text,
                time_hhmm=norm_hhmm,
                blocks=base_blocks,
                ignore_range=ignore_range,
            ):
                return base_blocks
        except Exception:
            return base_blocks

    ok_one, _ = _slot_allowed(day_text, norm_hhmm, 1)
    if ok_one:
        try:
            if repo.is_slot_free(
                barber=barber,
                day_text=day_text,
                time_hhmm=norm_hhmm,
                blocks=1,
                ignore_range=ignore_range,
            ):
                return 1
        except Exception:
            return base_blocks

    return base_blocks


def rgb_from_draft(draft: Draft) -> Optional[Dict[str, float]]:
    """
    Color por catálogo según service_key.
    Si el cliente es jubilado (>=65) usa un color más oscuro.
    """
    sk = (getattr(draft, "service_key", None) or "").strip()
    if not sk:
        return None

    rgb = rgb_for(sk)
    if not rgb:
        return None

    r, g, b = rgb

    age = getattr(draft, "age", None)

    # 👴 Jubilado -> oscurecemos el color
    if age is not None and age >= 65:
        r = max(0, r - 0.25)
        g = max(0, g - 0.25)
        b = max(0, b - 0.25)

    return {
        "red": float(r),
        "green": float(g),
        "blue": float(b),
    }


def _extract_day_num(day_text: str) -> Optional[int]:
    m = re.search(r"\b(\d{1,2})\b", (day_text or "").strip())
    return int(m.group(1)) if m else None


def _extract_dow(day_text: str) -> Optional[str]:
    s = (day_text or "").strip().lower()
    for dow in _DOW_INDEX_ES.keys():
        if dow in s:
            return dow
    return None


def _extract_month(day_text: str) -> Optional[int]:
    s = (day_text or "").strip().lower()
    for month_name, month_num in _MONTH_INDEX_ES.items():
        if re.search(rf"\b{re.escape(month_name)}\b", s):
            return month_num
    return None


def _normalize_hhmm(x: str) -> str:
    s = str(x or "").strip().replace(".", ":")

    if ":" in s:
        h, m = s.split(":", 1)
        if h.isdigit() and m.isdigit():
            hh = int(h)
            mm = int(m)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return f"{hh:02d}:{mm:02d}"
            raise ValueError(f"Hora inválida: {x!r}")

    digits = "".join(re.findall(r"\d+", s))
    if not digits:
        raise ValueError(f"Hora inválida: {x!r}")

    if len(digits) == 4:
        hh, mm = digits[:2], digits[2:]
    elif len(digits) == 3:
        hh, mm = digits[:1], digits[1:]
    elif len(digits) <= 2:
        hh, mm = digits, "00"
    else:
        raise ValueError(f"Hora inválida: {x!r}")

    hh_i = int(hh)
    mm_i = int(mm)
    if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
        raise ValueError(f"Hora inválida: {x!r}")

    return f"{hh_i:02d}:{mm_i:02d}"


def _hhmm_to_minutes(hhmm: str) -> int:
    hhmm2 = _normalize_hhmm(hhmm)
    h, m = hhmm2.split(":")
    return int(h) * 60 + int(m)


def _add_minutes_hhmm(hhmm: str, minutes: int) -> str:
    total = _hhmm_to_minutes(hhmm) + int(minutes)
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def _is_slot_aligned(hhmm: str) -> bool:
    try:
        return _hhmm_to_minutes(hhmm) % SLOT_MINUTES == 0
    except Exception:
        return False


def _latest_allowed_start_hhmm(blocks: int) -> str:
    """
    Último inicio posible para que el servicio termine como máximo a las 20:30.
    Ejemplo:
    - 12 bloques (6 horas) => último inicio 14:30
    """
    b = max(1, int(blocks or 1))
    latest_minutes = _hhmm_to_minutes(END_LIMIT_HHMM) - (b * SLOT_MINUTES)
    if latest_minutes < 0:
        latest_minutes = 0
    return f"{latest_minutes // 60:02d}:{latest_minutes % 60:02d}"


def _is_time_within_schedule_rules(time_hhmm: str, blocks: int) -> bool:
    """
    Reglas duras:
    - horario alineado a bloques de 30 min
    - 20:00 y 20:30 nunca se ofrecen como inicio
    - un servicio no puede terminar después de 20:30
    """
    try:
        if not _is_slot_aligned(time_hhmm):
            return False

        t = _hhmm_to_minutes(time_hhmm)
        latest = _hhmm_to_minutes(_latest_allowed_start_hhmm(blocks))
        blocked_start = _hhmm_to_minutes(LAST_START_BLOCKED_HHMM)
        end_limit = _hhmm_to_minutes(END_LIMIT_HHMM)
    except Exception:
        return False

    if t in {blocked_start, end_limit}:
        return False

    return t <= latest


def _resolve_requested_datetime(day_text: str, time_hhmm: str) -> Optional[datetime]:
    """
    Intenta convertir day_text + hh:mm a datetime local.

    Soporta:
    - 'hoy'
    - 'mañana' / 'manana'
    - 'pasado mañana' / 'pasado manana'
    - 'martes 11'
    - 'martes'
    - '11'
    - '1 de febrero'
    - 'miércoles 1 de febrero'

    Reglas:
    - si hay mes explícito, respeta ese mes
    - si no hay mes explícito y el día numérico ya pasó, usa próximo mes
    - si hay día de semana explícito, debe coincidir con la fecha
    - si la combinación no cierra, devuelve None
    - si es solo día de semana y ya pasó esa hora hoy, usa la próxima semana
    """
    now = _now_local()

    try:
        hhmm = _normalize_hhmm(time_hhmm)
        hh, mm = map(int, hhmm.split(":"))
    except Exception:
        return None

    raw_day = (day_text or "").strip().lower()

    # relativos
    if raw_day == "hoy":
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if raw_day in ("mañana", "manana"):
        base = now + timedelta(days=1)
        return base.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if raw_day in ("pasado mañana", "pasado manana"):
        base = now + timedelta(days=2)
        return base.replace(hour=hh, minute=mm, second=0, microsecond=0)

    day_num = _extract_day_num(day_text)
    dow = _extract_dow(day_text)
    month_num = _extract_month(day_text)

    def try_build(y: int, m: int, d: int) -> Optional[datetime]:
        try:
            return datetime(y, m, d, hh, mm, tzinfo=_tz())
        except ValueError:
            return None

    def matches_dow(dt: datetime) -> bool:
        if not dow:
            return True
        return dt.weekday() == _DOW_INDEX_ES.get(dow)

    # -----------------------------------------------------
    # día numérico explícito
    # -----------------------------------------------------
    if day_num is not None:
        # Caso A: mes explícito ("1 de febrero", "miércoles 1 de febrero")
        if month_num is not None:
            candidate = try_build(now.year, month_num, day_num)

            if candidate is None:
                return None

            if not matches_dow(candidate):
                return None

            # si ya pasó, probar mismo mes del año siguiente
            if candidate < now:
                candidate_next_year = try_build(now.year + 1, month_num, day_num)
                if candidate_next_year is None:
                    return None
                if not matches_dow(candidate_next_year):
                    return None
                return candidate_next_year

            return candidate

        # Caso B: sin mes explícito ("miércoles 1", "1")
        candidate = try_build(now.year, now.month, day_num)

        if candidate is not None and matches_dow(candidate) and candidate >= now:
            return candidate

        # probar mes siguiente
        if now.month == 12:
            y2 = now.year + 1
            m2 = 1
        else:
            y2 = now.year
            m2 = now.month + 1

        candidate2 = try_build(y2, m2, day_num)
        if candidate2 is None:
            return None
        if not matches_dow(candidate2):
            return None
        return candidate2

    # -----------------------------------------------------
    # solo día de semana
    # -----------------------------------------------------
    if dow is not None:
        target_wd = _DOW_INDEX_ES[dow]
        delta = (target_wd - now.weekday()) % 7
        base = now + timedelta(days=delta)
        candidate = base.replace(hour=hh, minute=mm, second=0, microsecond=0)

        if candidate < now:
            candidate = candidate + timedelta(days=7)

        return candidate

    return None


def _passes_lead_time(day_text: str, time_hhmm: str, *, lead_minutes: int = MIN_LEAD_MINUTES) -> bool:
    """
    No se puede reservar con menos de X minutos de anticipación.
    Si no se puede resolver la fecha, se considera inválida.
    """
    now = _now_local()
    target_dt = _resolve_requested_datetime(day_text, time_hhmm)
    if target_dt is None:
        return False

    diff_min = (target_dt - now).total_seconds() / 60.0
    return diff_min >= int(lead_minutes)


def _lead_time_error(day_text: str, time_hhmm: str, *, lead_minutes: int = MIN_LEAD_MINUTES) -> Optional[str]:
    """
    Devuelve un mensaje específico si el horario:
    - tiene fecha inválida
    - ya pasó
    - o no cumple la anticipación mínima
    """
    now = _now_local()
    target_dt = _resolve_requested_datetime(day_text, time_hhmm)
    if target_dt is None:
        return "La fecha indicada es incorrecta. Decime nuevamente el día y horario, por favor."

    diff_min = (target_dt - now).total_seconds() / 60.0

    if diff_min <= 0:
        return "Ese horario ya pasó. Decime otro horario y te ayudo 😊"

    if diff_min < int(lead_minutes):
        return f"Los turnos deben pedirse con al menos {int(lead_minutes)} minutos de anticipación."

    return None


def _slot_allowed(day_text: str, time_hhmm: str, blocks: int) -> Tuple[bool, Optional[str]]:
    """
    Valida reglas duras del turno.
    """
    if not _is_slot_aligned(time_hhmm):
        return False, "Ese horario no coincide con la grilla de turnos de 30 minutos."

    if not _is_time_within_schedule_rules(time_hhmm, blocks):
        latest = _latest_allowed_start_hhmm(blocks)
        return (
            False,
            f"Ese horario no se puede otorgar porque el último inicio posible para ese servicio es {latest}.",
        )

    lead_error = _lead_time_error(day_text, time_hhmm, lead_minutes=MIN_LEAD_MINUTES)
    if lead_error:
        return False, lead_error

    return True, None


def _filter_valid_times(day_text: str, times: List[str], blocks: int) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()

    for t in times:
        tt = (t or "").strip()
        if not tt:
            continue

        try:
            tt = _normalize_hhmm(tt)
        except Exception:
            continue

        if tt in seen:
            continue

        ok, _ = _slot_allowed(day_text, tt, blocks)
        if ok:
            out.append(tt)
            seen.add(tt)

    out.sort(key=_hhmm_to_minutes)
    return out


def _offer_sort_key(item: Dict[str, Any]) -> Tuple[datetime, int, str]:
    d = str(item.get("day_text") or "")
    t = str(item.get("time_hhmm") or "00:00")
    dt = _resolve_requested_datetime(d, t)
    if dt is None:
        dt = datetime.max.replace(tzinfo=_tz())
    return (dt, _hhmm_to_minutes(t), str(item.get("barber") or "").lower())


def _sort_offers(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(offers, key=_offer_sort_key)


def _dedupe_offers(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()

    for item in offers:
        key = (
            str(item.get("barber") or "").strip().lower(),
            str(item.get("day_text") or "").strip().lower(),
            str(item.get("time_hhmm") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)

    return out


def _fallback_next_days(day_text: str, days_ahead: int = 31) -> List[str]:
    """
    Genera una lista simple de días desde el pedido hacia adelante.
    Formato: 'martes 11'
    """
    now = _now_local()
    target_dt = _resolve_requested_datetime(day_text, "12:00")
    start = target_dt or now

    out: List[str] = []
    for i in range(days_ahead):
        d = start + timedelta(days=i)
        out.append(f"{_DOW_NAME_ES[d.weekday()]} {d.day}")
    return out


def _normalize_status(x: Any) -> str:
    s = str(x or "").strip().lower()
    if s in ("working", "trabajando", "activo", "available"):
        return "working"
    if s in ("vacation", "vacaciones", "vacation/absent"):
        return "vacation"
    if s in ("absent", "ausente", "no disponible", "unavailable"):
        return "absent"
    return "working"


def _safe_repo_day_list(repo: Any, day_text: str) -> List[str]:
    if hasattr(repo, "iter_days_from"):
        try:
            days = list(repo.iter_days_from(day_text=day_text))  # type: ignore[attr-defined]
            if days:
                return [str(x) for x in days]
        except Exception:
            pass

    if hasattr(repo, "list_days_in_month"):
        try:
            days = list(repo.list_days_in_month(day_text=day_text))  # type: ignore[attr-defined]
            if days:
                return [str(x) for x in days]
        except Exception:
            pass

    return _fallback_next_days(day_text, days_ahead=31)


def _is_day_fully_absent_x(repo: Any, barber: str, day_text: str) -> bool:
    """
    Intenta determinar si el día está completamente tachado con X
    (sin nombres), usando métodos del repo si existen.
    """
    for method_name in (
        "is_day_fully_absent_x",
        "is_day_fully_blocked_by_x",
        "is_day_fully_x",
    ):
        if hasattr(repo, method_name):
            try:
                fn = getattr(repo, method_name)
                return bool(fn(barber=barber, day_text=day_text))
            except Exception:
                pass

    return False


def _consecutive_absent_x_days(repo: Any, barber: str, day_text: str) -> int:
    """
    Cuenta días consecutivos completamente tachados con X a partir del día pedido.
    Si el repo ya expone ese dato, lo usa.
    """
    for method_name in (
        "count_consecutive_absent_days",
        "count_consecutive_fully_absent_x_days",
        "count_consecutive_fully_x_days",
    ):
        if hasattr(repo, method_name):
            try:
                fn = getattr(repo, method_name)
                return max(0, int(fn(barber=barber, day_text=day_text)))
            except Exception:
                pass

    streak = 0
    for d in _safe_repo_day_list(repo, day_text):
        if _is_day_fully_absent_x(repo, barber, d):
            streak += 1
        else:
            break
    return streak


def _status_for(repo: Any, barber: str, day_text: str) -> str:
    """
    Prioridad:
    1) get_barber_status() del repo, si existe
    2) inferencia por racha de días completos con X:
       - 5+ => vacation
       - 1..4 => absent
       - 0 => working
    """
    if hasattr(repo, "get_barber_status"):
        try:
            st = getattr(repo, "get_barber_status")(barber=barber, day_text=day_text)  # type: ignore[attr-defined]
            return _normalize_status(st)
        except Exception:
            pass

    streak = _consecutive_absent_x_days(repo, barber, day_text)
    if streak >= VACATION_STREAK_DAYS:
        return "vacation"
    if streak >= 1:
        return "absent"
    return "working"


def _load_for(repo: Any, barber: str, day_text: str) -> int:
    """
    Menor carga del día. Intenta usar el mejor método disponible.
    """
    for method_name in (
        "count_bookings_for_day",
        "count_booked_slots",
    ):
        if hasattr(repo, method_name):
            try:
                fn = getattr(repo, method_name)
                return max(0, int(fn(barber=barber, day_text=day_text)))
            except Exception:
                pass
    return 0


# =========================================================
# Availability
# =========================================================
def get_day_availability(
    draft: Draft,
    *,
    force_refresh: bool = False,
    ignore_range: Optional[Dict[str, Any]] = None,
) -> DayAvailability:
    """
    Devuelve horarios libres para (barber, day_text).
    Usa cache con TTL corto.
    Si falla el repo y hay cache vieja, devuelve cache.
    Además filtra horarios inválidos por reglas duras.

    EXCEPCIÓN:
    - Para Corte + Barba, prioriza ventanas de 2 bloques.
    - Si no hay ninguna, permite ventanas de 1 bloque.

    RESTRICCIÓN:
    - Si el servicio solo puede hacerlo cierto peluquero y el draft trae
      uno distinto, devuelve vacío.
    """
    barber = (draft.barber or "").strip()
    day_text = (draft.day_text or "").strip()

    if not barber or not day_text:
        return DayAvailability(barber=barber or "", day_text=day_text or "", free_times=[])

    if not _is_requested_barber_allowed_for_service(draft, barber):
        return DayAvailability(barber=barber, day_text=day_text, free_times=[])

    now_ts = time.time()
    blocks = service_blocks(draft)
    key = _cache_key(barber, day_text, blocks)
    use_cache = (not force_refresh) and (ignore_range is None)

    if use_cache:
        hit = _AVAIL_CACHE.get(key)
        if hit:
            ts, cached = hit
            if now_ts - ts < CACHE_TTL_SECONDS:
                valid_cached = _filter_valid_times(day_text, cached, blocks)
                return DayAvailability(barber=barber, day_text=day_text, free_times=valid_cached)

    repo = get_sheets_repo()
    try:
        if hasattr(repo, "get_day_windows"):
            free = list(
                repo.get_day_windows(
                    barber=barber,
                    day_text=day_text,
                    blocks=blocks,
                    ignore_range=ignore_range,
                )
            )  # type: ignore[attr-defined]
        else:
            free = list(repo.get_free_times_for_day(barber=barber, day_text=day_text))

        if not free and _is_corte_barba_service(draft) and blocks > 1:
            if hasattr(repo, "get_day_windows"):
                free = list(
                    repo.get_day_windows(
                        barber=barber,
                        day_text=day_text,
                        blocks=1,
                        ignore_range=ignore_range,
                    )
                )  # type: ignore[attr-defined]
            else:
                free = list(repo.get_free_times_for_day(barber=barber, day_text=day_text))
    except Exception:
        if use_cache:
            hit = _AVAIL_CACHE.get(key)
            if hit:
                _, cached = hit
                valid_cached = _filter_valid_times(day_text, cached, blocks)
                return DayAvailability(barber=barber, day_text=day_text, free_times=valid_cached)
        raise

    valid_blocks = 1 if (_is_corte_barba_service(draft) and not free and blocks > 1) else blocks
    valid_free = _filter_valid_times(day_text, list(free or []), valid_blocks)

    if use_cache:
        _AVAIL_CACHE[key] = (now_ts, valid_free)

    return DayAvailability(barber=barber, day_text=day_text, free_times=valid_free)


def recheck_slot_live(
    draft: Draft,
    *,
    time_hhmm: str,
    blocks: int = 1,
    ignore_range: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Recheck live antes de reservar.
    También aplica reglas duras:
    - no 20:30
    - no ocupar 20:30
    - no menos de 60 min

    EXCEPCIÓN:
    - Corte + Barba puede caer de 2 -> 1 bloque si 2 no entra y 1 sí.

    RESTRICCIÓN:
    - Rechaza si el peluquero no corresponde al servicio.
    """
    barber = (draft.barber or "").strip()
    day_text = (draft.day_text or "").strip()
    time_hhmm = (time_hhmm or "").strip()

    if not barber or not day_text or not time_hhmm:
        return False

    if not _is_requested_barber_allowed_for_service(draft, barber):
        return False

    effective_blocks = _resolve_effective_blocks_for_slot(
        draft,
        time_hhmm=time_hhmm,
        requested_blocks=blocks,
        ignore_range=ignore_range,
    )

    ok, _ = _slot_allowed(day_text, time_hhmm, effective_blocks)
    if not ok:
        return False

    repo = get_sheets_repo()
    return bool(
        repo.is_slot_free(
            barber=barber,
            day_text=day_text,
            time_hhmm=_normalize_hhmm(time_hhmm),
            blocks=effective_blocks,
            ignore_range=ignore_range,
        )
    )


# =========================================================
# Offers reales + reglas de negocio
# =========================================================
@dataclass
class OffersResult:
    offers: List[Dict[str, Any]]
    reason: str
    requested_barber: Optional[str] = None
    requested_day: Optional[str] = None
    next_same_barber_offers: Optional[List[Dict[str, Any]]] = None
    selectable_slots: Optional[List[Dict[str, Any]]] = None
    day_context: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        self.offers = _dedupe_offers(_sort_offers(list(self.offers or [])))
        self.next_same_barber_offers = _dedupe_offers(
            _sort_offers(list(self.next_same_barber_offers or []))
        )
        selectable = list(self.selectable_slots or self.offers or [])
        self.selectable_slots = _dedupe_offers(_sort_offers(selectable))
        self.day_context = dict(self.day_context or {})


def find_offers(
    draft: Draft,
    *,
    blocks: int,
    barbers: List[str],
    max_offers: int = 3,
    ignore_range: Optional[Dict[str, Any]] = None,
) -> OffersResult:
    """
    Reglas implementadas:

    1) "cualquiera"
       -> peluquero ACTIVO con menor cantidad de turnos en el día pedido

    2) peluquero específico AUSENTE / VACATION
       -> próximo turno disponible con ESE MISMO peluquero, sin limitarse al día pedido
       -> si no existe, fallback al próximo día disponible con cualquiera ACTIVO

    3) peluquero específico WORKING pero día lleno
       -> otro peluquero ACTIVO ese mismo día
       -> además calcula el próximo día disponible con ese mismo peluquero
       -> si no hay, siguiente día con cualquiera ACTIVO

    4) solo se ofrecen horarios válidos:
       - no ocupan 20:30
       - respetan lead time >= 60 min

    5) si el servicio tiene peluqueros habilitados específicos:
       - solo busca dentro de esos peluqueros
       - si el usuario pidió un peluquero inválido, no lo usa y busca con los habilitados

    6) CASO ESPECIAL pedido por negocio:
       - COLOR_MECHAS_GLOBAL_MAS_CORTE:
         * solo Franco y Sergio
         * si el usuario NO eligió peluquero:
             - mostrar ambos si hay lugar ese día
             - si ninguno tiene lugar ese día, devolver el próximo disponible semanal
               de Franco y el próximo disponible semanal de Sergio
         * si el usuario eligió Franco/Sergio:
             - responder puntualmente para ese peluquero

    EXCEPCIÓN:
       - Corte + Barba intenta primero con 2 bloques.
       - Si no encuentra ventanas, cae a 1 bloque.

    CAMBIO CLAVE:
       - offers = preview corta (ej. 3 opciones)
       - selectable_slots = slots reales seleccionables del contexto relevante
       - day_context = panorama real del día consultado para que dialogue pueda conversar mejor
    """
    repo = get_sheets_repo()

    barber_req = (draft.barber or "").strip()
    day_text = (draft.day_text or "").strip()
    blocks_i = max(1, int(blocks or 1))
    max_offers_i = max(1, int(max_offers or 1))

    barbers_clean = [str(b).strip() for b in (barbers or []) if str(b).strip()]
    barbers_effective = _filter_barbers_for_draft(draft, barbers_clean)
    allowed_barbers = _allowed_barbers_for_draft(draft) or []

    barber_req_is_any = barber_req.lower() in ("", "cualquiera", "cualquiera.", "cualquiera!")
    barber_req_allowed = _is_requested_barber_allowed_for_service(draft, barber_req)

    if not day_text or not barbers_effective:
        return OffersResult(
            offers=[],
            reason="no_day",
            requested_barber=barber_req or None,
            requested_day=day_text or None,
            next_same_barber_offers=[],
            selectable_slots=[],
            day_context={
                "day_text": day_text or None,
                "requested_barber": barber_req or None,
                "requested_time": getattr(draft, "time_hhmm", None),
                "service_name": getattr(draft, "service_name", None),
                "service_key": getattr(draft, "service_key", None),
                "allowed_barbers": allowed_barbers,
                "barbers": [],
                "selectable_slots": [],
            },
        )

    def _slot_item(b: str, d: str, t: str) -> Dict[str, Any]:
        return {"barber": b, "day_text": d, "time_hhmm": t}

    def windows_for(b: str, d: str) -> List[str]:
        try:
            if hasattr(repo, "get_day_windows"):
                raw = repo.get_day_windows(
                    barber=b,
                    day_text=d,
                    blocks=blocks_i,
                    ignore_range=ignore_range,
                )  # type: ignore[attr-defined]
            else:
                raw = repo.get_free_times_for_day(barber=b, day_text=d)
        except Exception:
            raw = []

        raw_list = list(raw or [])
        valid = _filter_valid_times(d, raw_list, blocks_i)
        if valid:
            return valid

        if _is_corte_barba_service(draft) and blocks_i > 1:
            try:
                if hasattr(repo, "get_day_windows"):
                    raw_fallback = repo.get_day_windows(
                        barber=b,
                        day_text=d,
                        blocks=1,
                        ignore_range=ignore_range,
                    )  # type: ignore[attr-defined]
                else:
                    raw_fallback = repo.get_free_times_for_day(barber=b, day_text=d)
            except Exception:
                raw_fallback = []

            return _filter_valid_times(d, list(raw_fallback or []), 1)

        return []

    def status_for(b: str, d: str) -> str:
        return _status_for(repo, b, d)

    def load_for(b: str, d: str) -> int:
        return _load_for(repo, b, d)

    def next_days_from(d: str) -> List[str]:
        return _safe_repo_day_list(repo, d)

    def slots_for_barber_day(b: str, d: str) -> List[Dict[str, Any]]:
        ws = windows_for(b, d)
        return [_slot_item(b, d, t) for t in ws]

    def preview_for_barber_day(b: str, d: str) -> List[Dict[str, Any]]:
        return slots_for_barber_day(b, d)[:max_offers_i]

    def build_day_context(d: str, chosen_barbers: List[str]) -> Dict[str, Any]:
        ordered_barbers: List[str] = []
        seen_barbers: set[str] = set()
        for b in chosen_barbers:
            bb = str(b or "").strip()
            if not bb:
                continue
            key = bb.lower()
            if key in seen_barbers:
                continue
            seen_barbers.add(key)
            ordered_barbers.append(bb)

        barbers_payload: List[Dict[str, Any]] = []
        selectable_slots: List[Dict[str, Any]] = []

        for b in ordered_barbers:
            st = status_for(b, d)
            ws = windows_for(b, d) if st == "working" else []
            barbers_payload.append(
                {
                    "barber": b,
                    "day_text": d,
                    "status": st,
                    "free_times": ws,
                    "free_times_count": len(ws),
                }
            )
            selectable_slots.extend(_slot_item(b, d, t) for t in ws)

        selectable_slots = _dedupe_offers(_sort_offers(selectable_slots))
        return {
            "day_text": d,
            "requested_barber": barber_req or None,
            "requested_time": getattr(draft, "time_hhmm", None),
            "service_name": getattr(draft, "service_name", None),
            "service_key": getattr(draft, "service_key", None),
            "allowed_barbers": allowed_barbers,
            "barbers": barbers_payload,
            "selectable_slots": selectable_slots,
        }

    def combine_preview(*groups: List[Dict[str, Any]], limit: Optional[int] = None) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        for grp in groups:
            merged.extend(grp or [])
        clean = _dedupe_offers(_sort_offers(merged))
        if limit is None:
            return clean
        return clean[: max(1, int(limit))]

    def combine_selectable(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        for grp in groups:
            merged.extend(grp or [])
        return _dedupe_offers(_sort_offers(merged))

    def best_same_day_candidates(d: str, exclude_barber: Optional[str] = None) -> List[Tuple[int, str, List[Dict[str, Any]]]]:
        candidates: List[Tuple[int, str, List[Dict[str, Any]]]] = []

        for b in barbers_effective:
            if exclude_barber and b.strip().lower() == exclude_barber.strip().lower():
                continue

            if status_for(b, d) != "working":
                continue

            slots = slots_for_barber_day(b, d)
            if not slots:
                continue

            candidates.append((load_for(b, d), b, slots))

        candidates.sort(key=lambda x: (x[0], x[1].lower()))
        return candidates

    def result_for_day(
        *,
        offers: List[Dict[str, Any]],
        reason: str,
        day_for_context: Optional[str] = None,
        context_barbers: Optional[List[str]] = None,
        selectable_slots: Optional[List[Dict[str, Any]]] = None,
        requested_barber_override: Optional[str] = None,
        next_same: Optional[List[Dict[str, Any]]] = None,
    ) -> OffersResult:
        ctx_day = (day_for_context or day_text or "").strip()
        ctx_barbers = [str(b).strip() for b in (context_barbers or []) if str(b).strip()]
        day_context = build_day_context(ctx_day, ctx_barbers) if ctx_day and ctx_barbers else {
            "day_text": ctx_day or None,
            "requested_barber": requested_barber_override if requested_barber_override is not None else (barber_req or None),
            "requested_time": getattr(draft, "time_hhmm", None),
            "service_name": getattr(draft, "service_name", None),
            "service_key": getattr(draft, "service_key", None),
            "allowed_barbers": allowed_barbers,
            "barbers": [],
            "selectable_slots": [],
        }

        selectable_effective = combine_selectable(
            selectable_slots or [],
            day_context.get("selectable_slots", []) if isinstance(day_context, dict) else [],
            offers,
            next_same or [],
        )

        if isinstance(day_context, dict):
            day_context["requested_barber"] = (
                requested_barber_override if requested_barber_override is not None else (barber_req or None)
            )
            day_context["selectable_slots"] = selectable_effective

        return OffersResult(
            offers=offers,
            reason=reason,
            requested_barber=requested_barber_override if requested_barber_override is not None else (barber_req or None),
            requested_day=ctx_day or (day_text or None),
            next_same_barber_offers=next_same or [],
            selectable_slots=selectable_effective,
            day_context=day_context,
        )

    def next_same_barber_offers_from(d: str, barber_name: str) -> List[Dict[str, Any]]:
        if not _is_requested_barber_allowed_for_service(draft, barber_name):
            return []

        for d2 in next_days_from(d)[1:]:
            if status_for(barber_name, d2) != "working":
                continue
            slots = slots_for_barber_day(barber_name, d2)
            if slots:
                return combine_preview(slots, limit=max_offers_i)
        return []

    def next_allowed_day_offers_from(d: str) -> OffersResult:
        for d2 in next_days_from(d)[1:]:
            day_candidates = best_same_day_candidates(d2)
            if not day_candidates:
                continue

            _, bbest, slots_best = day_candidates[0]
            offers_preview = combine_preview(slots_best, limit=max_offers_i)
            selectable = combine_selectable(*[slots for _, _, slots in day_candidates])
            barbers_ctx = [b for _, b, _ in day_candidates]
            return result_for_day(
                offers=offers_preview,
                reason="service_next_day",
                day_for_context=d2,
                context_barbers=barbers_ctx or [bbest],
                selectable_slots=selectable,
            )

        return result_for_day(
            offers=[],
            reason="no_offers",
            day_for_context=day_text,
            context_barbers=barbers_effective,
            selectable_slots=[],
        )

    def week_scope_from(d: str) -> List[str]:
        """
        Devuelve los días desde el pedido hasta el sábado de esa misma semana
        (o máximo 7 días si no se puede resolver bien).
        """
        out: List[str] = []
        for d2 in next_days_from(d):
            out.append(d2)
            dt2 = _resolve_requested_datetime(d2, "12:00")
            if dt2 is not None and dt2.weekday() == 5:  # sábado
                break
            if len(out) >= 7:
                break
        return out

    def next_weekly_offers_for_barber(start_day: str, barber_name: str) -> List[Dict[str, Any]]:
        """
        Devuelve el primer día de la semana que tenga lugar para ese peluquero,
        con hasta max_offers horarios de ese día.
        """
        for d2 in week_scope_from(start_day):
            if status_for(barber_name, d2) != "working":
                continue
            slots = slots_for_barber_day(barber_name, d2)
            if slots:
                return combine_preview(slots, limit=max_offers_i)
        return []

    def merge_grouped_offers(groups: List[List[Dict[str, Any]]], *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        merged = combine_selectable(*groups)
        if limit is None:
            return merged
        return merged[: max(1, int(limit))]

    # -----------------------------------------------------
    # Caso especial: COLOR + MECHAS GLOBAL + CORTE
    # -----------------------------------------------------
    if _is_color_global_service(draft):
        allowed_color = [b for b in (allowed_barbers or []) if b.lower() in {"franco", "sergio"}]
        if not allowed_color:
            allowed_color = [b for b in barbers_effective if b.lower() in {"franco", "sergio"}]

        if allowed_color:
            barbers_effective = allowed_color

        if barber_req and not barber_req_is_any and not barber_req_allowed:
            weekly_groups: List[List[Dict[str, Any]]] = []
            for b in barbers_effective:
                grp = next_weekly_offers_for_barber(day_text, b)
                if grp:
                    weekly_groups.append(grp)

            preview = merge_grouped_offers(
                weekly_groups,
                limit=max_offers_i * max(1, len(weekly_groups)) if weekly_groups else max_offers_i,
            )
            return result_for_day(
                offers=preview,
                reason="invalid_barber_for_service_next_day",
                day_for_context=day_text,
                context_barbers=barbers_effective,
                selectable_slots=preview,
            )

        if barber_req and not barber_req_is_any:
            st = status_for(barber_req, day_text)

            if st in ("absent", "vacation"):
                next_same = next_weekly_offers_for_barber(day_text, barber_req)
                if next_same:
                    return result_for_day(
                        offers=next_same,
                        reason=st,
                        day_for_context=day_text,
                        context_barbers=[barber_req],
                        selectable_slots=next_same,
                        requested_barber_override=barber_req,
                        next_same=next_same,
                    )

                weekly_groups: List[List[Dict[str, Any]]] = []
                for b in barbers_effective:
                    grp = next_weekly_offers_for_barber(day_text, b)
                    if grp:
                        weekly_groups.append(grp)

                preview = merge_grouped_offers(
                    weekly_groups,
                    limit=max_offers_i * max(1, len(weekly_groups)) if weekly_groups else max_offers_i,
                )
                return result_for_day(
                    offers=preview,
                    reason="restricted_service_compare_week",
                    day_for_context=day_text,
                    context_barbers=barbers_effective,
                    selectable_slots=preview,
                    requested_barber_override=barber_req,
                )

            direct_slots = slots_for_barber_day(barber_req, day_text)
            if direct_slots:
                preview = combine_preview(direct_slots, limit=max_offers_i)
                return result_for_day(
                    offers=preview,
                    reason="requested_barber_same_day",
                    day_for_context=day_text,
                    context_barbers=[barber_req],
                    selectable_slots=direct_slots,
                    requested_barber_override=barber_req,
                )

            next_same = next_weekly_offers_for_barber(day_text, barber_req)
            if next_same:
                return result_for_day(
                    offers=next_same,
                    reason="next_same_barber_day",
                    day_for_context=day_text,
                    context_barbers=[barber_req],
                    selectable_slots=next_same,
                    requested_barber_override=barber_req,
                    next_same=next_same,
                )

            return result_for_day(
                offers=[],
                reason="no_offers",
                day_for_context=day_text,
                context_barbers=[barber_req],
                selectable_slots=[],
                requested_barber_override=barber_req,
            )

        same_day_groups: List[List[Dict[str, Any]]] = []
        same_day_barbers: List[str] = []
        for b in barbers_effective:
            if status_for(b, day_text) != "working":
                continue
            grp = slots_for_barber_day(b, day_text)
            if grp:
                same_day_groups.append(grp)
                same_day_barbers.append(b)

        if same_day_groups:
            selectable = merge_grouped_offers(same_day_groups, limit=None)
            preview = merge_grouped_offers(
                [grp[:max_offers_i] for grp in same_day_groups],
                limit=max_offers_i * max(1, len(same_day_groups)),
            )
            return result_for_day(
                offers=preview,
                reason="restricted_service_same_day_compare",
                day_for_context=day_text,
                context_barbers=same_day_barbers,
                selectable_slots=selectable,
            )

        weekly_groups: List[List[Dict[str, Any]]] = []
        for b in barbers_effective:
            grp = next_weekly_offers_for_barber(day_text, b)
            if grp:
                weekly_groups.append(grp)

        preview = merge_grouped_offers(
            weekly_groups,
            limit=max_offers_i * max(1, len(weekly_groups)) if weekly_groups else max_offers_i,
        )
        return result_for_day(
            offers=preview,
            reason="restricted_service_compare_week",
            day_for_context=day_text,
            context_barbers=barbers_effective,
            selectable_slots=preview,
        )

    # -----------------------------------------------------
    # Caso especial: peluquero pedido inválido para el servicio
    # -----------------------------------------------------
    if barber_req and not barber_req_is_any and not barber_req_allowed:
        same_day = best_same_day_candidates(day_text)
        if same_day:
            _, bbest, slots_best = same_day[0]
            preview = combine_preview(slots_best, limit=max_offers_i)
            selectable = combine_selectable(*[slots for _, _, slots in same_day])
            return result_for_day(
                offers=preview,
                reason="invalid_barber_for_service",
                day_for_context=day_text,
                context_barbers=[b for _, b, _ in same_day] or [bbest],
                selectable_slots=selectable,
            )

        res = next_allowed_day_offers_from(day_text)
        if res.offers:
            res.reason = "invalid_barber_for_service_next_day"
        return res

    # -----------------------------------------------------
    # Caso 1: "cualquiera"
    # -----------------------------------------------------
    if barber_req_is_any:
        same_day = best_same_day_candidates(day_text)
        if same_day:
            _, bbest, slots_best = same_day[0]
            preview = combine_preview(slots_best, limit=max_offers_i)
            selectable = combine_selectable(*[slots for _, _, slots in same_day])
            return result_for_day(
                offers=preview,
                reason="any_barber_same_day",
                day_for_context=day_text,
                context_barbers=[b for _, b, _ in same_day] or [bbest],
                selectable_slots=selectable,
            )

        return next_allowed_day_offers_from(day_text)

    # -----------------------------------------------------
    # Caso 2: barbero específico válido
    # -----------------------------------------------------
    if barber_req and day_text:
        st = status_for(barber_req, day_text)

        if st in ("absent", "vacation"):
            for d2 in next_days_from(day_text):
                if status_for(barber_req, d2) != "working":
                    continue
                slots = slots_for_barber_day(barber_req, d2)
                if slots:
                    clean_preview = combine_preview(slots, limit=max_offers_i)
                    return result_for_day(
                        offers=clean_preview,
                        reason=st,
                        day_for_context=d2,
                        context_barbers=[barber_req],
                        selectable_slots=slots,
                        requested_barber_override=barber_req,
                        next_same=clean_preview,
                    )

            res = next_allowed_day_offers_from(day_text)
            if res.offers:
                res.reason = f"{st}_fallback_other_barber"
            else:
                res.reason = st
            return res

        direct_slots = slots_for_barber_day(barber_req, day_text)
        if direct_slots:
            preview = combine_preview(direct_slots, limit=max_offers_i)
            return result_for_day(
                offers=preview,
                reason="requested_barber_same_day",
                day_for_context=day_text,
                context_barbers=[barber_req],
                selectable_slots=direct_slots,
                requested_barber_override=barber_req,
            )

        same_day_alt = best_same_day_candidates(day_text, exclude_barber=barber_req)
        next_same = next_same_barber_offers_from(day_text, barber_req)

        if same_day_alt:
            selectable = combine_selectable(*[slots for _, _, slots in same_day_alt])
            preview = combine_preview(
                *[slots[:max_offers_i] for _, _, slots in same_day_alt],
                limit=max_offers_i,
            )
            return result_for_day(
                offers=preview,
                reason="fully_booked_same_day",
                day_for_context=day_text,
                context_barbers=[b for _, b, _ in same_day_alt],
                selectable_slots=selectable,
                requested_barber_override=barber_req,
                next_same=next_same,
            )

        if next_same:
            return result_for_day(
                offers=next_same,
                reason="next_same_barber_day",
                day_for_context=day_text,
                context_barbers=[barber_req],
                selectable_slots=next_same,
                requested_barber_override=barber_req,
                next_same=next_same,
            )

        res = next_allowed_day_offers_from(day_text)
        if res.offers:
            res.reason = "next_day_other_barber"
        return res

    return result_for_day(
        offers=[],
        reason="no_offers",
        day_for_context=day_text,
        context_barbers=barbers_effective,
        selectable_slots=[],
    )


# =========================================================
# Reserva REAL (Sheets + Supabase) con rollback
# =========================================================
@dataclass
class ReserveResult:
    ok: bool
    booking_id: Optional[int] = None
    error: Optional[str] = None
    reason: Optional[str] = None
    sheet_coords: Optional[Dict[str, Any]] = None


def reserve_slot(
    *,
    draft: Draft,
    phone: str,
    provider: str = "twilio",
    blocks: int = 1,
    rgb: Optional[Dict[str, float]] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    ignore_range: Optional[Dict[str, Any]] = None,
) -> ReserveResult:
    """
    Reserva REAL y consistente:
    1) valida compatibilidad servicio/peluquero
    2) valida reglas duras (20:30 y lead time)
    3) recheck live
    4) paint_blocks en Sheets
    5) insert booking en Supabase
    6) si falla Supabase -> rollback clear_blocks

    EXCEPCIÓN:
    - Corte + Barba puede caer de 2 -> 1 bloque si 2 no entra y 1 sí.
    """
    customer_name = (draft.customer_name or "").strip()
    barber = (draft.barber or "").strip()
    day_text = (draft.day_text or "").strip()
    time_hhmm = (draft.time_hhmm or "").strip()
    service_name = (draft.service_name or None)

    if not customer_name or not barber or not day_text or not time_hhmm:
        return ReserveResult(
            ok=False,
            error="Faltan datos para reservar (nombre/barbero/día/hora).",
            reason="missing_data",
        )

    if not _is_requested_barber_allowed_for_service(draft, barber):
        return ReserveResult(
            ok=False,
            error=_barber_service_error(draft, barber),
            reason="invalid_barber_for_service",
        )

    requested_blocks = max(1, int(blocks or 1))
    effective_blocks = _resolve_effective_blocks_for_slot(
        draft,
        time_hhmm=time_hhmm,
        requested_blocks=requested_blocks,
        ignore_range=ignore_range,
    )

    ok, reason = _slot_allowed(day_text, time_hhmm, effective_blocks)
    if not ok:
        return ReserveResult(
            ok=False,
            error=reason or "Ese horario no se puede otorgar.",
            reason="invalid_slot",
        )

    sheets = get_sheets_repo()
    norm_hhmm = _normalize_hhmm(time_hhmm)

    # 1) RECHECK LIVE
    try:
        is_free = sheets.is_slot_free(
            barber=barber,
            day_text=day_text,
            time_hhmm=norm_hhmm,
            blocks=effective_blocks,
            ignore_range=ignore_range,
        )
    except Exception as e:
        return ReserveResult(
            ok=False,
            error=f"No pude verificar disponibilidad en vivo: {e}",
            reason="sheet_error",
        )

    if not is_free:
        return ReserveResult(
            ok=False,
            error="Ese horario ya no está disponible 😕",
            reason="slot_taken",
        )

    # 2) PINTAR EN SHEETS (verdad primaria)
    paint = sheets.paint_blocks(
        barber=barber,
        day_text=day_text,
        time_hhmm=norm_hhmm,
        blocks=effective_blocks,
        customer_name=customer_name,
        rgb=rgb,
        ignore_range=ignore_range,
    )

    if not paint.get("ok"):
        return ReserveResult(
            ok=False,
            error=f"No pude agendar en el turnero: {paint.get('error')}",
            reason="sheet_error",
            sheet_coords=paint,
        )

    # coords confirmadas para DB
    try:
        tab = paint.get("tab")
        sheet_id = int(paint.get("sheet_id"))
        row = int(paint.get("row"))
        col = int(paint.get("col"))
        blocks_i = int(paint.get("blocks"))
    except Exception:
        return ReserveResult(
            ok=False,
            error="El turnero devolvió coordenadas inválidas al reservar.",
            reason="invalid_sheet_coords",
            sheet_coords=paint,
        )

    # 3) INSERT EN SUPABASE
    bookings = get_bookings_repo()

    payload = build_booking_payload_for_supabase(
        phone=phone,
        provider=provider,
        customer_name=customer_name,
        barber=barber,
        time_hhmm=norm_hhmm,
        sheet_id=sheet_id,
        tab=str(tab) if tab else None,
        row=row,
        col=col,
        blocks=blocks_i,
        day_num=_extract_day_num(day_text),
        date_text=day_text,
        date_iso=None,
        service_name=service_name,
        service_canonical=(getattr(draft, "service_key", None) or None),
        metadata={
            **({"age": getattr(draft, "age", None)} if getattr(draft, "age", None) is not None else {}),
            **(extra_metadata or {}),
        },
    )

    ins = bookings.create_booking(payload)

    if not ins.ok or not ins.booking_id:
        # 4) ROLLBACK si DB falla
        try:
            sheets.clear_blocks(
                tab=str(tab),
                sheet_id=sheet_id,
                row=row,
                col=col,
                blocks=blocks_i,
            )
        except Exception:
            pass

        return ReserveResult(
            ok=False,
            error=f"No pude guardar en la base: {ins.error}",
            reason="db_error",
            sheet_coords=paint,
        )

    # 5) invalidar cache de ese día para evitar offers viejos
    try:
        invalidate_day_cache(barber, day_text)
    except Exception:
        pass

    return ReserveResult(
        ok=True,
        booking_id=int(ins.booking_id),
        sheet_coords=paint,
    )