from __future__ import annotations

import time
from typing import Any, Dict

from twilio.rest import Client

from app.core.config import settings
from app.core.session_store import list_sessions, save_session


# =========================================================
# Config
# =========================================================

# A los 3 min sin respuesta: mandar follow-up
FOLLOWUP_AFTER_SECONDS = 3 * 60

# 3 min después del follow-up exitoso: limpiar sesión
CLEANUP_AFTER_FOLLOWUP_SECONDS = 3 * 60


# =========================================================
# Helpers
# =========================================================

def _twilio_client() -> Client:
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def _twilio_from_number() -> str:
    """
    Acepta:
    - 'whatsapp:+14155238886'
    - '+14155238886'
    """
    raw = str(getattr(settings, "TWILIO_WHATSAPP_FROM", "") or "").strip()
    if not raw:
        raw = str(getattr(settings, "TWILIO_WHATSAPP_NUMBER", "") or "").strip()

    if not raw:
        raise RuntimeError("Falta TWILIO_WHATSAPP_FROM o TWILIO_WHATSAPP_NUMBER en settings/.env")

    if raw.startswith("whatsapp:"):
        return raw

    return f"whatsapp:{raw}"


def _twilio_to_number(phone: str) -> str:
    phone = str(phone or "").strip()
    if phone.startswith("whatsapp:"):
        return phone
    return f"whatsapp:{phone}"


def _send_whatsapp_message(phone: str, body: str) -> Dict[str, Any]:
    """
    Devuelve el payload final del mensaje.
    IMPORTANTE: no asumir éxito solo porque el create devolvió SID.
    """
    client = _twilio_client()
    from_number = _twilio_from_number()
    to_number = _twilio_to_number(phone)

    print(
        "[DBG FOLLOWUP TRY]",
        {
            "from": from_number,
            "to": to_number,
            "body": body,
        }
    )

    msg = client.messages.create(
        from_=from_number,
        to=to_number,
        body=body,
    )

    sent_payload: Dict[str, Any] = {
        "phone": phone,
        "sid": getattr(msg, "sid", None),
        "status": getattr(msg, "status", None),
        "error_code": getattr(msg, "error_code", None),
        "error_message": getattr(msg, "error_message", None),
        "from": getattr(msg, "from_", None),
        "to": getattr(msg, "to", None),
    }

    print("[DBG FOLLOWUP SENT]", sent_payload)

    final_payload = sent_payload.copy()

    try:
        time.sleep(5)
        msg2 = client.messages(msg.sid).fetch()
        final_payload = {
            "phone": phone,
            "sid": getattr(msg2, "sid", None),
            "status": getattr(msg2, "status", None),
            "error_code": getattr(msg2, "error_code", None),
            "error_message": getattr(msg2, "error_message", None),
            "from": getattr(msg2, "from_", None),
            "to": getattr(msg2, "to", None),
        }
        print("[DBG FOLLOWUP FETCHED]", final_payload)
    except Exception as e:
        print("[DBG FOLLOWUP FETCH EXCEPTION]", type(e).__name__, str(e))

    return final_payload


def _is_success_status(status: str) -> bool:
    s = str(status or "").strip().lower()
    return s in {"queued", "accepted", "scheduled", "sending", "sent", "delivered", "read"}


def _reset_session_for_abandoned_flow(session) -> None:
    """
    Limpieza total para que el usuario pueda empezar de cero.
    """
    session.intent = "unknown"
    session.last_booking_id = ""
    session.last_user_message_at = None

    if getattr(session, "pending", None) is not None:
        session.pending.type = "none"
        session.pending.options = []

    session.pending_started_at = None
    session.followup_sent_at = None

    session.draft = session.draft.__class__(
        customer_name=None,
        barber=None,
        day_text=None,
        time_hhmm=None,
        service_name=None,
        service_key=None,
        age=None,
    )


def _has_active_pending(session) -> bool:
    try:
        return bool(session.pending and session.pending.type != "none")
    except Exception:
        return False


def _should_send_followup(now_ts: float, session) -> bool:
    if not _has_active_pending(session):
        return False

    pending_started_at = getattr(session, "pending_started_at", None)
    followup_sent_at = getattr(session, "followup_sent_at", None)
    last_user_message_at = getattr(session, "last_user_message_at", None)

    if not pending_started_at:
        return False

    if followup_sent_at:
        return False

    # referencia principal: último mensaje del usuario
    # fallback: inicio del pending
    base_ts = last_user_message_at or pending_started_at
    return (now_ts - base_ts) >= FOLLOWUP_AFTER_SECONDS


def _should_cleanup(now_ts: float, session) -> bool:
    if not _has_active_pending(session):
        return False

    followup_sent_at = getattr(session, "followup_sent_at", None)
    last_user_message_at = getattr(session, "last_user_message_at", None)

    if not followup_sent_at:
        return False

    # si el usuario escribió después del follow-up, no limpiar
    if last_user_message_at and last_user_message_at > followup_sent_at:
        return False

    return (now_ts - followup_sent_at) >= CLEANUP_AFTER_FOLLOWUP_SECONDS


def _followup_text(session) -> str:
    pending_type = ""
    try:
        pending_type = session.pending.type or ""
    except Exception:
        pending_type = ""

    if pending_type == "collect_booking_data":
        return (
            "Hola 🙂 Veo que empezaste a reservar un turno pero faltan algunos datos.\n"
            "¿Querés seguir con la gestión?\n"
            "Respondeme *sí* para continuar o *no* para cancelarla."
        )

    if pending_type == "confirm_booking":
        return (
            "Hola 🙂 Veo que quedó una reserva sin terminar.\n"
            "¿Querés seguir con la gestión?\n"
            "Respondeme *sí* para continuar o *no* para cancelarla."
        )

    if pending_type in {"choose_slot", "choose_time"}:
        return (
            "Hola 🙂 Veo que quedó una elección de horario sin terminar.\n"
            "¿Querés seguir con la gestión?\n"
            "Respondeme *sí* para continuar o *no* para cancelarla."
        )

    if pending_type in {"choose_cancel", "confirm_cancel"}:
        return (
            "Hola 🙂 Veo que quedó una cancelación sin terminar.\n"
            "¿Querés seguir con la gestión?\n"
            "Respondeme *sí* para continuar o *no* para cancelarla."
        )

    if pending_type in {"choose_reschedule", "choose_new_slot", "confirm_reschedule"}:
        return (
            "Hola 🙂 Veo que quedó una reprogramación sin terminar.\n"
            "¿Querés seguir con la gestión?\n"
            "Respondeme *sí* para continuar o *no* para cancelarla."
        )

    return (
        "Hola 🙂 Veo que quedó una gestión sin terminar.\n"
        "¿Querés seguir?\n"
        "Respondeme *sí* para continuar o *no* para cancelarla."
    )


# =========================================================
# Main job
# =========================================================

def run_pending_followup_job() -> None:
    now_ts = time.time()
    sessions = list_sessions()

    print("[DBG FOLLOWUP JOB] sessions_len=", len(sessions))

    for phone, session in sessions.items():
        try:
            if not _has_active_pending(session):
                continue

            if _should_send_followup(now_ts, session):
                body = _followup_text(session)
                result = _send_whatsapp_message(phone, body)

                final_status = str(result.get("status") or "").lower()
                error_code = result.get("error_code")
                error_message = result.get("error_message")

                # Solo marcar como enviado si NO terminó en failed/undelivered
                if _is_success_status(final_status) and not error_code:
                    session.followup_sent_at = now_ts
                    save_session(phone, session)
                    print(
                        "[DBG FOLLOWUP MARKED AS SENT]",
                        {
                            "phone": phone,
                            "status": final_status,
                            "sid": result.get("sid"),
                        }
                    )
                else:
                    print(
                        "[DBG FOLLOWUP NOT MARKED AS SENT]",
                        {
                            "phone": phone,
                            "status": final_status,
                            "error_code": error_code,
                            "error_message": error_message,
                            "sid": result.get("sid"),
                        }
                    )

                continue

            if _should_cleanup(now_ts, session):
                _reset_session_for_abandoned_flow(session)
                save_session(phone, session)
                print("[DBG FOLLOWUP CLEANUP OK]", phone)

        except Exception as e:
            print("[DBG FOLLOWUP JOB EXCEPTION]", phone, type(e).__name__, str(e))


if __name__ == "__main__":
    run_pending_followup_job()