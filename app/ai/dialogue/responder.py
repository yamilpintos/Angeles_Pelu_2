from __future__ import annotations

from app.ai.client import responses_parse
from app.core.config import settings
from app.core.types import AIReply, Session

from .prompt import SYSTEM
from . import context as dialogue_context
from . import sheet_context as dialogue_sheet_context


print("[DBG IMPORT responder.py]", __file__)


def respond(user_text: str, session: Session) -> AIReply:
    print("[DBG RESPONDER FILE]", __file__)
    print("[DBG CONTEXT MODULE FILE]", getattr(dialogue_context, "__file__", "NO_FILE"))
    print("[DBG SHEET_CONTEXT MODULE FILE]", getattr(dialogue_sheet_context, "__file__", "NO_FILE"))

    prompt = (
        dialogue_context._now_context()
        + "\n"
        + dialogue_context._session_context(session)
        + "\n"
        + dialogue_sheet_context._sheet_context_for_one_day(user_text, session)
        + "\n"
        + f"Mensaje del cliente: {user_text}"
    )

    print("[DBG PROMPT LEN]", len(prompt))
    print("[DBG PROMPT HEAD]", repr(prompt[:1200]))

    parsed = responses_parse(
        model=settings.OPENAI_MODEL,
        system=SYSTEM,
        user=prompt,
        text_format=AIReply,
    )

    if not getattr(parsed, "reply_text", None) or not str(parsed.reply_text).strip():
        parsed.reply_text = "Dale, contame y te ayudo con eso."

    print("[DBG AI RESPOND] intent=", getattr(parsed, "intent", None))
    print("[DBG AI RESPOND] action=", getattr(getattr(parsed, "action", None), "type", None))
    print("[DBG AI RESPOND] confirmation_state=", getattr(parsed, "confirmation_state", None))

    pending_resolution = getattr(parsed, "pending_resolution", None)
    if hasattr(pending_resolution, "model_dump"):
        print("[DBG AI RESPOND] pending_resolution=", pending_resolution.model_dump())
    else:
        print("[DBG AI RESPOND] pending_resolution=", pending_resolution)

    print("[DBG AI RESPOND] reply_text=", repr(getattr(parsed, "reply_text", None)))
    print("[DBG AI DAY_TEXT]", getattr(getattr(parsed, "draft_patch", None), "day_text", None))
    print("[DBG AI TIME_HHMM]", getattr(getattr(parsed, "draft_patch", None), "time_hhmm", None))
    print("[DBG AI LATEST_FINISH]", getattr(getattr(parsed, "draft_patch", None), "latest_finish_hhmm", None))

    return parsed  # type: ignore