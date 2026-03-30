from __future__ import annotations

from app.actions.booking import get_day_availability
from app.core.utils import merge_draft
from app.repos.sheets_repo import get_sheets_repo
from app.actions.booking import _status_for
from app.flows.common import allowed_barbers_for_session


def candidate_barbers_for_day_context(session, requested_barber: str | None = None, all_barbers: list[str] | None = None) -> list[str]:
    requested = (requested_barber or getattr(session.draft, "barber", None) or "").strip()
    requested_lower = requested.lower()

    allowed = allowed_barbers_for_session(session)
    base = allowed or [str(x).strip() for x in (all_barbers or []) if str(x).strip()]

    ordered: list[str] = []
    seen: set[str] = set()
    for item in base:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)

    if requested and requested_lower not in {"cualquiera", "cualquiera.", "cualquiera!"}:
        if not ordered:
            return [requested]
        if requested_lower in {x.lower() for x in ordered}:
            return [next(x for x in ordered if x.lower() == requested_lower)]
        return ordered

    return ordered


def dedupe_slot_options(options: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for opt in options or []:
        if not isinstance(opt, dict):
            continue
        key = (
            str(opt.get("barber") or "").strip().lower(),
            str(opt.get("day_text") or opt.get("date_text") or "").strip().lower(),
            str(opt.get("time_hhmm") or "").strip(),
        )
        if not key[0] and not key[1] and not key[2]:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "barber": opt.get("barber"),
                "day_text": opt.get("day_text") or opt.get("date_text"),
                "time_hhmm": opt.get("time_hhmm"),
                "service_name": opt.get("service_name"),
                "service_key": opt.get("service_key") or opt.get("service_canonical"),
                "customer_name": opt.get("customer_name"),
                "metadata": opt.get("metadata"),
                "age": opt.get("age"),
            }
        )
    return out


def build_day_context_payload(
    session,
    *,
    requested_day: str | None = None,
    requested_barber: str | None = None,
    force_refresh: bool = True,
    all_barbers: list[str] | None = None,
) -> dict:
    day_text = (requested_day or getattr(session.draft, "day_text", None) or "").strip()
    barbers = candidate_barbers_for_day_context(
        session,
        requested_barber=requested_barber,
        all_barbers=all_barbers,
    )
    repo = get_sheets_repo()

    service_key = getattr(session.draft, "service_key", None)
    booking_stage = "service_fit_check" if service_key else "operational_check"

    payload = {
        "day_text": day_text or None,
        "requested_barber": (requested_barber or getattr(session.draft, "barber", None) or None),
        "requested_time": getattr(session.draft, "time_hhmm", None),
        "service_name": getattr(session.draft, "service_name", None),
        "service_key": service_key,
        "allowed_barbers": allowed_barbers_for_session(session),
        "booking_stage": booking_stage,
        "barbers": [],
        "selectable_slots": [],
    }

    if not day_text:
        return payload

    slots: list[dict] = []
    barbers_payload: list[dict] = []

    for barber in barbers:
        try:
            status = _status_for(repo, barber, day_text)
        except Exception:
            status = "working"

        free_times: list[str] = []

        if service_key:
            local_draft = merge_draft(
                session.draft,
                session.draft.__class__(barber=barber, day_text=day_text),
            )

            try:
                availability = get_day_availability(local_draft, force_refresh=force_refresh)
                free_times = list(getattr(availability, "free_times", []) or [])
            except Exception as e:
                print("[ERR BUILD DAY CONTEXT]", type(e).__name__, str(e))
                free_times = []

        barbers_payload.append(
            {
                "barber": barber,
                "day_text": day_text,
                "status": status,
                "free_times": free_times,
                "free_times_count": len(free_times),
            }
        )

        if service_key:
            for hhmm in free_times:
                slots.append(
                    {
                        "barber": barber,
                        "day_text": day_text,
                        "time_hhmm": hhmm,
                        "service_name": getattr(session.draft, "service_name", None),
                        "service_key": service_key,
                    }
                )

    payload["barbers"] = barbers_payload
    payload["selectable_slots"] = dedupe_slot_options(slots) if service_key else []
    return payload


def pending_choose_slot_options(offers_result, session, all_barbers: list[str] | None = None) -> list[dict]:
    requested_barber = getattr(offers_result, "requested_barber", None)
    requested_day = getattr(offers_result, "requested_day", None)

    day_context = build_day_context_payload(
        session,
        requested_day=requested_day,
        requested_barber=requested_barber,
        force_refresh=True,
        all_barbers=all_barbers,
    )

    merged: list[dict] = []
    merged.extend(day_context.get("selectable_slots") or [])
    merged.extend(getattr(offers_result, "offers", []) or [])
    merged.extend(getattr(offers_result, "next_same_barber_offers", []) or [])
    return dedupe_slot_options(merged)


def day_availability_sys_event(
    session,
    *,
    requested_day: str | None = None,
    requested_barber: str | None = None,
    event_name: str = "SISTEMA_DAY_AVAILABILITY",
    all_barbers: list[str] | None = None,
) -> str:
    payload = build_day_context_payload(
        session,
        requested_day=requested_day,
        requested_barber=requested_barber,
        force_refresh=True,
        all_barbers=all_barbers,
    )
    return f"{event_name}: {payload}"