from __future__ import annotations

from typing import Any, Dict, Optional
import httpx
from datetime import datetime, timezone

from app.core.config import settings
from app.core.types import Session


class SupabaseSessionStore:
    def __init__(self):
        self.base_url = settings.SUPABASE_URL.rstrip("/")
        self.key = settings.SUPABASE_SERVICE_ROLE_KEY
        self.table = "chat_sessions"

        if not self.base_url or not self.key:
            raise RuntimeError("Falta SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY en .env")

    def _headers(self) -> Dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def load(self, phone: str) -> Session:
        url = f"{self.base_url}/rest/v1/{self.table}"
        phone_raw = str(phone or "").strip()
        params = {
            "phone": f"eq.{phone_raw}",
            "select": "session_json,updated_at",
            "order": "updated_at.desc",
            "limit": "1",
        }

        with httpx.Client(timeout=15) as client:
            r = client.get(url, headers=self._headers(), params=params)
            if r.status_code >= 300:
                return Session()

            rows = r.json()
            if not rows:
                return Session()

            sj = rows[0].get("session_json") or {}
            try:
                return Session.model_validate(sj)
            except Exception:
                return Session()

    def save(self, phone: str, session: Session) -> None:
        url = f"{self.base_url}/rest/v1/{self.table}"
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        phone = str(phone or "").strip()

        payload = {
            "phone": phone,
            "provider": "twilio",
            "session_json": session.model_dump(),
            "updated_at": now_iso,

            "intent": session.intent,
            "customer_name": session.draft.customer_name,
            "barber": session.draft.barber,
            "date_text": session.draft.day_text,
            "time_hhmm": session.draft.time_hhmm,
            "age": session.draft.age,

            # 👇 NUEVO
            "pending_type": session.pending.type if session.pending else "none",
            "pending_started_at": getattr(session, "pending_started_at", None),
            "followup_sent_at": getattr(session, "followup_sent_at", None),
            "last_user_message_at": getattr(session, "last_user_message_at", None),
        }

        headers = self._headers()
        headers["Prefer"] = "resolution=merge-duplicates"

        with httpx.Client(timeout=15) as client:
            try:
                print("[DBG SESSION SAVE PAYLOAD] pending_type=", payload.get("pending_type"))
                print("[DBG SESSION SAVE PAYLOAD] pending_started_at=", payload.get("pending_started_at"))
                print("[DBG SESSION SAVE PAYLOAD] followup_sent_at=", payload.get("followup_sent_at"))
                print("[DBG SESSION SAVE PAYLOAD] last_user_message_at=", payload.get("last_user_message_at"))

                r = client.post(
                    url,
                    headers=headers,
                    params={"on_conflict": "phone"},
                    json=payload,
                )

                if r.status_code >= 300:
                    print("[DBG SESSION SAVE FAIL]", r.status_code, r.text[:300])
                else:
                    print("[DBG SESSION SAVE OK]")

            except Exception as e:
                print("[DBG SESSION SAVE EXCEPTION]", type(e).__name__, str(e))

    def list_sessions(self) -> Dict[str, Session]:
        """
        Devuelve sesiones pendientes indexadas por phone.
        Útil para jobs de follow-up / limpieza.
        """
        url = f"{self.base_url}/rest/v1/{self.table}"
        params = {
            "select": "phone,session_json,updated_at",
            "pending_type": "neq.none",
            "order": "updated_at.desc",
            "limit": "1000",
        }

        out: Dict[str, Session] = {}

        with httpx.Client(timeout=30) as client:
            try:
                r = client.get(url, headers=self._headers(), params=params)
                if r.status_code >= 300:
                    print("[DBG SESSION LIST FAIL]", r.status_code, r.text[:300])
                    return out

                rows = r.json() or []
                for row in rows:
                    phone = str(row.get("phone") or "").strip()
                    if not phone:
                        continue

                    sj = row.get("session_json") or {}
                    try:
                        session = Session.model_validate(sj)
                    except Exception:
                        session = Session()

                    out[phone] = _clean_pending_if_expired(session)

            except Exception as e:
                print("[DBG SESSION LIST EXCEPTION]", type(e).__name__, str(e))

        return out


# =========================================================
# Optional: limpieza de pending vencido (si tu Pending tiene expires_at)
# No rompe aunque tu modelo no lo tenga todavía.
# =========================================================

def _parse_dt(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)

    s = str(v).strip()
    if not s:
        return None

    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _clean_pending_if_expired(session: Session) -> Session:
    try:
        p = getattr(session, "pending", None)
        if not p:
            return session

        expires_at = getattr(p, "expires_at", None)
        if not expires_at:
            return session

        dt = _parse_dt(expires_at)
        if not dt:
            return session

        if datetime.now(timezone.utc) >= dt:
            try:
                session.pending.type = "none"
                session.pending.options = []
                if hasattr(session.pending, "expires_at"):
                    session.pending.expires_at = None
            except Exception:
                session.pending = None
    except Exception:
        pass

    return session


# =========================================================
# Public API esperado por el webhook / jobs
# =========================================================

_store: Optional[SupabaseSessionStore] = None


def _get_store() -> SupabaseSessionStore:
    global _store
    if _store is None:
        _store = SupabaseSessionStore()
    return _store


def load_session(phone: str) -> Session:
    """
    Wrapper para mantener compatibilidad con:
    from app.core.session_store import load_session, save_session
    """
    s = _get_store().load(phone)
    return _clean_pending_if_expired(s)


def save_session(phone: str, session: Session) -> None:
    """
    Wrapper para mantener compatibilidad con:
    from app.core.session_store import load_session, save_session
    """
    _get_store().save(phone, session)


def list_sessions() -> Dict[str, Session]:
    """
    Wrapper público para jobs de seguimiento / limpieza.
    """
    return _get_store().list_sessions()