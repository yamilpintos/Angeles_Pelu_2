from __future__ import annotations

from app.core.utils import merge_draft
from app.flows.pending_helpers import set_pending
from app.flows.replying import reply_with_event
from app.repos.bookings_repo import get_bookings_repo


def cancel_options_sys_event(rows: list[dict]) -> str:
    return f"SISTEMA_CANCEL_OPTIONS: {rows}"


def cancel_confirm_sys_event(row: dict) -> str:
    return f"SISTEMA_CANCEL_CONFIRM: {row}"


def start_cancel_flow(phone: str, session, text: str) -> bool:
    bookings_repo = get_bookings_repo()
    rows = bookings_repo.list_active_by_phone(phone)

    session.intent = "cancel"
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

    if len(rows) == 1:
        row = rows[0]
        session.last_booking_id = str(row.get("id") or "")
        set_pending(session, "confirm_cancel", [])
        patch = session.draft.__class__(
            customer_name=row.get("customer_name"),
            barber=row.get("barber"),
            day_text=row.get("date_text") or row.get("day_text"),
            time_hhmm=row.get("time_hhmm"),
            service_name=row.get("service_name"),
            service_key=row.get("service_key") or row.get("service_canonical"),
            age=(row.get("metadata") or {}).get("age") or row.get("age"),
            latest_finish_hhmm=None,
        )
        session.draft = merge_draft(session.draft, patch)
        reply_with_event(phone, session, cancel_confirm_sys_event(row), text)
        return True

    set_pending(session, "choose_cancel", rows)
    reply_with_event(phone, session, cancel_options_sys_event(rows), text)
    return True