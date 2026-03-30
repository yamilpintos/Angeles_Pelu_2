from __future__ import annotations

import re

import httpx

from app.core.config import settings
from app.core.session_store import save_session
from app.ai.dialogue import respond


def meta_to_number(phone: str) -> str:
    raw = str(phone or "").strip()
    return re.sub(r"\D", "", raw)


def meta_api_version() -> str:
    return str(getattr(settings, "WHATSAPP_API_VERSION", "v23.0") or "v23.0").strip()


def meta_messages_url() -> str:
    phone_number_id = str(getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "") or "").strip()
    if not phone_number_id:
        raise RuntimeError("Falta WHATSAPP_PHONE_NUMBER_ID en settings/.env")

    return f"https://graph.facebook.com/{meta_api_version()}/{phone_number_id}/messages"


def send_whatsapp_message(phone: str, body: str) -> None:
    body_txt = str(body or "").strip()
    to_number = meta_to_number(phone)
    access_token = str(getattr(settings, "WHATSAPP_ACCESS_TOKEN", "") or "").strip()

    if not body_txt:
        print("[DBG META SEND] body vacío, no envío")
        return

    if not to_number:
        print("[DBG META SEND] destino vacío/inválido, no envío")
        return

    if not access_token:
        raise RuntimeError("Falta WHATSAPP_ACCESS_TOKEN en settings/.env")

    if to_number.startswith("549"):
        to_number = "54" + to_number[3:]

    url = meta_messages_url()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": body_txt},
    }

    print("[DBG META FINAL TO]", to_number)
    print("[DBG META SEND] to=", to_number)
    print("[DBG META SEND] body=", repr(body_txt[:500]))

    with httpx.Client(timeout=20.0) as client:
        resp = client.post(url, headers=headers, json=payload)
        print("[DBG META SEND STATUS]", resp.status_code)
        print("[DBG META SEND BODY]", resp.text[:1000])
        resp.raise_for_status()


def reply_async(phone: str, text: str) -> None:
    txt = str(text or "").strip()
    if not txt:
        print("[DBG REPLY ASYNC] texto vacío, no envío")
        return
    send_whatsapp_message(phone, txt)


def welcome_text() -> str:
    return (
        f"Hola 👋 Soy Ángeles, de {settings.SALON_NAME}. ¿Querés reservar un turno? 😊\n\n"
        "Para agendarlo necesito:\n"
        "👤 *Nombre y apellido*\n"
        "🎂 *Edad*\n"
        f"💈 *Peluquero:* {', '.join(settings.BARBERS)}\n"
        "📅 *Día:* ej. martes 3\n"
        "🕒 *Horario:* ej. 15:00\n\n"
        "Si querés, podés mandarme todo junto. Por ejemplo:\n"
        '"Gonzalo García, 32 años, Sergio, miércoles 4, 15:00"\n\n'
        "También te puedo ayudar a:\n"
        "🔹 Cancelar turnos\n"
        "🔹 Reprogramar una reserva\n"
        "🔹 Consultar servicios y precios\n\n"
        "Escribime como te quede más cómodo 😊"
    )


def safe_reply_text(reply_text: str | None, session, incoming_text: str = "") -> str:
    txt = (reply_text or "").strip()
    if txt:
        return txt

    try:
        draft_data = session.draft.model_dump()
        has_draft_data = any(v not in (None, "", [], {}) for v in draft_data.values())
    except Exception:
        has_draft_data = False

    pending_type = "none"
    try:
        pending_type = session.pending.type if session.pending else "none"
    except Exception:
        pending_type = "none"

    low = (incoming_text or "").strip().lower()
    greetings = (
        "hola",
        "holi",
        "buenas",
        "buen dia",
        "buen día",
        "buenas tardes",
        "buenas noches",
        "hello",
    )
    is_greeting = any(low.startswith(g) for g in greetings) or low in {"", "ok", "dale"}

    if pending_type == "none" and not has_draft_data and is_greeting:
        return welcome_text()

    return (
        "Perdón 😊 no llegué a responderte bien.\n\n"
        "Si querés reservar, pasame:\n"
        "👤 Nombre y apellido\n"
        "🎂 Edad\n"
        "💈 Peluquero\n"
        "📅 Día\n"
        "🕒 Hora\n\n"
        "Y si necesitás, también te ayudo a cancelar, reprogramar o ver servicios."
    )


def reply_with_event(phone: str, session, event_text: str, fallback_incoming: str = "") -> None:
    try:
        # Guarda defensiva: si el subflujo de doble booking está activo,
        # no llamamos a la IA general para evitar respuestas fantasma.
        # El handler de double booking ya maneja sus propias respuestas.
        from app.flows.double_booking import is_double_booking_active
        if is_double_booking_active(session):
            print("[DBG REPLY WITH EVENT] double_booking activo → skip IA general")
            save_session(phone, session)
            return

        ai2 = respond(event_text, session)
        save_session(phone, session)
        reply_async(phone, safe_reply_text(ai2.reply_text, session, fallback_incoming))
    except Exception as e:
        print("[ERR REPLY WITH EVENT]", type(e).__name__, str(e))
        save_session(phone, session)
        reply_async(phone, "Perdón, tuve un problema al responderte. ¿Me lo repetís?")