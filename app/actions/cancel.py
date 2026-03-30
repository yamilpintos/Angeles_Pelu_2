from typing import Dict, Any, Optional
from app.repos.bookings_repo import get_bookings_repo
from app.repos.sheets_repo import get_sheets_repo


def cancel_booking(booking_id: int, blocks_override: Optional[int] = None) -> Dict[str, Any]:
    """
    Cancela turno real:
    1) busca booking
    2) libera bloques en Sheets
    3) marca booking cancelado

    ✅ blocks_override:
    - Si viene, se usa para limpiar N bloques aunque en DB falte/esté mal 'blocks'.
    """

    bookings = get_bookings_repo()
    sheets = get_sheets_repo()

    booking = bookings.get_booking_by_id(booking_id)
    if not booking:
        return {"ok": False, "error": "No encontré el turno."}

    try:
        tab = booking.get("tab")
        sheet_id = int(booking.get("sheet_id"))
        row = int(booking.get("row"))
        col = int(booking.get("col"))
    except Exception:
        return {"ok": False, "error": "Datos del turno incompletos (coords)."}

    # ✅ blocks robusto
    if blocks_override is not None:
        blocks = int(blocks_override)
    else:
        try:
            blocks = int(booking.get("blocks") or 0)
        except Exception:
            blocks = 0

    if blocks <= 0:
        blocks = 1  # fallback seguro

    # 1️⃣ liberar bloques en Sheets
    ok = sheets.clear_blocks(
        tab=tab,
        sheet_id=sheet_id,
        row=row,
        col=col,
        blocks=blocks,
    )

    if not ok:
        return {"ok": False, "error": "No pude liberar el horario en la agenda."}

    # 2️⃣ marcar cancelado en DB
    db_ok = bookings.mark_cancelled(booking_id)

    if not db_ok:
        return {
            "ok": False,
            "error": "El turno se liberó en la agenda pero no pude actualizar la base.",
        }

    return {"ok": True}