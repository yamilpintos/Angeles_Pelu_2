from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


DoubleBookingConfirmationState = Literal["none", "confirm", "reject"]


class DoubleBookingItemPatch(BaseModel):
    slot_id: Optional[Literal["A", "B"]] = None

    person_label: Optional[str] = None
    customer_name: Optional[str] = None
    age: Optional[int] = None

    service_name: Optional[str] = None
    service_key: Optional[str] = None

    barber: Optional[str] = None


class DoubleBookingDraftPatch(BaseModel):
    holder_name: Optional[str] = None
    day_text: Optional[str] = None
    preferred_time_hhmm: Optional[str] = None

    mode_preference: Optional[
        Literal["parallel_first", "parallel", "serial", "indifferent"]
    ] = None

    item_a: DoubleBookingItemPatch = Field(default_factory=DoubleBookingItemPatch)
    item_b: DoubleBookingItemPatch = Field(default_factory=DoubleBookingItemPatch)


class DoubleBookingAction(BaseModel):
    type: Literal[
        "none",
        "collect_double_booking_data",
        "build_candidate_plans",
        "choose_plan",
        "confirm_double_booking",
        "exit_double_booking",
        "fallback_to_general",
    ] = "none"

    plan_id: Optional[str] = None
    reason: Optional[str] = None


class DoubleBookingPendingResolution(BaseModel):
    type: Literal[
        "none",
        "select_plan",
    ] = "none"

    plan_id: Optional[str] = None


class DoubleBookingReply(BaseModel):
    draft_patch: DoubleBookingDraftPatch = Field(default_factory=DoubleBookingDraftPatch)
    action: DoubleBookingAction = Field(default_factory=DoubleBookingAction)

    confirmation_state: DoubleBookingConfirmationState = "none"
    pending_resolution: DoubleBookingPendingResolution = Field(
        default_factory=DoubleBookingPendingResolution
    )

    reply_text: str = ""