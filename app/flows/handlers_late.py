from __future__ import annotations

from app.flows.common import pick_relevant_booking_for_late
from app.flows.replying import reply_with_event
from app.repos.bookings_repo import get_bookings_repo


def handle_late_arrival(phone: str, session, ai, text: str) -> bool:
    if not ai.action or ai.action.type != "handle_late_arrival":
        return False

    bookings_repo = get_bookings_repo()
    rows = bookings_repo.list_active_by_phone(phone)

    if not rows:
        reply_with_event(phone, session, "SISTEMA_LATE_NO_ACTIVE_BOOKING", text)
        return True

    chosen = pick_relevant_booking_for_late(rows)
    if not chosen:
        reply_with_event(phone, session, "SISTEMA_LATE_AMBIGUOUS_BOOKING", text)
        return True

    late_minutes = getattr(ai.action, "late_minutes", None)
    session.last_booking_id = str(chosen.get("id") or "")

    if late_minutes is None:
        reply_with_event(phone, session, "SISTEMA_LATE_ASK_MINUTES", text)
        return True

    if late_minutes <= 15:
        reply_with_event(phone, session, f"SISTEMA_LATE_OK: {{'late_minutes': {late_minutes}}}", text)
        return True

    reply_with_event(phone, session, f"SISTEMA_LATE_OVER_LIMIT: {{'late_minutes': {late_minutes}}}", text)
    return True