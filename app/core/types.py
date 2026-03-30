from __future__ import annotations

from typing import Optional, Literal, List, Any, Dict
from pydantic import BaseModel, Field



Intent = Literal["book", "cancel", "reschedule", "info", "late", "unknown"]
ConfirmationState = Literal["none", "confirm", "reject"]


class Draft(BaseModel):
    customer_name: Optional[str] = None
    barber: Optional[str] = None
    day_text: Optional[str] = None
    time_hhmm: Optional[str] = None
    service_name: Optional[str] = None
    service_key: Optional[str] = None
    age: Optional[int] = None

    # hora máxima a la que el cliente necesita terminar
    latest_finish_hhmm: Optional[str] = None


class Pending(BaseModel):
    type: Literal[
        "none",
        "collect_booking_data",
        "choose_time",
        "choose_slot",
        "confirm_booking",
        "choose_cancel",
        "confirm_cancel",
        "choose_reschedule",
        "choose_new_slot",
        "confirm_reschedule",
        # double booking
        "collect_double_booking_data",
        "choose_double_plan",
        "confirm_double_booking",
    ] = "none"

    # En etapa 2, cada opción debería empezar a traer option_id estable.
    options: List[Dict[str, Any]] = Field(default_factory=list)


class Action(BaseModel):
    type: Literal[
        "none",
        "check_day_availability",
        "find_offers",
        "resolve_pending_choice",
        "cancel_booking",
        "handle_late_arrival",
    ] = "none"

    booking_id: Optional[int] = None
    late_minutes: Optional[int] = None


class PendingResolution(BaseModel):
    type: Literal[
        "none",
        "pending_option",
    ] = "none"

    # Se usará de verdad a partir de la etapa 2,
    # cuando los pending.options tengan option_id.
    option_id: Optional[str] = None


class AIReply(BaseModel):
    intent: Intent = "unknown"
    draft_patch: Draft = Field(default_factory=Draft)
    action: Action = Field(default_factory=Action)

    # NUEVO: confirmación/rechazo explícito desde la IA
    confirmation_state: ConfirmationState = "none"

    # NUEVO: resolución explícita de una opción pendiente
    pending_resolution: PendingResolution = Field(default_factory=PendingResolution)

    # TRANSICIONAL:
    # Lo mantenemos por compatibilidad con el código actual.
    # En etapas siguientes lo vamos a ir retirando del circuito principal.
    selected_time_hhmm: Optional[str] = None

    reply_text: str = ""


class Session(BaseModel):
    intent: Intent = "unknown"
    draft: Draft = Field(default_factory=Draft)
    pending: Pending = Field(default_factory=Pending)

    last_booking_id: Optional[str] = None

    last_user_message_at: Optional[float] = None
    pending_started_at: Optional[float] = None
    followup_sent_at: Optional[float] = None

    # Estado del subflujo de turno doble.
    # Se persiste como dict para que model_dump() lo incluya en la sesión de Supabase.
    double_booking: Optional[Dict[str, Any]] = None


class DayAvailability(BaseModel):
    barber: str
    day_text: str
    free_times: List[str] = Field(default_factory=list)