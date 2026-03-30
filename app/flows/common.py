from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.actions.booking import _load_for, _status_for
from app.core.catalog import allowed_barbers_for
from app.core.config import settings
from app.core.utils import merge_draft
from app.repos.sheets_repo import get_sheets_repo


def allowed_barbers_for_session(session) -> list[str]:
    service_key = (getattr(session.draft, "service_key", None) or "").strip()
    if not service_key:
        return []
    try:
        allowed = allowed_barbers_for(service_key)
    except Exception:
        return []
    return [str(x).strip() for x in (allowed or []) if str(x).strip()]


def human_join(parts: list[str]) -> str:
    vals = [str(x).strip() for x in parts if str(x).strip()]
    if not vals:
        return ""
    if len(vals) == 1:
        return vals[0]
    if len(vals) == 2:
        return f"{vals[0]} y {vals[1]}"
    return ", ".join(vals[:-1]) + f" y {vals[-1]}"


def service_barber_error_for_chat(session) -> str | None:
    barber = (getattr(session.draft, "barber", None) or "").strip()
    service_name = (getattr(session.draft, "service_name", None) or "").strip()
    if not barber:
        return None

    allowed = allowed_barbers_for_session(session)
    if not allowed:
        return None

    allowed_lower = {x.lower() for x in allowed}
    if barber.lower() in {"cualquiera", "cualquiera.", "cualquiera!"}:
        return None
    if barber.lower() in allowed_lower:
        return None

    service_label = service_name or "Ese servicio"
    return f"{service_label} solo se puede reservar con {human_join(allowed)}."


def pick_relevant_booking_for_late(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]

    tz = ZoneInfo(settings.TIMEZONE)
    now = datetime.now(tz)
    future_candidates: list[tuple[datetime, dict]] = []

    for row in rows:
        iso = row.get("starts_at")
        if not iso:
            continue
        try:
            dt = datetime.fromisoformat(str(iso))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            if dt >= now:
                future_candidates.append((dt, row))
        except Exception:
            continue

    if future_candidates:
        future_candidates.sort(key=lambda x: x[0])
        return future_candidates[0][1]

    return rows[0]


def resolve_any_barber_by_rule(day_text: str, barbers: list[str], session=None) -> str | None:
    repo = get_sheets_repo()
    day = (day_text or "").strip()
    if not day:
        return None

    barbers_effective = [str(x).strip() for x in (barbers or []) if str(x).strip()]
    if session is not None:
        allowed = allowed_barbers_for_session(session)
        if allowed:
            allowed_lower = {x.lower() for x in allowed}
            barbers_effective = [b for b in barbers_effective if b.lower() in allowed_lower]

    candidates: list[tuple[int, str]] = []
    for b in barbers_effective:
        try:
            if _status_for(repo, b, day) != "working":
                continue
            load = _load_for(repo, b, day)
            candidates.append((int(load), b))
        except Exception:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[0], x[1].lower()))
    return candidates[0][1]


def maybe_resolve_any_barber(session) -> None:
    barber_txt = (session.draft.barber or "").strip().lower()
    if barber_txt in {"cualquiera", "cualquiera.", "cualquiera!"}:
        chosen_barber = resolve_any_barber_by_rule(
            session.draft.day_text,
            settings.BARBERS,
            session=session,
        )
        if chosen_barber:
            session.draft = merge_draft(
                session.draft,
                session.draft.__class__(barber=chosen_barber),
            )


def apply_ai_result(session, ai) -> None:
    pending_type = session.pending.type if session.pending else "none"

    if getattr(ai, "intent", None) and ai.intent != "unknown":
        session.intent = ai.intent
    elif pending_type in {"choose_new_slot", "confirm_reschedule"}:
        session.intent = "reschedule"
    elif pending_type in {"choose_cancel", "confirm_cancel"}:
        session.intent = "cancel"

    if getattr(ai, "draft_patch", None):
        session.draft = merge_draft(session.draft, ai.draft_patch)


def norm(s: str | None) -> str:
    return str(s or "").strip().lower()


def draft_snapshot(draft) -> dict:
    return {
        "customer_name": getattr(draft, "customer_name", None),
        "age": getattr(draft, "age", None),
        "barber": getattr(draft, "barber", None),
        "day_text": getattr(draft, "day_text", None),
        "time_hhmm": getattr(draft, "time_hhmm", None),
        "service_name": getattr(draft, "service_name", None),
        "service_key": getattr(draft, "service_key", None),
        "latest_finish_hhmm": getattr(draft, "latest_finish_hhmm", None),
    }


# =========================================================
# ETAPA 2: la verdad semántica pasa por la salida de la IA
# =========================================================

def ai_confirmation_state(ai) -> str:
    raw = str(getattr(ai, "confirmation_state", "none") or "none").strip().lower()
    if raw in {"confirm", "reject", "none"}:
        return raw
    return "none"


def ai_confirmed(ai) -> bool:
    return ai_confirmation_state(ai) == "confirm"


def ai_rejected(ai) -> bool:
    return ai_confirmation_state(ai) == "reject"


def _clean_option_id(value: Any) -> str | None:
    txt = str(value or "").strip()
    return txt or None


def build_pending_option_id(option: dict | None) -> str | None:
    if not isinstance(option, dict):
        return None

    explicit = _clean_option_id(option.get("option_id"))
    if explicit:
        return explicit

    row_id = option.get("id")
    if row_id not in (None, ""):
        return f"booking:{row_id}"

    barber = norm(option.get("barber"))
    day_text = norm(option.get("day_text") or option.get("date_text"))
    time_hhmm = str(option.get("time_hhmm") or "").strip()

    if barber or day_text or time_hhmm:
        return f"slot:{barber}|{day_text}|{time_hhmm}"

    customer_name = norm(option.get("customer_name"))
    if customer_name:
        return f"customer:{customer_name}"

    return None


def ensure_pending_option_ids(options: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()

    for opt in options or []:
        if not isinstance(opt, dict):
            continue

        row = dict(opt)
        option_id = build_pending_option_id(row)
        if option_id:
            row["option_id"] = option_id

        dedupe_key = option_id or repr(sorted(row.items()))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append(row)

    return out


def resolve_pending_option(session, ai) -> dict | None:
    pending = getattr(session, "pending", None)
    if not pending:
        return None

    options = ensure_pending_option_ids(getattr(pending, "options", None) or [])
    pending.options = options

    if not options:
        return None

    pending_resolution = getattr(ai, "pending_resolution", None)
    resolution_type = str(getattr(pending_resolution, "type", "none") or "none").strip().lower()
    option_id = _clean_option_id(getattr(pending_resolution, "option_id", None))
    action_type = str(getattr(getattr(ai, "action", None), "type", "none") or "none").strip().lower()

    if option_id:
        for opt in options:
            if _clean_option_id(opt.get("option_id")) == option_id:
                return opt
        return None

    # Modo transicional:
    # si solo hay una opción, aceptamos que la IA confirme o marque resolve_pending_choice
    # aunque todavía no haya devuelto option_id.
    if len(options) == 1 and (
        resolution_type == "pending_option"
        or action_type == "resolve_pending_choice"
        or ai_confirmed(ai)
    ):
        return options[0]

    return None