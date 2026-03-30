from __future__ import annotations

from app.ai.client import responses_parse
from app.core.config import settings
from app.core.types import Session

from .schemas import DoubleBookingReply
from .prompt import SYSTEM
from . import context as double_booking_context


print("[DBG IMPORT double_booking/responder.py]", __file__)


def respond_double_booking(user_text: str, session: Session) -> DoubleBookingReply:
    print("[DBG DOUBLE BOOKING RESPONDER FILE]", __file__)
    print(
        "[DBG DOUBLE BOOKING CONTEXT MODULE FILE]",
        getattr(double_booking_context, "__file__", "NO_FILE"),
    )

    prompt = double_booking_context.build_double_booking_prompt(user_text, session)

    print("[DBG DOUBLE BOOKING PROMPT LEN]", len(prompt))
    print("[DBG DOUBLE BOOKING PROMPT HEAD]", repr(prompt[:1200]))

    parsed = responses_parse(
        model=settings.OPENAI_MODEL,
        system=SYSTEM,
        user=prompt,
        text_format=DoubleBookingReply,
    )

    if not getattr(parsed, "reply_text", None) or not str(parsed.reply_text).strip():
        parsed.reply_text = "Dale, contame un poco más y te ayudo a armar ese turno doble 😊"

    print(
        "[DBG DOUBLE BOOKING AI ACTION]",
        getattr(getattr(parsed, "action", None), "type", None),
    )
    print(
        "[DBG DOUBLE BOOKING AI CONFIRMATION]",
        getattr(parsed, "confirmation_state", None),
    )

    pending_resolution = getattr(parsed, "pending_resolution", None)
    if hasattr(pending_resolution, "model_dump"):
        print(
            "[DBG DOUBLE BOOKING AI PENDING_RESOLUTION]",
            pending_resolution.model_dump(),
        )
    else:
        print("[DBG DOUBLE BOOKING AI PENDING_RESOLUTION]", pending_resolution)

    draft_patch = getattr(parsed, "draft_patch", None)
    if hasattr(draft_patch, "model_dump"):
        print("[DBG DOUBLE BOOKING AI DRAFT_PATCH]", draft_patch.model_dump())
    else:
        print("[DBG DOUBLE BOOKING AI DRAFT_PATCH]", draft_patch)

    print("[DBG DOUBLE BOOKING AI REPLY_TEXT]", repr(getattr(parsed, "reply_text", None)))

    return parsed  # type: ignore