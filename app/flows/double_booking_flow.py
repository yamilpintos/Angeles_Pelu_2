from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterable, List, Optional, Tuple

from app.actions.booking import (
    _add_minutes_hhmm,
    _hhmm_to_minutes,
    _load_for,
    _normalize_hhmm,
    _resolve_effective_blocks_for_slot,
    _status_for,
    get_day_availability,
    recheck_slot_live,
    service_blocks,
)
from app.repos.sheets_repo import get_sheets_repo
from app.core.catalog import allowed_barbers_for
from app.flows.double_booking_types import (
    DoubleBookingAssignment,
    DoubleBookingItem,
    DoubleBookingPlan,
    DoubleBookingSession,
)


@dataclass(frozen=True)
class _SerialCandidate:
    barber: str
    first_slot_id: str
    second_slot_id: str
    first_time: str
    second_time: str
    first_blocks: int
    second_blocks: int


def _norm_text(value: str) -> str:
    return str(value or "").strip().lower()


def _is_any_barber(value: str | None) -> bool:
    raw = _norm_text(value or "")
    return raw in {
        "",
        "cualquiera",
        "cualquiera de los dos",
        "cualquiera de los peluqueros",
        "el que tenga lugar",
        "el que esté",
        "indistinto",
        "me da igual",
        "da igual",
        "any",
    }


def _person_ref(item: DoubleBookingItem) -> str:
    if item.person_label and str(item.person_label).strip():
        return str(item.person_label).strip()
    if item.customer_name and str(item.customer_name).strip():
        return str(item.customer_name).strip()
    return f"persona {item.slot_id}"


def _build_temp_draft(
    *,
    item: DoubleBookingItem,
    day_text: str,
    time_hhmm: str,
    barber: str,
):
    return SimpleNamespace(
        customer_name=(item.customer_name or _person_ref(item)),
        barber=(barber or "").strip(),
        day_text=(day_text or "").strip(),
        time_hhmm=(time_hhmm or "").strip(),
        service_name=(item.service_name or "").strip() or None,
        service_key=(item.service_key or "").strip() or None,
        age=item.age,
    )


def _service_ready(item: DoubleBookingItem) -> bool:
    return bool((item.service_key or "").strip() or (item.service_name or "").strip())


def _candidate_barbers_for_item(
    item: DoubleBookingItem,
    *,
    day_text: str,
    all_barbers: list[str],
) -> list[str]:
    clean_all = [str(x).strip() for x in (all_barbers or []) if str(x).strip()]
    if not clean_all:
        return []

    allowed = None
    service_key = (item.service_key or "").strip()
    if service_key:
        try:
            allowed = allowed_barbers_for(service_key)
        except Exception:
            allowed = None

    explicit = (item.barber or "").strip()
    if explicit and not _is_any_barber(explicit):
        if allowed and explicit.lower() not in {x.lower() for x in allowed}:
            return []
        return [explicit]

    if allowed:
        allowed_lower = {x.lower() for x in allowed}
        clean_all = [b for b in clean_all if b.lower() in allowed_lower]

    repo = get_sheets_repo()
    ranked: list[tuple[int, int, str]] = []

    for barber in clean_all:
        try:
            st = _status_for(repo, barber, day_text)
        except Exception:
            st = "working"

        if st in {"absent", "vacation", "off"}:
            continue

        try:
            load = int(_load_for(repo, barber, day_text))
        except Exception:
            load = 0

        ranked.append((0 if st == "working" else 1, load, barber))

    ranked.sort(key=lambda x: (x[0], x[1], x[2].lower()))
    return [barber for _, _, barber in ranked]


def _availability_times_for_item(
    item: DoubleBookingItem,
    *,
    day_text: str,
    barber: str,
) -> list[str]:
    if not _service_ready(item):
        return []

    draft = _build_temp_draft(
        item=item,
        day_text=day_text,
        time_hhmm="12:00",
        barber=barber,
    )

    try:
        avail = get_day_availability(draft, force_refresh=True)
        free_times = list(getattr(avail, "free_times", []) or [])
    except Exception:
        return []

    normalized: list[str] = []
    for value in free_times:
        try:
            normalized.append(_normalize_hhmm(value))
        except Exception:
            continue

    # dedupe preservando orden
    seen: set[str] = set()
    out: list[str] = []
    for hhmm in normalized:
        if hhmm in seen:
            continue
        seen.add(hhmm)
        out.append(hhmm)

    return out


def _preferred_minutes(state: DoubleBookingSession) -> Optional[int]:
    raw = (state.preferred_time_hhmm or "").strip()
    if not raw:
        return None
    try:
        return _hhmm_to_minutes(raw)
    except Exception:
        return None


def _time_distance_from_preference(hhmm: str, preferred_minutes: Optional[int]) -> int:
    if preferred_minutes is None:
        return 0
    try:
        return abs(_hhmm_to_minutes(hhmm) - preferred_minutes)
    except Exception:
        return 999999


def _state_items(state: DoubleBookingSession) -> tuple[DoubleBookingItem, DoubleBookingItem] | None:
    items = list(state.items or [])
    if len(items) < 2:
        return None
    return items[0], items[1]


def missing_double_booking_fields(state: DoubleBookingSession) -> list[str]:
    missing: list[str] = []

    if not (state.day_text or "").strip():
        missing.append("día")

    items = list(state.items or [])
    if len(items) < 2:
        missing.append("segunda persona")
        return missing

    item_a = items[0]
    item_b = items[1]

    if not _service_ready(item_a):
        missing.append("servicio de la primera persona")
    if not _service_ready(item_b):
        missing.append("servicio de la segunda persona")

    return missing


def has_minimum_double_booking_data(state: DoubleBookingSession) -> bool:
    return len(missing_double_booking_fields(state)) == 0


def _effective_blocks(
    item: DoubleBookingItem,
    *,
    day_text: str,
    barber: str,
    time_hhmm: str,
) -> int:
    draft = _build_temp_draft(
        item=item,
        day_text=day_text,
        time_hhmm=time_hhmm,
        barber=barber,
    )
    base_blocks = max(1, int(service_blocks(draft) or 1))

    try:
        return max(
            1,
            int(
                _resolve_effective_blocks_for_slot(
                    draft,
                    time_hhmm=time_hhmm,
                    requested_blocks=base_blocks,
                    ignore_range=None,
                )
                or base_blocks
            ),
        )
    except Exception:
        return base_blocks


def _slot_ok(
    item: DoubleBookingItem,
    *,
    day_text: str,
    barber: str,
    time_hhmm: str,
) -> tuple[bool, int]:
    draft = _build_temp_draft(
        item=item,
        day_text=day_text,
        time_hhmm=time_hhmm,
        barber=barber,
    )
    blocks = _effective_blocks(
        item,
        day_text=day_text,
        barber=barber,
        time_hhmm=time_hhmm,
    )
    ok = bool(
        recheck_slot_live(
            draft,
            time_hhmm=time_hhmm,
            blocks=blocks,
        )
    )
    return ok, blocks


def _parallel_summary(
    item_a: DoubleBookingItem,
    item_b: DoubleBookingItem,
    *,
    day_text: str,
    time_hhmm: str,
    barber_a: str,
    barber_b: str,
) -> str:
    return (
        f"En paralelo el {day_text} a las {time_hhmm}: "
        f"{_person_ref(item_a)} con {barber_a} y {_person_ref(item_b)} con {barber_b}."
    )


def _serial_summary(
    item_a: DoubleBookingItem,
    item_b: DoubleBookingItem,
    *,
    day_text: str,
    barber: str,
    first_slot_id: str,
    second_slot_id: str,
    first_time: str,
    second_time: str,
) -> str:
    first_ref = _person_ref(item_a if first_slot_id == "A" else item_b)
    second_ref = _person_ref(item_a if second_slot_id == "A" else item_b)
    return (
        f"En serie el {day_text} con {barber}: "
        f"{first_ref} a las {first_time} y {second_ref} a las {second_time}."
    )


def _dedupe_parallel_key(
    *,
    day_text: str,
    time_hhmm: str,
    barber_a: str,
    barber_b: str,
) -> tuple[str, str, str, str]:
    ordered = sorted([barber_a.strip().lower(), barber_b.strip().lower()])
    return (
        day_text.strip().lower(),
        time_hhmm.strip(),
        ordered[0],
        ordered[1],
    )


def _dedupe_serial_key(
    *,
    day_text: str,
    barber: str,
    first_slot_id: str,
    second_slot_id: str,
    first_time: str,
    second_time: str,
) -> tuple[str, str, str, str, str, str]:
    return (
        day_text.strip().lower(),
        barber.strip().lower(),
        first_slot_id,
        second_slot_id,
        first_time,
        second_time,
    )


def build_parallel_plans(
    state: DoubleBookingSession,
    *,
    all_barbers: list[str],
    max_plans: int = 4,
) -> list[DoubleBookingPlan]:
    items = _state_items(state)
    if not items:
        return []

    item_a, item_b = items
    day_text = (state.day_text or "").strip()
    if not day_text:
        return []

    barbers_a = _candidate_barbers_for_item(item_a, day_text=day_text, all_barbers=all_barbers)
    barbers_b = _candidate_barbers_for_item(item_b, day_text=day_text, all_barbers=all_barbers)
    preferred_minutes = _preferred_minutes(state)

    candidates: list[tuple[tuple[int, int, str, str], DoubleBookingPlan]] = []
    seen: set[tuple[str, str, str, str]] = set()

    for barber_a in barbers_a:
        times_a = _availability_times_for_item(item_a, day_text=day_text, barber=barber_a)
        if not times_a:
            continue

        for barber_b in barbers_b:
            if barber_a.strip().lower() == barber_b.strip().lower():
                continue

            times_b = _availability_times_for_item(item_b, day_text=day_text, barber=barber_b)
            if not times_b:
                continue

            common = sorted(set(times_a).intersection(times_b), key=_hhmm_to_minutes)

            for hhmm in common:
                key = _dedupe_parallel_key(
                    day_text=day_text,
                    time_hhmm=hhmm,
                    barber_a=barber_a,
                    barber_b=barber_b,
                )
                if key in seen:
                    continue

                ok_a, blocks_a = _slot_ok(item_a, day_text=day_text, barber=barber_a, time_hhmm=hhmm)
                if not ok_a:
                    continue

                ok_b, blocks_b = _slot_ok(item_b, day_text=day_text, barber=barber_b, time_hhmm=hhmm)
                if not ok_b:
                    continue

                seen.add(key)

                plan = DoubleBookingPlan(
                    plan_id="",
                    mode="parallel",
                    day_text=day_text,
                    summary=_parallel_summary(
                        item_a,
                        item_b,
                        day_text=day_text,
                        time_hhmm=hhmm,
                        barber_a=barber_a,
                        barber_b=barber_b,
                    ),
                    assignments=[
                        DoubleBookingAssignment(
                            slot_id="A",
                            barber=barber_a,
                            day_text=day_text,
                            time_hhmm=hhmm,
                            service_name=item_a.service_name,
                            service_key=item_a.service_key,
                            blocks=blocks_a,
                            metadata={
                                "person_ref": _person_ref(item_a),
                            },
                        ),
                        DoubleBookingAssignment(
                            slot_id="B",
                            barber=barber_b,
                            day_text=day_text,
                            time_hhmm=hhmm,
                            service_name=item_b.service_name,
                            service_key=item_b.service_key,
                            blocks=blocks_b,
                            metadata={
                                "person_ref": _person_ref(item_b),
                            },
                        ),
                    ],
                    metadata={
                        "preferred_mode": state.mode_preference,
                        "score_time_distance": _time_distance_from_preference(hhmm, preferred_minutes),
                    },
                )
                score = (
                    _time_distance_from_preference(hhmm, preferred_minutes),
                    _hhmm_to_minutes(hhmm),
                    barber_a.lower(),
                    barber_b.lower(),
                )
                candidates.append((score, plan))

    candidates.sort(key=lambda x: x[0])

    out: list[DoubleBookingPlan] = []
    for _, plan in candidates[: max(1, int(max_plans or 1))]:
        out.append(plan)
    return out


def _build_serial_candidate(
    *,
    first_item: DoubleBookingItem,
    second_item: DoubleBookingItem,
    first_slot_id: str,
    second_slot_id: str,
    day_text: str,
    barber: str,
    first_time: str,
) -> _SerialCandidate | None:
    ok_first, first_blocks = _slot_ok(
        first_item,
        day_text=day_text,
        barber=barber,
        time_hhmm=first_time,
    )
    if not ok_first:
        return None

    second_time = _add_minutes_hhmm(first_time, first_blocks * 30)

    ok_second, second_blocks = _slot_ok(
        second_item,
        day_text=day_text,
        barber=barber,
        time_hhmm=second_time,
    )
    if not ok_second:
        return None

    return _SerialCandidate(
        barber=barber,
        first_slot_id=first_slot_id,
        second_slot_id=second_slot_id,
        first_time=first_time,
        second_time=second_time,
        first_blocks=first_blocks,
        second_blocks=second_blocks,
    )


def build_serial_same_barber_plans(
    state: DoubleBookingSession,
    *,
    all_barbers: list[str],
    max_plans: int = 4,
) -> list[DoubleBookingPlan]:
    items = _state_items(state)
    if not items:
        return []

    item_a, item_b = items
    day_text = (state.day_text or "").strip()
    if not day_text:
        return []

    barbers_a = set(_candidate_barbers_for_item(item_a, day_text=day_text, all_barbers=all_barbers))
    barbers_b = set(_candidate_barbers_for_item(item_b, day_text=day_text, all_barbers=all_barbers))
    shared_barbers = sorted(barbers_a.intersection(barbers_b), key=lambda x: x.lower())
    preferred_minutes = _preferred_minutes(state)

    candidates: list[tuple[tuple[int, int, str], DoubleBookingPlan]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()

    for barber in shared_barbers:
        times_a = _availability_times_for_item(item_a, day_text=day_text, barber=barber)
        times_b = _availability_times_for_item(item_b, day_text=day_text, barber=barber)

        for first_time in times_a:
            serial = _build_serial_candidate(
                first_item=item_a,
                second_item=item_b,
                first_slot_id="A",
                second_slot_id="B",
                day_text=day_text,
                barber=barber,
                first_time=first_time,
            )
            if serial is None:
                continue

            key = _dedupe_serial_key(
                day_text=day_text,
                barber=barber,
                first_slot_id=serial.first_slot_id,
                second_slot_id=serial.second_slot_id,
                first_time=serial.first_time,
                second_time=serial.second_time,
            )
            if key in seen:
                continue

            seen.add(key)

            plan = DoubleBookingPlan(
                plan_id="",
                mode="serial",
                day_text=day_text,
                summary=_serial_summary(
                    item_a,
                    item_b,
                    day_text=day_text,
                    barber=barber,
                    first_slot_id=serial.first_slot_id,
                    second_slot_id=serial.second_slot_id,
                    first_time=serial.first_time,
                    second_time=serial.second_time,
                ),
                assignments=[
                    DoubleBookingAssignment(
                        slot_id=serial.first_slot_id,
                        barber=barber,
                        day_text=day_text,
                        time_hhmm=serial.first_time,
                        service_name=(item_a.service_name if serial.first_slot_id == "A" else item_b.service_name),
                        service_key=(item_a.service_key if serial.first_slot_id == "A" else item_b.service_key),
                        blocks=serial.first_blocks,
                        metadata={
                            "person_ref": _person_ref(item_a if serial.first_slot_id == "A" else item_b),
                            "sequence": 1,
                        },
                    ),
                    DoubleBookingAssignment(
                        slot_id=serial.second_slot_id,
                        barber=barber,
                        day_text=day_text,
                        time_hhmm=serial.second_time,
                        service_name=(item_a.service_name if serial.second_slot_id == "A" else item_b.service_name),
                        service_key=(item_a.service_key if serial.second_slot_id == "A" else item_b.service_key),
                        blocks=serial.second_blocks,
                        metadata={
                            "person_ref": _person_ref(item_a if serial.second_slot_id == "A" else item_b),
                            "sequence": 2,
                        },
                    ),
                ],
                metadata={
                    "order": "A_then_B",
                    "preferred_mode": state.mode_preference,
                    "score_time_distance": _time_distance_from_preference(serial.first_time, preferred_minutes),
                },
            )
            score = (
                _time_distance_from_preference(serial.first_time, preferred_minutes),
                _hhmm_to_minutes(serial.first_time),
                barber.lower(),
            )
            candidates.append((score, plan))

        for first_time in times_b:
            serial = _build_serial_candidate(
                first_item=item_b,
                second_item=item_a,
                first_slot_id="B",
                second_slot_id="A",
                day_text=day_text,
                barber=barber,
                first_time=first_time,
            )
            if serial is None:
                continue

            key = _dedupe_serial_key(
                day_text=day_text,
                barber=barber,
                first_slot_id=serial.first_slot_id,
                second_slot_id=serial.second_slot_id,
                first_time=serial.first_time,
                second_time=serial.second_time,
            )
            if key in seen:
                continue

            seen.add(key)

            plan = DoubleBookingPlan(
                plan_id="",
                mode="serial",
                day_text=day_text,
                summary=_serial_summary(
                    item_a,
                    item_b,
                    day_text=day_text,
                    barber=barber,
                    first_slot_id=serial.first_slot_id,
                    second_slot_id=serial.second_slot_id,
                    first_time=serial.first_time,
                    second_time=serial.second_time,
                ),
                assignments=[
                    DoubleBookingAssignment(
                        slot_id=serial.first_slot_id,
                        barber=barber,
                        day_text=day_text,
                        time_hhmm=serial.first_time,
                        service_name=(item_b.service_name if serial.first_slot_id == "B" else item_a.service_name),
                        service_key=(item_b.service_key if serial.first_slot_id == "B" else item_a.service_key),
                        blocks=serial.first_blocks,
                        metadata={
                            "person_ref": _person_ref(item_b if serial.first_slot_id == "B" else item_a),
                            "sequence": 1,
                        },
                    ),
                    DoubleBookingAssignment(
                        slot_id=serial.second_slot_id,
                        barber=barber,
                        day_text=day_text,
                        time_hhmm=serial.second_time,
                        service_name=(item_a.service_name if serial.second_slot_id == "A" else item_b.service_name),
                        service_key=(item_a.service_key if serial.second_slot_id == "A" else item_b.service_key),
                        blocks=serial.second_blocks,
                        metadata={
                            "person_ref": _person_ref(item_a if serial.second_slot_id == "A" else item_b),
                            "sequence": 2,
                        },
                    ),
                ],
                metadata={
                    "order": "B_then_A",
                    "preferred_mode": state.mode_preference,
                    "score_time_distance": _time_distance_from_preference(serial.first_time, preferred_minutes),
                },
            )
            score = (
                _time_distance_from_preference(serial.first_time, preferred_minutes),
                _hhmm_to_minutes(serial.first_time),
                barber.lower(),
            )
            candidates.append((score, plan))

    candidates.sort(key=lambda x: x[0])

    out: list[DoubleBookingPlan] = []
    for _, plan in candidates[: max(1, int(max_plans or 1))]:
        out.append(plan)
    return out


def _mode_priority(state: DoubleBookingSession) -> list[str]:
    mode = _norm_text(state.mode_preference or "parallel_first")
    if mode == "parallel":
        return ["parallel", "serial"]
    if mode == "serial":
        return ["serial", "parallel"]
    if mode == "indifferent":
        return ["parallel", "serial"]
    return ["parallel", "serial"]


def _assign_plan_ids(plans: Iterable[DoubleBookingPlan]) -> list[DoubleBookingPlan]:
    out: list[DoubleBookingPlan] = []
    for idx, plan in enumerate(plans, start=1):
        payload = plan.model_copy(deep=True)
        payload.plan_id = f"DBP-{idx}"
        out.append(payload)
    return out


def build_candidate_plans(
    state: DoubleBookingSession,
    *,
    all_barbers: list[str],
    max_plans: int = 5,
) -> list[DoubleBookingPlan]:
    if not has_minimum_double_booking_data(state):
        return []

    max_plans = max(1, int(max_plans or 1))

    parallel = build_parallel_plans(
        state,
        all_barbers=all_barbers,
        max_plans=max_plans,
    )
    serial = build_serial_same_barber_plans(
        state,
        all_barbers=all_barbers,
        max_plans=max_plans,
    )

    ordered: list[DoubleBookingPlan] = []
    for mode_name in _mode_priority(state):
        if mode_name == "parallel":
            ordered.extend(parallel)
        elif mode_name == "serial":
            ordered.extend(serial)

    # dedupe final por contenido operativo
    seen: set[tuple[Any, ...]] = set()
    unique: list[DoubleBookingPlan] = []

    for plan in ordered:
        key = (
            plan.mode,
            plan.day_text,
            tuple(
                (
                    a.slot_id,
                    a.barber,
                    a.day_text,
                    a.time_hhmm,
                    a.blocks,
                    a.service_key,
                )
                for a in plan.assignments
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(plan)
        if len(unique) >= max_plans:
            break

    return _assign_plan_ids(unique)


def apply_candidate_plans_to_state(
    state: DoubleBookingSession,
    *,
    all_barbers: list[str],
    max_plans: int = 5,
) -> DoubleBookingSession:
    plans = build_candidate_plans(
        state,
        all_barbers=all_barbers,
        max_plans=max_plans,
    )
    updated = state.model_copy(deep=True)
    updated.offered_plans = plans
    updated.stage = "choose_plan" if plans else "planning"
    updated.selected_plan_id = None
    return updated


def get_selected_plan(
    state: DoubleBookingSession,
    plan_id: str,
) -> Optional[DoubleBookingPlan]:
    wanted = (plan_id or "").strip().lower()
    if not wanted:
        return None

    for plan in list(state.offered_plans or []):
        if (plan.plan_id or "").strip().lower() == wanted:
            return plan
    return None


def format_plan_option(plan: DoubleBookingPlan) -> str:
    if plan.summary and str(plan.summary).strip():
        return str(plan.summary).strip()

    if plan.mode == "parallel":
        parts = []
        for a in plan.assignments:
            parts.append(
                f"{a.metadata.get('person_ref') or a.slot_id} con {a.barber} a las {a.time_hhmm}"
            )
        return f"En paralelo el {plan.day_text}: " + " y ".join(parts) + "."

    ordered = sorted(
        list(plan.assignments or []),
        key=lambda a: int((a.metadata or {}).get("sequence", 99)),
    )
    parts = []
    for a in ordered:
        parts.append(f"{a.metadata.get('person_ref') or a.slot_id} a las {a.time_hhmm}")
    barber = ordered[0].barber if ordered else ""
    return f"En serie el {plan.day_text} con {barber}: " + " y ".join(parts) + "."


def plans_to_pending_options(plans: list[DoubleBookingPlan]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    for plan in plans:
        out.append(
            {
                "type": "double_booking_plan",
                "plan_id": plan.plan_id,
                "label": format_plan_option(plan),
                "mode": plan.mode,
                "day_text": plan.day_text,
                "summary": plan.summary,
                "assignments": [a.model_dump() for a in list(plan.assignments or [])],
            }
        )

    return out