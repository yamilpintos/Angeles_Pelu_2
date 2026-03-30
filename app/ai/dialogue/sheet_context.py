import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from app.ai.client import responses_parse
from app.core.catalog import get_service
from app.core.config import settings
from app.core.types import Session
from app.repos.sheets_repo import get_sheets_repo

from .context import _current_now, _norm_text, _now_context


DAY_FOCUS_SYSTEM = """
Tu única tarea es resolver si el cliente pidió un día concreto y devolver ESE día en formato explícito.

Devolvés JSON válido con estos campos:
- asked_specific_day: boolean
- normalized_day_text: string | null
- confidence: "low" | "medium" | "high"

Reglas:
- Resolver referencias relativas:
  - "hoy"
  - "mañana"
  - "pasado mañana"
  a un día explícito tipo "sábado 14" o "martes 4 de febrero".

- Si el usuario dice solo un día de semana, por ejemplo:
  - "miércoles"
  - "el miércoles"
  - "para el miércoles"
  - "este miércoles"
  - "jueves"
  - "el jueves"
  - "para el jueves"
  - "este jueves"
  interpretarlo siempre como el próximo día de esa semana más cercano hacia adelante desde la fecha actual.

- "Más cercano hacia adelante" significa:
  - si hoy todavía no es ese día, usar el primero que viene
  - si hoy ya es ese día, usar el día actual solo si el mensaje realmente se refiere a hoy
  - si el contexto horario ya dejó vencido ese día para una consulta inmediata, la lógica posterior podrá mover el día efectivo para consultar disponibilidad, pero normalized_day_text debe representar el día de semana pedido más cercano hacia adelante.

- Nunca conviertas "para el miércoles" o "para el jueves" en una fecha lejana o ambigua si existe un miércoles/jueves más próximo.

- Si el usuario dice día de semana + número, conservarlo explícito, por ejemplo:
  - "miércoles 4"
  - "martes 4 de febrero"

- Si el usuario dice mes + número, devolverlo como fecha explícita:
  - "febrero 5" -> "5 de febrero"

- Si el usuario usa expresiones como:
  - "mismo día"
  - "ese día"
  - "para ese día"
  - "dejalo ese día"
  y el contexto trae un pending_anchor_day válido, usar ESE pending_anchor_day como referencia principal.

- Solo si NO existe pending_anchor_day y sí existe draft.day_text válido, usar draft.day_text.

- Esas expresiones NO significan "hoy" salvo que el usuario diga explícitamente "hoy".

- Si el mensaje actual no menciona un día nuevo de forma explícita, pero el contexto ya trae un pending_anchor_day o un draft.day_text válido y el mensaje parece una continuación o refinamiento de una consulta anterior, no inventes un día nuevo.

- En ese caso devolvé:
  - asked_specific_day = false
  - normalized_day_text = null

- No conviertas preguntas como "después de la 1", "más tarde", "a la tarde", "qué tiene", "hay turno" en el día actual si el mensaje no nombró un día.

- Si el usuario no pidió un día concreto, devolver:
  - asked_specific_day = false
  - normalized_day_text = null

- No inventes una fecha si realmente no hay ninguna referencia temporal.
- Si hay duda leve pero razonable, devolvé la mejor resolución y confidence = "medium".
- Nunca devuelvas "hoy", "mañana", "pasado mañana", "este jueves" o similares en normalized_day_text.
"""


class DayFocus(BaseModel):
    asked_specific_day: bool = False
    normalized_day_text: Optional[str] = None
    confidence: str = "low"


def _pending_selected_booking(session: Session) -> Optional[dict]:
    pending = getattr(session, "pending", None)
    if not pending:
        return None

    if getattr(pending, "type", None) not in {"choose_new_slot", "confirm_reschedule"}:
        return None

    options = getattr(pending, "options", None) or []
    for opt in options:
        if isinstance(opt, dict) and opt.get("__selected_booking__"):
            return opt

    return None


def _service_blocks(service_key: Optional[str]) -> int:
    if not service_key:
        return 1

    try:
        svc = get_service(service_key)
        if svc is None:
            return 1

        if isinstance(svc, dict):
            raw = svc.get("blocks")
        else:
            raw = getattr(svc, "blocks", None)

        return max(1, int(raw or 1))
    except Exception:
        return 1


def _safe_ignore_int(value) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _build_context_ignore_range(session: Session, service_key: Optional[str]) -> Optional[Dict[str, Any]]:
    selected = _pending_selected_booking(session)
    if not selected:
        return None

    selected_service_key = selected.get("service_key") or service_key

    tab = str(selected.get("tab") or "").strip() or None
    sheet_id = _safe_ignore_int(selected.get("sheet_id"))
    row = _safe_ignore_int(selected.get("row"))
    col = _safe_ignore_int(selected.get("col"))

    blocks = _safe_ignore_int(selected.get("blocks"))
    if blocks is None or blocks <= 0:
        blocks = _service_blocks(selected_service_key)

    if tab and sheet_id is not None and row is not None and col is not None:
        out = {
            "tab": tab,
            "sheet_id": sheet_id,
            "row": row,
            "col": col,
            "blocks": max(1, int(blocks or 1)),
            "__source__": "selected_booking_anchor_coords",
        }
        print("[DBG CONTEXT IGNORE RANGE]", out)
        return out

    print(
        "[DBG CONTEXT IGNORE RANGE MISSING COORDS]",
        {
            "selected_id": selected.get("id"),
            "tab": selected.get("tab"),
            "sheet_id": selected.get("sheet_id"),
            "row": selected.get("row"),
            "col": selected.get("col"),
            "blocks": selected.get("blocks"),
            "service_key": selected_service_key,
        },
    )
    return None


def _service_last_start_hhmm(service_key: Optional[str]) -> str:
    blocks = _service_blocks(service_key)

    latest_minutes = (20 * 60) - (blocks * 30)
    if latest_minutes < 12 * 60:
        latest_minutes = 12 * 60

    latest_h = latest_minutes // 60
    latest_m = latest_minutes % 60
    return f"{latest_h:02d}:{latest_m:02d}"


def _all_turn_slots(service_key: Optional[str] = None) -> List[str]:
    slots: List[str] = []
    latest = _service_last_start_hhmm(service_key)

    total = 12 * 60
    end = _hhmm_to_minutes(latest)

    while total <= end:
        slots.append(_minutes_to_hhmm(total))
        total += 30

    return slots


def _unique_in_order(values: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _normalize_hhmm(x) -> str:
    s = str(x or "").strip()

    if ":" in s:
        h, m = s.split(":", 1)
        if h.isdigit() and m.isdigit():
            return f"{int(h):02d}:{int(m):02d}"

    if "." in s:
        h, m = s.split(".", 1)
        if h.isdigit() and m.isdigit():
            return f"{int(h):02d}:{int(m):02d}"

    digits = "".join(re.findall(r"\d+", s))
    if not digits:
        raise ValueError(f"bad hhmm (no digits): {s!r}")

    if len(digits) == 4:
        hh, mm = digits[:2], digits[2:]
    elif len(digits) == 3:
        hh, mm = digits[:1], digits[1:]
    elif len(digits) <= 2:
        hh, mm = digits, "00"
    else:
        raise ValueError(f"bad hhmm (len={len(digits)}): {s!r}")

    return f"{int(hh):02d}:{int(mm):02d}"


def _service_duration_minutes(service_key: Optional[str]) -> Optional[int]:
    if not service_key:
        return None

    try:
        svc = get_service(service_key)
        if svc is None:
            return None

        if isinstance(svc, dict):
            raw = (
                svc.get("duration_min")
                or svc.get("duration_minutes")
                or svc.get("minutes")
            )
        else:
            raw = (
                getattr(svc, "duration_min", None)
                or getattr(svc, "duration_minutes", None)
                or getattr(svc, "minutes", None)
            )

        return int(raw) if raw is not None else None
    except Exception as e:
        print("[DBG SERVICE DURATION ERROR]", type(e).__name__, str(e))
        return None


def _service_allowed_barbers(service_key: Optional[str]) -> List[str]:
    if not service_key:
        return []

    try:
        svc = get_service(service_key)
        if svc is None:
            return []

        candidates = None

        if isinstance(svc, dict):
            for key in ("allowed_barbers", "barbers", "only_barbers", "valid_barbers"):
                if key in svc and svc.get(key):
                    candidates = svc.get(key)
                    break
        else:
            for key in ("allowed_barbers", "barbers", "only_barbers", "valid_barbers"):
                value = getattr(svc, key, None)
                if value:
                    candidates = value
                    break

        if not candidates:
            return []

        all_barbers = [str(b) for b in getattr(settings, "BARBERS", [])]
        allowed_norm = {_norm_text(str(x)) for x in candidates}
        allowed = [b for b in all_barbers if _norm_text(b) in allowed_norm]
        return allowed
    except Exception as e:
        print("[DBG SERVICE ALLOWED BARBERS ERROR]", type(e).__name__, str(e))
        return []


def _service_is_restricted(service_key: Optional[str]) -> bool:
    allowed = _service_allowed_barbers(service_key)
    all_barbers = [str(b) for b in getattr(settings, "BARBERS", [])]
    return bool(allowed) and len(allowed) < len(all_barbers)


def _infer_service_key_from_user_text(user_text: str, session: Session) -> Optional[str]:
    current = getattr(session.draft, "service_key", None)
    if current:
        return str(current)

    norm = _norm_text(user_text)

    color_markers = [
        "color (mechas/global) + corte",
        "color mechas global",
        "color y mechas globales",
        "mechas globales",
        "mechas global",
        "color global",
        "mechas y color global",
        "colo y mechas globales",
    ]
    if any(marker in norm for marker in color_markers):
        return "COLOR_MECHAS_GLOBAL_MAS_CORTE"

    return None


def _context_service_key(user_text: str, session: Session) -> Optional[str]:
    return _infer_service_key_from_user_text(user_text, session) or getattr(session.draft, "service_key", None)


def _general_restricted_service_start() -> Tuple[str, Optional[str], List[str]]:
    now = _current_now()
    notes: List[str] = []

    if now.weekday() != 6:
        next_slot = _next_slot_from_now(now)
    else:
        next_slot = None

    if next_slot is not None:
        base_day = _format_explicit_day(now)
        min_time = next_slot
        notes.append(f"- Punto de partida para buscar próximos turnos: {base_day} desde {min_time}.")
        return base_day, min_time, notes

    next_day = _next_open_day(now)
    base_day = _format_explicit_day(next_day)
    min_time = "12:00"
    notes.append(f"- Punto de partida para buscar próximos turnos: {base_day} desde 12:00.")
    return base_day, min_time, notes


def _candidate_forward_dates(start_day: Optional[str], days_ahead: int = 31) -> List[datetime]:
    now = _current_now()
    base_date = now

    if start_day:
        for delta in range(0, days_ahead + 1):
            cand = now + timedelta(days=delta)
            if _same_calendar_day(start_day, cand):
                base_date = cand
                break

    out: List[datetime] = []
    current = base_date
    for _ in range(days_ahead + 1):
        if current.weekday() != 6:
            out.append(current)
        current = current + timedelta(days=1)

    return out


def _next_operational_day_for_barber(
    repo,
    barber: str,
    start_day: str,
) -> Optional[str]:
    for dt in _candidate_forward_dates(start_day, days_ahead=31)[1:]:
        day_label = _format_explicit_day(dt)
        try:
            if repo.get_barber_status(barber=barber, day_text=day_label) == "working":
                return day_label
        except Exception:
            continue
    return None


def _operational_barber_context(
    repo,
    barber: str,
    effective_day: str,
    service_key: Optional[str],
) -> List[str]:
    lines: List[str] = []

    try:
        status = repo.get_barber_status(barber=barber, day_text=effective_day)
    except Exception:
        status = "working"

    lines.append(f"- Estado operativo de {barber} para {effective_day}: {status}.")

    if not service_key:
        lines.append(
            "- Todavía no hay servicio confirmado: no responder con horarios exactos; "
            "solo validar si atiende ese día o indicar el próximo día operativo."
        )

    if status in {"absent", "vacation"}:
        next_day = _next_operational_day_for_barber(repo, barber, effective_day)
        if next_day:
            lines.append(f"- Próximo día operativo de {barber}: {next_day}.")
        else:
            lines.append(f"- No se encontró próximo día operativo visible para {barber}.")

    return lines


def _restricted_service_general_context(user_text: str, session: Session, service_key: Optional[str]) -> str:
    if not service_key or not _service_is_restricted(service_key):
        return ""

    allowed_barbers = _service_allowed_barbers(service_key)
    if not allowed_barbers:
        return ""

    explicit_barbers = [b for b in _extract_barbers_from_text(user_text) if b in allowed_barbers]
    target_barbers = explicit_barbers or allowed_barbers

    try:
        repo = get_sheets_repo()
    except Exception as e:
        return (
            "Contexto real del turnero:\n"
            f"- No se pudo cargar el sheet ({type(e).__name__}: {e}).\n"
        )

    base_day, min_time_for_display, notes = _general_restricted_service_start()
    ignore_range = _build_context_ignore_range(session, service_key)

    lines: List[str] = [
        "Contexto real del turnero:",
        "- Consulta general sin día específico para un servicio restringido.",
        f"- Este servicio solo lo realizan {', '.join(allowed_barbers)}.",
        "- Si el cliente pregunta en general por este servicio, respondé con el próximo disponible visible de cada peluquero habilitado.",
    ]
    lines.extend(notes)
    lines.append("- Próximos disponibles visibles dentro de la semana actual:")
    lines.extend(
        _weekly_next_for_restricted_service(
            repo=repo,
            effective_day=base_day,
            service_key=service_key,
            allowed_barbers=target_barbers,
            min_time_for_display=min_time_for_display,
            ignore_range=ignore_range,
        )
    )
    return "\n".join(lines) + "\n"


def _coerce_booking_hour_to_pm_if_needed(hhmm: str) -> str:
    hhmm = _normalize_hhmm(hhmm)
    hh = int(hhmm[:2])
    mm = hhmm[3:5]

    if hh < 12:
        pm_hhmm = f"{hh + 12:02d}:{mm}"
        if "12:00" <= pm_hhmm <= "19:30":
            return pm_hhmm

    return hhmm


def _add_minutes_hhmm(hhmm, minutes: int) -> str:
    hhmm2 = _normalize_hhmm(hhmm)
    h, m = hhmm2.split(":")
    total = int(h) * 60 + int(m) + minutes
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def _summarize_slots(slots: List[str], label_empty: str) -> str:
    clean = [str(t).strip() for t in slots if str(t).strip()]
    if not clean:
        return label_empty

    ranges: List[str] = []
    start = clean[0]
    prev = clean[0]

    for current in clean[1:]:
        if current == _add_minutes_hhmm(prev, 30):
            prev = current
            continue

        if start == prev:
            ranges.append(start)
        else:
            ranges.append(f"{start} a {prev}")

        start = current
        prev = current

    if start == prev:
        ranges.append(start)
    else:
        ranges.append(f"{start} a {prev}")

    return ", ".join(ranges)


def _compute_busy_times(free_times: List[str], service_key: Optional[str] = None) -> List[str]:
    all_slots = _all_turn_slots(service_key)
    free_set = {str(t).strip() for t in free_times if str(t).strip()}
    return [slot for slot in all_slots if slot not in free_set]


def _extract_barbers_from_text(text: str) -> List[str]:
    found: List[str] = []
    all_barbers = [str(b) for b in getattr(settings, "BARBERS", [])]
    norm = _norm_text(text)

    if not norm:
        return []

    if any(x in norm for x in ["cualquiera", "me da igual", "con el que este", "con el que esté"]):
        return []

    for barber in all_barbers:
        if re.search(rf"\b{re.escape(_norm_text(barber))}\b", norm):
            found.append(barber)

    return _unique_in_order(found)


def _extract_requested_barbers(user_text: str, session: Session) -> List[str]:
    explicit_barbers = _extract_barbers_from_text(user_text)
    if explicit_barbers:
        return explicit_barbers

    draft_barber = getattr(session.draft, "barber", None)
    if draft_barber and str(draft_barber).strip().lower() != "cualquiera":
        return [str(draft_barber)]

    return []


def _message_mentions_new_day(user_text: str) -> bool:
    norm = _norm_text(user_text)

    day_words = [
        "hoy", "mañana", "pasado mañana",
        "lunes", "martes", "miercoles", "miércoles", "jueves",
        "viernes", "sabado", "sábado", "domingo",
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]

    if any(word in norm for word in day_words):
        return True

    if re.search(r"\b\d{1,2}\s+de\s+[a-záéíóú]+\b", norm):
        return True

    if re.search(r"\b(?:lunes|martes|miercoles|miércoles|jueves|viernes|sabado|sábado|domingo)\s+\d{1,2}\b", norm):
        return True

    return False


def _extract_day_focus(user_text: str, session: Session) -> DayFocus:
    draft_day = getattr(session.draft, "day_text", None)
    pending_day = _pending_anchor_day(session)
    pending_type = getattr(getattr(session, "pending", None), "type", None)

    prompt = (
        _now_context()
        + "\n"
        + "Contexto breve para resolver solo el día:\n"
        + f"- Pending.type actual: {pending_type}\n"
        + f"- Pending anchor day actual: {pending_day}\n"
        + f"- Draft.day_text actual: {draft_day}\n"
        + f"- Mensaje del cliente: {user_text}\n"
    )

    try:
        parsed = responses_parse(
            model=settings.OPENAI_MODEL,
            system=DAY_FOCUS_SYSTEM,
            user=prompt,
            text_format=DayFocus,
        )
        if parsed and getattr(parsed, "asked_specific_day", False):
            return parsed
    except Exception as e:
        print("[DBG DAY FOCUS ERROR]", type(e).__name__, str(e))

    return DayFocus()


def _is_same_day_reference(user_text: str, focus: DayFocus, now: datetime) -> bool:
    norm = _norm_text(user_text)

    if _is_immediate_request(user_text):
        return True

    same_day_markers = [
        "hoy",
        "esta tarde",
        "esta noche",
        "mas tarde",
        "más tarde",
        "para hoy",
        "por hoy",
        "para esta tarde",
        "ultimos turnos",
        "últimos turnos",
    ]
    if any(marker in norm for marker in same_day_markers):
        return True

    if focus.asked_specific_day and focus.normalized_day_text and _same_calendar_day(focus.normalized_day_text, now):
        return True

    return False


def _should_anchor_reschedule_to_pending_day(session: Session, user_text: str) -> bool:
    pending_type = getattr(getattr(session, "pending", None), "type", None)
    if pending_type not in {"choose_new_slot", "confirm_reschedule"}:
        return False

    pending_day = _pending_anchor_day(session)
    if not pending_day:
        return False

    if _message_mentions_new_day(user_text):
        return False

    return True


def _resolve_same_day_request(
    user_text: str,
    session: Session,
    focus: DayFocus,
    now: datetime,
) -> bool:
    pending_day = _pending_anchor_day(session)

    if _should_anchor_reschedule_to_pending_day(session, user_text) and pending_day:
        anchored_same_day = _same_calendar_day(pending_day, now)
        print(
            "[DBG RESCHEDULE SAME DAY ANCHOR]",
            {
                "user_text": user_text,
                "pending_day": pending_day,
                "anchored_same_day": anchored_same_day,
            },
        )
        return anchored_same_day

    return _is_same_day_reference(user_text, focus, now)


def _is_immediate_request(user_text: str) -> bool:
    norm = _norm_text(user_text)
    immediate_patterns = [
        r"\bahora\b",
        r"\bpara ahora\b",
        r"\bya\b",
        r"\ben este momento\b",
        r"\bpara ya\b",
        r"\btenes turno para ahora\b",
        r"\btene[s] turno para ahora\b",
        r"\bdisponibilidad para ahora\b",
        r"\bturno ya\b",
    ]
    return any(re.search(pattern, norm) for pattern in immediate_patterns)


def _format_explicit_day(dt: datetime, include_month: bool = True) -> str:
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]
    if include_month:
        return f"{dias[dt.weekday()]} {dt.day} de {meses[dt.month - 1]}"
    return f"{dias[dt.weekday()]} {dt.day}"


def _same_calendar_day(day_text: str, dt: datetime) -> bool:
    norm_day = _norm_text(day_text)
    weekday_norm = _norm_text(["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"][dt.weekday()])
    month_norm = _norm_text([
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ][dt.month - 1])

    candidates = [
        f"{weekday_norm} {dt.day}",
        f"{weekday_norm} {dt.day} de {month_norm}",
        f"{dt.day} de {month_norm}",
    ]
    return any(c in norm_day for c in candidates)


def _next_open_day(now: datetime) -> datetime:
    candidate = now + timedelta(days=1)
    while candidate.weekday() == 6:
        candidate += timedelta(days=1)
    return candidate


def _round_up_to_30(total_minutes: int) -> int:
    return ((total_minutes + 29) // 30) * 30


def _hhmm_to_minutes(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def _minutes_to_hhmm(total: int) -> str:
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def _next_slot_from_now(now: datetime) -> Optional[str]:
    start_minutes = 12 * 60
    end_minutes = 19 * 60 + 30
    current_minutes = now.hour * 60 + now.minute

    base_minutes = max(start_minutes, current_minutes + 60)
    rounded = _round_up_to_30(base_minutes)

    if rounded > end_minutes:
        return None

    return _minutes_to_hhmm(rounded)


def _filter_times_from_min(free_times: List[str], min_hhmm: Optional[str]) -> List[str]:
    if not min_hhmm:
        return free_times

    try:
        min_total = int(min_hhmm[:2]) * 60 + int(min_hhmm[3:5])
    except Exception:
        return free_times

    out: List[str] = []
    for t in free_times:
        try:
            total = int(t[:2]) * 60 + int(t[3:5])
            if total >= min_total:
                out.append(t)
        except Exception:
            continue
    return out


def _infer_recommended_start_from_latest_finish(
    day_text: Optional[str],
    barber: Optional[str],
    service_key: Optional[str],
    latest_finish_hhmm: Optional[str],
) -> Optional[str]:
    if not day_text or not barber or not service_key or not latest_finish_hhmm:
        return None

    duration_min = _service_duration_minutes(service_key)
    if not duration_min:
        return None

    try:
        latest_finish = _hhmm_to_minutes(_coerce_booking_hour_to_pm_if_needed(latest_finish_hhmm))
        latest_start_allowed = latest_finish - duration_min

        repo = get_sheets_repo()
        free_times = repo.get_free_times_for_day(barber=barber, day_text=day_text)
        if not free_times:
            return None

        free_times_clean = [str(x).strip() for x in free_times if str(x).strip()]
        candidates: List[str] = []

        for t in free_times_clean:
            try:
                hhmm = _normalize_hhmm(t)
                total = _hhmm_to_minutes(hhmm)
                if total <= latest_start_allowed:
                    candidates.append(hhmm)
            except Exception:
                continue

        if not candidates:
            return None

        return max(candidates, key=_hhmm_to_minutes)

    except Exception as e:
        print("[DBG RECOMMENDED START ERROR]", type(e).__name__, str(e))
        return None


def _nearest_grid_suggestions(hhmm: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        total = _hhmm_to_minutes(_normalize_hhmm(hhmm))
    except Exception:
        return None, None

    lower = (total // 30) * 30
    upper = lower if total % 30 == 0 else lower + 30

    start_minutes = 12 * 60
    end_minutes = 19 * 60 + 30

    lower_txt = _minutes_to_hhmm(lower) if start_minutes <= lower <= end_minutes else None
    upper_txt = _minutes_to_hhmm(upper) if start_minutes <= upper <= end_minutes else None
    return lower_txt, upper_txt


def _extract_invalid_grid_time(user_text: str) -> Optional[str]:
    norm = _norm_text(user_text)

    patterns = [
        r"\ba las\s+(\d{1,2}(?::\d{1,2})?)\b",
        r"\btipo\s+(\d{1,2}(?::\d{1,2})?)\b",
        r"\b(\d{1,2}:\d{1,2})\b",
        r"\b(\d{1,2}\.\d{1,2})\b",
    ]

    for pattern in patterns:
        m = re.search(pattern, norm)
        if not m:
            continue
        raw = m.group(1)
        try:
            hhmm = _normalize_hhmm(raw)
        except Exception:
            continue
        if hhmm.endswith(":00") or hhmm.endswith(":30"):
            return None
        return hhmm

    return None


def _detect_latest_finish_hint(user_text: str) -> Optional[str]:
    norm = _norm_text(user_text)
    patterns = [
        r"me tengo que ir a las (\d{1,2}(?::\d{1,2})?)",
        r"necesito terminar a las (\d{1,2}(?::\d{1,2})?)",
        r"para las (\d{1,2}(?::\d{1,2})?) tengo que estar saliendo",
    ]
    for pattern in patterns:
        m = re.search(pattern, norm)
        if m:
            try:
                return _coerce_booking_hour_to_pm_if_needed(m.group(1))
            except Exception:
                return None
    return None


def _extract_contextual_min_time(user_text: str, now: datetime) -> Tuple[Optional[str], List[str]]:
    norm = _norm_text(user_text)
    notes: List[str] = []

    explicit_min: Optional[str] = None

    patterns = [
        r"\bdespues de las\s+(\d{1,2}(?::\d{1,2})?)\b",
        r"\bdespués de las\s+(\d{1,2}(?::\d{1,2})?)\b",
        r"\bdespues de\s+(\d{1,2}(?::\d{1,2})?)\b",
        r"\bdespués de\s+(\d{1,2}(?::\d{1,2})?)\b",
        r"\ba partir de las\s+(\d{1,2}(?::\d{1,2})?)\b",
        r"\ba partir de\s+(\d{1,2}(?::\d{1,2})?)\b",
        r"\bdesde las\s+(\d{1,2}(?::\d{1,2})?)\b",
        r"\bdesde\s+(\d{1,2}(?::\d{1,2})?)\b",
        r"\btipo\s+(\d{1,2}(?::\d{1,2})?)\b",
    ]

    raw_time: Optional[str] = None
    for pattern in patterns:
        m = re.search(pattern, norm)
        if m:
            raw_time = m.group(1)
            break

    if raw_time:
        try:
            hhmm = _coerce_booking_hour_to_pm_if_needed(raw_time)
            if _normalize_hhmm(raw_time) != hhmm:
                notes.append(f"- Se interpretó la hora ambigua '{raw_time}' como {hhmm} porque no existen turnos por la mañana.")
            explicit_min = hhmm
        except Exception:
            explicit_min = None

    if explicit_min is None:
        if any(x in norm for x in ["ultimos turnos", "últimos turnos", "ultima franja", "última franja"]):
            explicit_min = "18:00"
            notes.append("- El cliente pidió 'últimos turnos'; se prioriza desde las 18:00.")
        elif any(x in norm for x in ["a la tarde", "por la tarde", "esta tarde", "mas tarde", "más tarde"]):
            explicit_min = "15:00"
            notes.append("- El cliente pidió franja de tarde; se prioriza desde las 15:00.")

    if explicit_min:
        if any(x in norm for x in ["despues de", "después de"]):
            try:
                explicit_min = _add_minutes_hhmm(explicit_min, 30)
                notes.append(f"- Como pidió después de esa hora, se toma como mínimo útil {explicit_min}.")
            except Exception:
                pass
        elif any(x in norm for x in ["a partir de", "desde"]):
            notes.append(f"- Como pidió desde esa hora, se toma como mínimo útil {explicit_min}.")

    return explicit_min, notes


def _pending_anchor_day(session: Session) -> Optional[str]:
    pending = getattr(session, "pending", None)
    if not pending:
        return None

    pending_type = getattr(pending, "type", None)
    if pending_type not in {"choose_slot", "choose_time", "choose_new_slot", "confirm_reschedule"}:
        return None

    options = getattr(pending, "options", None) or []
    if not options:
        return None

    day_values: List[str] = []

    for opt in options:
        if isinstance(opt, dict):
            day_text = opt.get("day_text")
        else:
            day_text = getattr(opt, "day_text", None)

        if day_text:
            day_values.append(str(day_text).strip())

    unique_days = _unique_in_order(day_values)
    return unique_days[0] if unique_days else None


def _resolve_effective_day_and_min_time(
    user_text: str,
    session: Session,
    focus: DayFocus,
) -> Tuple[Optional[str], Optional[str], List[str]]:
    notes: List[str] = []
    now = _current_now()

    same_day_request = _resolve_same_day_request(user_text, session, focus, now)
    draft_day = getattr(session.draft, "day_text", None)
    pending_day = _pending_anchor_day(session)
    mentions_new_day = _message_mentions_new_day(user_text)
    anchored_reschedule_day = _should_anchor_reschedule_to_pending_day(session, user_text)

    if anchored_reschedule_day and pending_day:
        effective_day = pending_day
        notes.append(
            f"- En reprogramación, el mensaje se ancló al día del turno seleccionado: {pending_day}"
        )
    elif mentions_new_day and focus.asked_specific_day and focus.normalized_day_text:
        effective_day = focus.normalized_day_text
    else:
        effective_day = pending_day or draft_day

    if pending_day and not mentions_new_day and not anchored_reschedule_day:
        notes.append(f"- Se usó como ancla conversacional el día del pending activo: {pending_day}")

    min_time_for_display: Optional[str] = None

    contextual_min_time, contextual_notes = _extract_contextual_min_time(user_text, now)
    if contextual_notes:
        notes.extend(contextual_notes)

    if not same_day_request:
        if effective_day and contextual_min_time:
            min_time_for_display = contextual_min_time
            notes.append(
                f"- Se filtrará el día pedido desde {min_time_for_display} por condición horaria del cliente."
            )
        return effective_day, min_time_for_display, notes

    next_slot = _next_slot_from_now(now)

    if not effective_day:
        if next_slot is not None:
            effective_day = _format_explicit_day(now)
            min_time_for_display = next_slot
            notes.append("- El cliente pidió disponibilidad del mismo día.")
            notes.append(f"- Hora mínima útil para mostrar disponibilidad hoy: {min_time_for_display}")
        else:
            next_day = _next_open_day(now)
            effective_day = _format_explicit_day(next_day)
            min_time_for_display = "12:00"
            notes.append("- El cliente pidió disponibilidad del mismo día, pero el horario útil de hoy ya terminó.")
            notes.append(f"- Día efectivo para responder disponibilidad: {effective_day}")
            notes.append("- Hora mínima útil para mostrar disponibilidad en ese día: 12:00")
    else:
        if _same_calendar_day(effective_day, now):
            if next_slot is not None:
                min_time_for_display = next_slot
                notes.append("- El cliente pidió disponibilidad sobre el día actual.")
                notes.append(f"- Hora mínima útil para mostrar disponibilidad hoy: {min_time_for_display}")
            else:
                original_day = effective_day
                next_day = _next_open_day(now)
                effective_day = _format_explicit_day(next_day)
                min_time_for_display = "12:00"
                notes.append("- El cliente pidió disponibilidad sobre el día actual, pero el horario útil de hoy ya terminó.")
                notes.append(f"- Día mencionado originalmente: {original_day}")
                notes.append(f"- Día efectivo para responder disponibilidad: {effective_day}")
                notes.append("- Hora mínima útil para mostrar disponibilidad en ese día: 12:00")

    if contextual_min_time:
        if min_time_for_display:
            min_time_for_display = max(min_time_for_display, contextual_min_time)
        else:
            min_time_for_display = contextual_min_time
        notes.append(f"- Se aplicó además la condición horaria pedida por el cliente: desde {min_time_for_display}.")

    return effective_day, min_time_for_display, notes


def _render_day_barber(
    repo,
    day_label: str,
    barber: str,
    service_key: Optional[str] = None,
    min_time_for_display: Optional[str] = None,
    ignore_range: Optional[Dict[str, Any]] = None,
) -> str:
    try:
        blocks = _service_blocks(service_key)

        free_times = repo.get_day_windows(
            barber=barber,
            day_text=day_label,
            blocks=blocks,
            ignore_range=ignore_range,
        )
    except Exception as e:
        msg = _norm_text(str(e))
        if "no barber col" in msg or "barber col" in msg:
            return f"  - {barber}: no figura columna para este peluquero"
        return f"  - {barber}: no se pudo leer disponibilidad"

    if free_times is None:
        return f"  - {barber}: ese día no figura disponible en el turnero o no se atiende ese día"

    free_times_clean = [str(x).strip() for x in free_times if str(x).strip()]
    free_times_filtered = _filter_times_from_min(free_times_clean, min_time_for_display)

    busy_times_all = _compute_busy_times(free_times_clean, service_key=service_key)
    busy_times = _filter_times_from_min(busy_times_all, min_time_for_display)

    free_txt = _summarize_slots(free_times_filtered, "sin horarios libres")
    busy_txt = _summarize_slots(busy_times, "sin horarios ocupados")
    full_day_note = " | jornada completa libre" if free_times_filtered and not busy_times else ""

    last_start_note = ""
    if service_key:
        latest_start = _service_last_start_hhmm(service_key)
        if latest_start != "19:30":
            last_start_note = f" | ultimo_inicio_posible=[{latest_start}]"

    if min_time_for_display:
        return (
            f"  - {barber}: "
            f"libres_desde_{min_time_for_display}=[{free_txt}] | "
            f"ocupados_desde_{min_time_for_display}=[{busy_txt}]"
            f"{full_day_note}{last_start_note}"
        )

    return (
        f"  - {barber}: "
        f"libres=[{free_txt}] | "
        f"ocupados=[{busy_txt}]"
        f"{full_day_note}{last_start_note}"
    )


def _day_label_no_month(dt: datetime) -> str:
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    return f"{dias[dt.weekday()]} {dt.day}"


def _candidate_week_dates(now: datetime, effective_day: Optional[str]) -> List[datetime]:
    base_date = now
    if effective_day:
        for delta in range(0, 14):
            cand = now + timedelta(days=delta)
            if _same_calendar_day(effective_day, cand):
                base_date = cand
                break

    out: List[datetime] = []
    current = base_date
    while True:
        if current.weekday() != 6:
            out.append(current)
        if current.weekday() == 5:
            break
        current = current + timedelta(days=1)
        if len(out) > 7:
            break
    return out


def _first_free_for_day(
    repo,
    barber: str,
    day_label: str,
    service_key: Optional[str],
    min_time_for_display: Optional[str] = None,
    ignore_range: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, List[str]]]:
    try:
        blocks = _service_blocks(service_key)

        free_times = repo.get_day_windows(
            barber=barber,
            day_text=day_label,
            blocks=blocks,
            ignore_range=ignore_range,
        )
    except Exception:
        return None

    if free_times is None:
        return None

    free_times_clean = [str(x).strip() for x in free_times if str(x).strip()]
    free_times_filtered = _filter_times_from_min(free_times_clean, min_time_for_display)

    if not free_times_filtered:
        return None

    return day_label, free_times_filtered


def _weekly_next_for_restricted_service(
    repo,
    effective_day: str,
    service_key: str,
    allowed_barbers: List[str],
    min_time_for_display: Optional[str],
    ignore_range: Optional[Dict[str, Any]] = None,
) -> List[str]:
    now = _current_now()
    days = _candidate_week_dates(now, effective_day)
    lines: List[str] = []

    for barber in allowed_barbers:
        found = None
        for i, dt in enumerate(days):
            day_label = _format_explicit_day(dt)
            local_min = min_time_for_display if i == 0 else None
            found = _first_free_for_day(
                repo=repo,
                barber=barber,
                day_label=day_label,
                service_key=service_key,
                min_time_for_display=local_min,
                ignore_range=ignore_range,
            )
            if found:
                break

        if found:
            day_label, free_times = found
            first_three = ", ".join(free_times[:3])
            lines.append(f"- Próximo disponible de {barber}: {day_label} -> [{first_three}]")
        else:
            lines.append(f"- Próximo disponible de {barber}: sin lugar visible en la semana actual")

    return lines


def _sheet_context_for_one_day(user_text: str, session: Session) -> str:
    focus = _extract_day_focus(user_text, session)

    print("[DBG DAY FOCUS]", focus.model_dump())

    effective_day, min_time_for_display, timing_notes = _resolve_effective_day_and_min_time(
        user_text, session, focus
    )

    latest_finish_hint = _detect_latest_finish_hint(user_text)
    service_key = _context_service_key(user_text, session)
    allowed_barbers = _service_allowed_barbers(service_key)
    restricted_service = _service_is_restricted(service_key)
    ignore_range = _build_context_ignore_range(session, service_key)

    invalid_grid_time = _extract_invalid_grid_time(user_text)
    grid_notes: List[str] = []
    if invalid_grid_time:
        lower, upper = _nearest_grid_suggestions(invalid_grid_time)
        if lower and upper and lower != upper:
            grid_notes.append(
                f"- El cliente mencionó una hora fuera de grilla ({invalid_grid_time}); horarios cercanos sugeribles: {lower} y {upper}."
            )
        elif lower:
            grid_notes.append(
                f"- El cliente mencionó una hora fuera de grilla ({invalid_grid_time}); horario cercano sugerible: {lower}."
            )
        elif upper:
            grid_notes.append(
                f"- El cliente mencionó una hora fuera de grilla ({invalid_grid_time}); horario cercano sugerible: {upper}."
            )

    if not effective_day:
        restricted_ctx = _restricted_service_general_context(user_text, session, service_key)
        if restricted_ctx:
            lines = [restricted_ctx.rstrip()]
            if latest_finish_hint:
                lines.append(
                    f"- El cliente expresó una hora límite de salida: {latest_finish_hint}. "
                    "Tomalo como latest_finish_hhmm, no como horario de inicio del turno."
                )
            if grid_notes:
                lines.extend(grid_notes)
            return "\n".join(lines) + "\n"

        lines = [
            "Contexto real del turnero:",
            "- En este mensaje no hay un día concreto resuelto para cargar del sheet.",
            "- No asumas disponibilidad si no aparece más abajo un día específico.",
        ]
        if restricted_service and allowed_barbers:
            lines.append(
                f"- Este servicio es restringido: solo lo realizan {', '.join(allowed_barbers)}."
            )
            lines.append(
                "- Si el cliente pregunta la disponibilidad general de este servicio sin día específico, "
                "respondé con el próximo disponible visible de cada peluquero habilitado."
            )
        if latest_finish_hint:
            lines.append(
                f"- El cliente expresó una hora límite de salida: {latest_finish_hint}. "
                "Tomalo como latest_finish_hhmm, no como horario de inicio del turno."
            )
        if grid_notes:
            lines.extend(grid_notes)
        return "\n".join(lines) + "\n"

    try:
        repo = get_sheets_repo()
    except Exception as e:
        return (
            "Contexto real del turnero:\n"
            f"- No se pudo cargar el sheet ({type(e).__name__}: {e}).\n"
        )

    requested_barbers = _extract_requested_barbers(user_text, session)
    same_day_request = _resolve_same_day_request(user_text, session, focus, _current_now())
    pending_day = _pending_anchor_day(session)
    anchored_reschedule_day = _should_anchor_reschedule_to_pending_day(session, user_text)
    resolved_day_for_display = (
        pending_day
        if anchored_reschedule_day and pending_day
        else (focus.normalized_day_text if focus.normalized_day_text else "sin día explícito")
    )

    if restricted_service:
        if requested_barbers:
            requested_barbers = [b for b in requested_barbers if b in allowed_barbers]
        else:
            requested_barbers = allowed_barbers[:]

    explicit_barbers = bool(requested_barbers)

    if not requested_barbers:
        requested_barbers = [str(b) for b in getattr(settings, "BARBERS", [])]

    explicit_barbers_from_text = _extract_barbers_from_text(user_text)
    requested_barber = (
        explicit_barbers_from_text[0]
        if explicit_barbers_from_text
        else getattr(session.draft, "barber", None)
    )

    lines: List[str] = [
        "Contexto real del turnero:",
        "- Se cargó únicamente el día relevante para este mensaje.",
        f"- Día resuelto por IA/contexto: {resolved_day_for_display}",
        f"- Confianza de resolución del día: {focus.confidence}",
        f"- Día efectivo usado para consultar el sheet: {effective_day}",
        "- Rango real de turnos reservables: 12:00 a 19:30.",
        "- El local trabaja hasta las 20:00, pero 19:30 es el último turno reservable para clientes.",
        "- 20:00 y 20:30 no deben considerarse disponibles ni sugerirse.",
    ]

    if anchored_reschedule_day and pending_day:
        lines.append(
            f"- En reprogramación, expresiones como 'mismo día', 'ese día' o una hora sola se anclan al turno seleccionado: {pending_day}."
        )

    if effective_day and requested_barber:
        lines.extend(
            _operational_barber_context(
                repo=repo,
                barber=requested_barber,
                effective_day=effective_day,
                service_key=service_key,
            )
        )

    if restricted_service and allowed_barbers:
        lines.append(
            f"- Este servicio es restringido: solo lo realizan {', '.join(allowed_barbers)}."
        )
        lines.append(
            "- Si el día pedido no tiene lugar con ninguno de los peluqueros habilitados, "
            "debés explicarlo claramente y mostrar el próximo disponible de cada uno dentro de la semana."
        )

    if not explicit_barbers:
        lines.append("- El cliente no pidió un peluquero específico en este mensaje.")
        lines.append(
            "- No respondas como si hubiera pedido a uno puntual; si mostrás opciones, presentalas como panorama general o por peluquero."
        )

    effective_day_norm = _norm_text(effective_day or "")
    if effective_day_norm.startswith("domingo"):
        lines.append(
            "- Atención: domingo no es un día laborable en este turnero. "
            "No respondas como si hubiera disponibilidad real el domingo; indicá que ese día no se trabaja "
            "y ofrecé el próximo día de atención o retomá el día anterior del contexto si corresponde."
        )

    if latest_finish_hint:
        lines.append(
            f"- El cliente expresó una hora límite de salida: {latest_finish_hint}. "
            "Tomalo como latest_finish_hhmm, no como horario de inicio del turno."
        )

    if same_day_request:
        lines.append("- Tipo de consulta detectada: del mismo día.")
        if min_time_for_display:
            lines.append(f"- Mostrar solo horarios útiles desde: {min_time_for_display}")
        else:
            lines.append("- No hay horarios útiles restantes hoy; usar el siguiente día informado.")
    else:
        lines.append("- Tipo de consulta detectada: disponibilidad general del día.")
        if min_time_for_display:
            lines.append(f"- Aplicar filtro horario desde: {min_time_for_display}")
        else:
            lines.append("- Mostrar panorama general del día.")

    if timing_notes:
        lines.append("- Ajuste por contexto horario actual:")
        lines.extend(timing_notes)

    if grid_notes:
        lines.append("- Ajuste por grilla de 30 minutos:")
        lines.extend(grid_notes)

    lines.extend([
        "",
        f"- {effective_day}",
    ])

    day_has_any_for_allowed = False

    for barber in requested_barbers:
        line = _render_day_barber(
            repo=repo,
            day_label=effective_day,
            barber=barber,
            service_key=service_key,
            min_time_for_display=min_time_for_display,
            ignore_range=ignore_range,
        )
        lines.append(line)

        if restricted_service and barber in allowed_barbers:
            result = _first_free_for_day(
                repo=repo,
                barber=barber,
                day_label=effective_day,
                service_key=service_key,
                min_time_for_display=min_time_for_display,
                ignore_range=ignore_range,
            )
            if result:
                day_has_any_for_allowed = True

    if restricted_service and allowed_barbers and not day_has_any_for_allowed:
        lines.append("")
        lines.append("- En el día pedido no hay lugar con ninguno de los peluqueros habilitados para este servicio.")
        lines.append("- Próximos disponibles dentro de la semana:")
        lines.extend(
            _weekly_next_for_restricted_service(
                repo=repo,
                effective_day=effective_day,
                service_key=service_key,
                allowed_barbers=allowed_barbers,
                min_time_for_display=min_time_for_display,
                ignore_range=ignore_range,
            )
        )

    return "\n".join(lines) + "\n"
