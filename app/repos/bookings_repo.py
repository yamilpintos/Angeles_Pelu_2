from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, List
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote  # <-- agregar arriba
from app.core.config import settings


# =========================================================
# Resultados
# =========================================================

@dataclass
class BookingInsertResult:
    ok: bool
    booking_id: Optional[int] = None
    error: Optional[str] = None
    row: Optional[Dict[str, Any]] = None


# =========================================================
# Repo Supabase
# =========================================================

class SupabaseBookingsRepo:
    def __init__(self):
        self.base_url = settings.SUPABASE_URL.rstrip("/")
        self.key = settings.SUPABASE_SERVICE_ROLE_KEY
        self.table = settings.SUPABASE_BOOKINGS_TABLE

        if not self.base_url or not self.key:
            raise RuntimeError("Falta SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY en .env")

    def _headers(self) -> Dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    # =========================================================
    # CREAR BOOKING
    # =========================================================
    def create_booking(self, payload: Dict[str, Any]) -> BookingInsertResult:
        url = f"{self.base_url}/rest/v1/{self.table}"

        try:
            with httpx.Client(timeout=15) as client:
                r = client.post(url, headers=self._headers(), json=payload)
                if r.status_code >= 300:
                    return BookingInsertResult(ok=False, error=f"{r.status_code}: {r.text}")

                rows = r.json() if r.text else []
                if not rows:
                    return BookingInsertResult(ok=False, error="Supabase no devolvió fila insertada")

                booking_id = int(rows[0].get("id"))
                return BookingInsertResult(ok=True, booking_id=booking_id, row=rows[0])

        except Exception as e:
            return BookingInsertResult(ok=False, error=f"{type(e).__name__}: {e}")

    # =========================================================
    # OBTENER BOOKING POR ID
    # =========================================================
    def get_booking_by_id(self, booking_id: int) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/rest/v1/{self.table}?id=eq.{booking_id}&select=*"
        try:
            with httpx.Client(timeout=15) as client:
                r = client.get(url, headers=self._headers())
                rows = r.json() if r.text else []
                return rows[0] if rows else None
        except Exception:
            return None

    # =========================================================
    # LISTAR TURNOS ACTIVOS POR TELÉFONO
    # =========================================================


    def list_active_by_phone(self, phone: str) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/rest/v1/{self.table}"
        params = {
            "phone": f"eq.{str(phone or '').strip()}",
            "status": "eq.booked",
            "select": "*",
        }
        try:
            with httpx.Client(timeout=15) as client:
                r = client.get(url, headers=self._headers(), params=params)
                if r.status_code >= 300:
                    return []
                return r.json() if r.text else []
        except Exception:
            return []

    # =========================================================
    # MARCAR CANCELADO
    # =========================================================
    def mark_cancelled(self, booking_id: int) -> bool:
        url = f"{self.base_url}/rest/v1/{self.table}?id=eq.{booking_id}"
        try:
            with httpx.Client(timeout=15) as client:
                r = client.patch(
                    url,
                    headers=self._headers(),
                    json={"status": "cancelled"},
                )
                return r.status_code < 300
        except Exception:
            return False


# =========================================================
# BUILD PAYLOAD
# =========================================================

def build_booking_payload_for_supabase(
    *,
    phone: str,
    provider: str,
    customer_name: str,
    barber: str,
    time_hhmm: str,
    sheet_id: int,
    tab: Optional[str],
    row: int,
    col: int,
    blocks: int,
    day_num: Optional[int] = None,
    date_text: Optional[str] = None,
    date_iso: Optional[str] = None,
    service_name: Optional[str] = None,
    service_canonical: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:

    tz = ZoneInfo(settings.TIMEZONE)

    starts_at = None
    ends_at = None

    if date_iso and time_hhmm:
        try:
            dt = datetime.fromisoformat(f"{date_iso}T{time_hhmm}:00").replace(tzinfo=tz)
            starts_at = dt.isoformat()
            ends_at = (dt + timedelta(minutes=30 * int(blocks))).isoformat()
        except Exception:
            pass

    return {
        "provider": provider,
        "phone": phone,
        "customer_name": customer_name,
        "barber": barber,
        "day_num": day_num,
        "date_text": date_text,
        "time_hhmm": time_hhmm,
        "service_name": service_name,
        "service_canonical": service_canonical,
        "sheet_id": int(sheet_id),
        "tab": tab,
        "row": int(row),
        "col": int(col),
        "blocks": int(blocks),
        "status": "booked",
        "starts_at": starts_at,
        "ends_at": ends_at,
        "date_iso": date_iso,
        "metadata": metadata or {},
    }


# =========================================================
# FACTORY
# =========================================================

def get_bookings_repo() -> SupabaseBookingsRepo:
    return SupabaseBookingsRepo()