from __future__ import annotations

from app.core.session_store import save_session
from app.flows.pending_helpers import set_pending
from app.flows.replying import reply_async, reply_with_event
from app.repos.bookings_repo import get_bookings_repo


def _safe_int(value, default=None):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _extract_coords(row: dict, metadata: dict) -> dict:
    """
    Devuelve las coordenadas reales del turno viejo en el sheet.
    Prioridad:
    1) top-level row
    2) metadata directo
    3) metadata.sheet_coords / metadata.sheet / metadata.paint
    """
    nested_candidates = [
        metadata.get("sheet_coords"),
        metadata.get("sheet"),
        metadata.get("paint"),
    ]

    nested = {}
    for candidate in nested_candidates:
        if isinstance(candidate, dict) and candidate:
            nested = candidate
            break

    tab = (
        row.get("tab")
        or metadata.get("tab")
        or nested.get("tab")
    )

    sheet_id = _safe_int(
        row.get("sheet_id"),
        _safe_int(metadata.get("sheet_id"), _safe_int(nested.get("sheet_id"))),
    )
    row_idx = _safe_int(
        row.get("row"),
        _safe_int(metadata.get("row"), _safe_int(nested.get("row"))),
    )
    col = _safe_int(
        row.get("col"),
        _safe_int(metadata.get("col"), _safe_int(nested.get("col"))),
    )
    blocks = _safe_int(
        row.get("blocks"),
        _safe_int(metadata.get("blocks"), _safe_int(nested.get("blocks"), 1)),
    )

    if blocks is None or blocks <= 0:
        blocks = 1

    return {
        "tab": str(tab).strip() if tab else None,
        "sheet_id": sheet_id,
        "row": row_idx,
        "col": col,
        "blocks": blocks,
    }


def build_selected_reschedule_anchor(row: dict) -> dict:
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    day_text = row.get("date_text") or row.get("day_text")
    coords = _extract_coords(row, metadata)

    return {
        "__selected_booking__": True,
        "id": row.get("id"),
        "customer_name": row.get("customer_name"),
        "barber": row.get("barber"),
        "day_text": day_text,
        "date_text": day_text,
        "date_iso": row.get("date_iso"),
        "time_hhmm": row.get("time_hhmm"),
        "service_name": row.get("service_name"),
        "service_key": row.get("service_key") or row.get("service_canonical"),
        "age": metadata.get("age") or row.get("age"),
        "tab": coords.get("tab"),
        "sheet_id": coords.get("sheet_id"),
        "row": coords.get("row"),
        "col": coords.get("col"),
        "blocks": coords.get("blocks"),
    }


def _format_reschedule_option(idx: int, row: dict) -> str:
    customer_name = row.get("customer_name") or "Cliente"
    day_text = row.get("date_text") or row.get("day_text") or "día sin fecha"
    time_hhmm = row.get("time_hhmm") or "hora sin definir"
    barber = row.get("barber") or "peluquero sin definir"
    service = row.get("service_name") or "servicio sin definir"
    return f"{idx}. {customer_name} - {day_text} a las {time_hhmm} con {barber} ({service})"


def reschedule_options_sys_event(rows: list[dict]) -> str:
    formatted = "\n".join(
        _format_reschedule_option(idx, row)
        for idx, row in enumerate(rows, start=1)
    )
    return (
        "SISTEMA_RESCHEDULE_OPTIONS:\n"
        f"{formatted}\n"
        "Respondé con el número del turno que querés reprogramar."
    )


def start_reschedule_flow(phone: str, session, text: str) -> bool:
    bookings_repo = get_bookings_repo()
    rows = bookings_repo.list_active_by_phone(phone) or []

    print("[DBG RESCHEDULE ROWS COUNT]", len(rows))
    for i, row in enumerate(rows, start=1):
        metadata = row.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        coords = _extract_coords(row, metadata)

        print(
            "[DBG RESCHEDULE ROW]",
            i,
            {
                "id": row.get("id"),
                "customer_name": row.get("customer_name"),
                "barber": row.get("barber"),
                "date_text": row.get("date_text") or row.get("day_text"),
                "date_iso": row.get("date_iso"),
                "time_hhmm": row.get("time_hhmm"),
                "service_name": row.get("service_name"),
                "service_key": row.get("service_key") or row.get("service_canonical"),
                "status": row.get("status"),
                "phone": row.get("phone"),
                "starts_at": row.get("starts_at"),
                "created_at": row.get("created_at"),
                "tab": coords.get("tab"),
                "sheet_id": coords.get("sheet_id"),
                "row": coords.get("row"),
                "col": coords.get("col"),
                "blocks": coords.get("blocks"),
            },
        )

    session.intent = "reschedule"
    session.last_booking_id = ""
    set_pending(session, "none", [])
    session.draft = session.draft.__class__(
        customer_name=None,
        barber=None,
        day_text=None,
        time_hhmm=None,
        service_name=None,
        service_key=None,
        age=None,
        latest_finish_hhmm=None,
    )

    if not rows:
        reply_with_event(phone, session, "SISTEMA_NO_ACTIVE_BOOKINGS", text)
        return True

    def _row_sort_key(row: dict):
        starts_at = row.get("starts_at")
        if starts_at:
            return ("1", str(starts_at))

        date_iso = row.get("date_iso")
        time_hhmm = row.get("time_hhmm")
        if date_iso and time_hhmm:
            return ("2", f"{date_iso}T{time_hhmm}")

        return ("3", str(row.get("created_at") or ""))

    rows = sorted(rows, key=_row_sort_key)

    print("[DBG RESCHEDULE ROWS SORTED]")
    for i, row in enumerate(rows, start=1):
        metadata = row.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        coords = _extract_coords(row, metadata)

        print(
            "[DBG RESCHEDULE ROW SORTED]",
            i,
            {
                "id": row.get("id"),
                "date_text": row.get("date_text") or row.get("day_text"),
                "date_iso": row.get("date_iso"),
                "time_hhmm": row.get("time_hhmm"),
                "barber": row.get("barber"),
                "starts_at": row.get("starts_at"),
                "tab": coords.get("tab"),
                "sheet_id": coords.get("sheet_id"),
                "row": coords.get("row"),
                "col": coords.get("col"),
                "blocks": coords.get("blocks"),
            },
        )

    if len(rows) == 1:
        row = rows[0]
        metadata = row.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        selected_anchor = build_selected_reschedule_anchor(row)

        session.last_booking_id = str(row.get("id") or "")
        set_pending(session, "choose_new_slot", [selected_anchor])
        session.draft = session.draft.__class__(
            customer_name=row.get("customer_name"),
            barber=row.get("barber"),
            day_text=None,
            time_hhmm=None,
            service_name=row.get("service_name"),
            service_key=row.get("service_key") or row.get("service_canonical"),
            age=metadata.get("age") or row.get("age"),
            latest_finish_hhmm=None,
        )
        reply_with_event(phone, session, "SISTEMA_RESCHEDULE_CHOOSE_NEW_TIME", text)
        return True

    set_pending(session, "choose_reschedule", rows)
    save_session(phone, session)
    reply_async(
        phone,
        reschedule_options_sys_event(rows).replace("SISTEMA_RESCHEDULE_OPTIONS:\n", ""),
    )
    return True

    