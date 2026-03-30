from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.actions.booking import reserve_slot, service_blocks, rgb_from_draft
from app.actions.cancel import cancel_booking
from app.core.types import Draft
from app.flows.double_booking_types import (
    DoubleBookingAssignment,
    DoubleBookingItem,
    DoubleBookingPlan,
    DoubleBookingSession,
)


def _item_for_slot(state: DoubleBookingSession, slot_id: str) -> DoubleBookingItem | None:
    for item in list(state.items or []):
        if item.slot_id == slot_id:
            return item
    return None


def _draft_for_assignment(
    assignment: DoubleBookingAssignment,
    item: DoubleBookingItem,
    holder_name: str | None,
) -> Draft:
    customer_name = (
        (item.customer_name or "").strip()
        or (item.person_label or "").strip()
        or (holder_name or "").strip()
        or f"persona {item.slot_id}"
    )
    return Draft(
        customer_name=customer_name,
        barber=(assignment.barber or "").strip(),
        day_text=(assignment.day_text or "").strip(),
        time_hhmm=(assignment.time_hhmm or "").strip(),
        service_name=(assignment.service_name or item.service_name or "").strip() or None,
        service_key=(assignment.service_key or item.service_key or "").strip() or None,
        age=item.age,
    )


@dataclass
class DoubleReserveResult:
    ok: bool
    bundle_id: Optional[str] = None
    bookings: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    reason: Optional[str] = None


def reserve_double_plan(
    *,
    phone: str,
    provider: str = "meta",
    state: DoubleBookingSession,
    plan: DoubleBookingPlan,
) -> DoubleReserveResult:
    """
    Reserva atómica lógica de un plan doble.

    1) Valida que el plan tenga 2 asignaciones.
    2) Genera un bundle_id único.
    3) Reserva la primera asignación via reserve_slot().
    4) Reserva la segunda asignación via reserve_slot().
    5) Si falla la segunda, revierte la primera via cancel_booking().
    6) Devuelve resultado con ambas reservas y bundle_id común.

    Reutiliza íntegramente la lógica de reserve_slot():
    validación, recheck live, paint Sheets, insert Supabase, rollback interno.
    """
    assignments = list(plan.assignments or [])
    if len(assignments) < 2:
        return DoubleReserveResult(
            ok=False,
            error="El plan no tiene dos asignaciones.",
            reason="invalid_plan",
        )

    bundle_id = f"DBL-{uuid.uuid4().hex[:12].upper()}"
    booked: List[Dict[str, Any]] = []
    first_booking_id: Optional[int] = None
    first_assignment: Optional[DoubleBookingAssignment] = None

    for idx, assignment in enumerate(assignments):
        item = _item_for_slot(state, assignment.slot_id)
        if not item:
            # Rollback del primer slot si ya se reservó
            if first_booking_id is not None and first_assignment is not None:
                try:
                    cancel_booking(
                        first_booking_id,
                        blocks_override=first_assignment.blocks,
                    )
                except Exception as e:
                    print(f"[DBG DOUBLE BOOKING ROLLBACK FAIL] booking_id={first_booking_id} err={e}")
            return DoubleReserveResult(
                ok=False,
                error=f"No encontré datos para el slot {assignment.slot_id}.",
                reason="invalid_state",
            )

        draft = _draft_for_assignment(assignment, item, state.holder_name)
        blocks = max(1, int(assignment.blocks or service_blocks(draft)))
        rgb = rgb_from_draft(draft)

        print(
            f"[DBG DOUBLE BOOKING RESERVE] slot={assignment.slot_id} "
            f"barber={assignment.barber} day={assignment.day_text} "
            f"time={assignment.time_hhmm} blocks={blocks} "
            f"customer={draft.customer_name}"
        )

        result = reserve_slot(
            draft=draft,
            phone=phone,
            provider=provider,
            blocks=blocks,
            rgb=rgb,
            extra_metadata={
                "bundle_id": bundle_id,
                "bundle_slot": assignment.slot_id,
                "bundle_mode": plan.mode,
            },
        )

        if not result.ok:
            print(
                f"[DBG DOUBLE BOOKING RESERVE FAIL] slot={assignment.slot_id} "
                f"reason={result.reason} error={result.error}"
            )
            # Si falla el segundo slot, revertir el primero
            if first_booking_id is not None and first_assignment is not None:
                try:
                    cancel_booking(
                        first_booking_id,
                        blocks_override=first_assignment.blocks,
                    )
                    print(f"[DBG DOUBLE BOOKING ROLLBACK OK] booking_id={first_booking_id}")
                except Exception as e:
                    print(f"[DBG DOUBLE BOOKING ROLLBACK FAIL] booking_id={first_booking_id} err={e}")

            return DoubleReserveResult(
                ok=False,
                error=result.error or "No se pudo reservar uno de los turnos.",
                reason=result.reason or "reserve_failed",
            )

        person_ref = (
            (item.person_label or "").strip()
            or (item.customer_name or "").strip()
            or f"persona {item.slot_id}"
        )

        booked.append({
            "booking_id": result.booking_id,
            "slot_id": assignment.slot_id,
            "person_ref": person_ref,
            "customer_name": draft.customer_name,
            "barber": assignment.barber,
            "day_text": assignment.day_text,
            "time_hhmm": assignment.time_hhmm,
            "service_name": assignment.service_name or item.service_name,
            "bundle_id": bundle_id,
        })

        if idx == 0:
            first_booking_id = result.booking_id
            first_assignment = assignment

    print(f"[DBG DOUBLE BOOKING RESERVE OK] bundle_id={bundle_id} bookings={len(booked)}")

    return DoubleReserveResult(
        ok=True,
        bundle_id=bundle_id,
        bookings=booked,
    )
