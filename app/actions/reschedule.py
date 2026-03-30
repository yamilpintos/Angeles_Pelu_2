from typing import Dict, Any, Optional

from app.actions.booking import reserve_slot
from app.repos.bookings_repo import get_bookings_repo
from app.repos.sheets_repo import get_sheets_repo


def reschedule_booking(
    booking_id: int,
    draft,
    phone: str,
    provider: str,
    blocks: int,
    rgb: Optional[Dict[str, float]] = None,  # ✅ color del servicio
) -> Dict[str, Any]:
    """
    Reprogramación segura:
    1) lee el booking anterior (para conocer old_blocks)
    2) si draft no trae servicio, lo hereda del turno anterior (para no perder color)
    3) si draft no trae age, la hereda del turno anterior (para no perder color jubilado)
    4) reserva nuevo turno (con color si rgb existe), permitiendo superposición consigo mismo
    5) limpia del sheet solo la parte vieja que no se superpone con el nuevo
    6) marca el anterior como cancelado en DB
    """

    # 0️⃣ traer booking viejo
    bookings = get_bookings_repo()
    old = bookings.get_booking_by_id(booking_id)
    if not old:
        return {"ok": False, "error": "No encontré el turno anterior para reprogramar."}

    # old_blocks: si no existe o viene mal, fallback a 1
    try:
        old_blocks = int(old.get("blocks") or 0)
    except Exception:
        old_blocks = 0
    if old_blocks <= 0:
        old_blocks = 1

    # ✅ FIX: si draft no trae servicio o edad, heredar del turno anterior
    # (así rgb_from_draft puede calcular color normal/jubilado)
    try:
        draft_service = getattr(draft, "service_key", None) or getattr(draft, "service_name", None)
        if not (draft_service and str(draft_service).strip()):
            setattr(draft, "service_key", old.get("service_key") or old.get("service_canonical"))
            setattr(draft, "service_name", old.get("service_name"))

        draft_age = getattr(draft, "age", None)
        if draft_age is None:
            old_meta = old.get("metadata") or {}
            old_age = old_meta.get("age")
            if old_age is not None:
                setattr(draft, "age", int(old_age))
    except Exception:
        # fallback si draft fuese dict-like
        try:
            draft_service = (draft.get("service_key") or draft.get("service_name"))
            if not (draft_service and str(draft_service).strip()):
                draft["service_key"] = old.get("service_key") or old.get("service_canonical")
                draft["service_name"] = old.get("service_name")

            draft_age = draft.get("age")
            if draft_age is None:
                old_meta = old.get("metadata") or {}
                old_age = old_meta.get("age")
                if old_age is not None:
                    draft["age"] = int(old_age)
        except Exception:
            pass

    # ✅ permitir superposición consigo mismo
    ignore_range = {
        "tab": old.get("tab"),
        "sheet_id": old.get("sheet_id"),
        "row": old.get("row"),
        "col": old.get("col"),
        "blocks": old_blocks,
    }

    # 1️⃣ reservar nuevo
    new_res = reserve_slot(
        draft=draft,
        phone=phone,
        provider=provider,
        blocks=blocks,
        rgb=rgb,
        ignore_range=ignore_range,
    )

    if not new_res.ok:
        return {"ok": False, "error": new_res.error}

    # 2️⃣ limpiar del sheet solo la parte vieja que NO se superpone con el nuevo
    try:
        sheets = get_sheets_repo()

        old_tab = str(old.get("tab") or "")
        old_sheet_id = int(old.get("sheet_id"))
        old_row = int(old.get("row"))
        old_col = int(old.get("col"))
        old_end = old_row + old_blocks

        new_coords = new_res.sheet_coords or {}
        new_tab = str(new_coords.get("tab") or "")
        new_sheet_id = int(new_coords.get("sheet_id"))
        new_row = int(new_coords.get("row"))
        new_col = int(new_coords.get("col"))
        new_blocks = int(new_coords.get("blocks") or blocks or 1)
        new_end = new_row + new_blocks

        clear_ok = True

        # Si cambió de tab/hoja/columna, no hay solapamiento real: borrar todo el viejo
        if old_tab != new_tab or old_sheet_id != new_sheet_id or old_col != new_col:
            clear_ok = sheets.clear_blocks(
                tab=old_tab,
                sheet_id=old_sheet_id,
                row=old_row,
                col=old_col,
                blocks=old_blocks,
            )
        else:
            # tramo anterior del viejo
            if old_row < new_row:
                before_blocks = new_row - old_row
                if before_blocks > 0:
                    clear_ok = clear_ok and sheets.clear_blocks(
                        tab=old_tab,
                        sheet_id=old_sheet_id,
                        row=old_row,
                        col=old_col,
                        blocks=before_blocks,
                    )

            # tramo posterior del viejo
            if old_end > new_end:
                after_row = new_end
                after_blocks = old_end - new_end
                if after_blocks > 0:
                    clear_ok = clear_ok and sheets.clear_blocks(
                        tab=old_tab,
                        sheet_id=old_sheet_id,
                        row=after_row,
                        col=old_col,
                        blocks=after_blocks,
                    )

        if not clear_ok:
            return {
                "ok": False,
                "error": "Reservé el nuevo turno, pero no pude limpiar correctamente el anterior en el sheet.",
            }

    except Exception as e:
        return {
            "ok": False,
            "error": f"Reservé el nuevo turno, pero falló la limpieza del turno anterior: {e}",
        }

    # 3️⃣ marcar viejo como cancelado en DB
    try:
        db_ok = bool(bookings.mark_cancelled(booking_id))
    except Exception as e:
        return {
            "ok": False,
            "error": f"Reservé el nuevo turno, pero no pude cancelar el anterior en la base: {e}",
        }

    if not db_ok:
        return {
            "ok": False,
            "error": "Reservé el nuevo turno, pero no pude cancelar el anterior en la base.",
        }

    return {"ok": True, "new_booking_id": new_res.booking_id}