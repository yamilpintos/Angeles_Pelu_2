from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


DoubleBookingStage = Literal[
    "collecting",
    "planning",
    "choose_plan",
    "confirming",
    "completed",
    "cancelled",
]

DoubleBookingModePreference = Literal[
    "parallel_first",
    "parallel",
    "serial",
    "indifferent",
]

DoubleBookingPlanMode = Literal[
    "parallel",
    "serial",
]


class DoubleBookingItem(BaseModel):
    slot_id: str
    person_label: Optional[str] = None

    customer_name: Optional[str] = None
    age: Optional[int] = None

    service_name: Optional[str] = None
    service_key: Optional[str] = None

    barber: Optional[str] = None

    metadata: Dict[str, Any] = Field(default_factory=dict)


class DoubleBookingAssignment(BaseModel):
    slot_id: str

    barber: Optional[str] = None
    day_text: Optional[str] = None
    time_hhmm: Optional[str] = None

    service_name: Optional[str] = None
    service_key: Optional[str] = None

    blocks: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DoubleBookingPlan(BaseModel):
    plan_id: str
    mode: DoubleBookingPlanMode = "parallel"

    day_text: Optional[str] = None
    summary: Optional[str] = None

    assignments: List[DoubleBookingAssignment] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DoubleBookingSession(BaseModel):
    active: bool = True
    stage: DoubleBookingStage = "collecting"

    entry_text: Optional[str] = None

    holder_name: Optional[str] = None
    day_text: Optional[str] = None
    preferred_time_hhmm: Optional[str] = None

    mode_preference: DoubleBookingModePreference = "parallel_first"

    items: List[DoubleBookingItem] = Field(default_factory=list)
    offered_plans: List[DoubleBookingPlan] = Field(default_factory=list)

    selected_plan_id: Optional[str] = None
    created_bundle_id: Optional[str] = None

    notes: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


def build_initial_double_booking_state(entry_text: str = "") -> DoubleBookingSession:
    return DoubleBookingSession(
        active=True,
        stage="collecting",
        entry_text=(entry_text or "").strip(),
        items=[
            DoubleBookingItem(slot_id="A"),
            DoubleBookingItem(slot_id="B"),
        ],
    )