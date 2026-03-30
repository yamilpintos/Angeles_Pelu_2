from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional, Tuple, Dict, Any
from app.core.config import settings


# =========================================================
# Helpers de texto y tiempo
# =========================================================

_DOW_WORDS = [
    "lunes", "martes", "miercoles", "miércoles", "jueves", "viernes", "sabado", "sábado", "domingo"
]

_MONTH_INDEX_ES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

_MONTH_TAB_ES = {
    1: "ENE",
    2: "FEB",
    3: "MAR",
    4: "ABR",
    5: "MAY",
    6: "JUN",
    7: "JUL",
    8: "AGO",
    9: "SEP",
    10: "OCT",
    11: "NOV",
    12: "DIC",
}

def _hhmm_to_minutes(hhmm: str) -> int:
    hhmm = _normalize_hhmm(hhmm)
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _latest_start_for_blocks(blocks: int) -> str:
    """
    Último inicio permitido respetando que:
    - el bloque de 20:00 queda reservado
    - no debe correrse a 20:30
    """
    b = max(1, int(blocks or 1))
    latest_minutes = (20 * 60) - (b * 30)   # 20:00 - duración
    if latest_minutes < (12 * 60):
        latest_minutes = 12 * 60
    return f"{latest_minutes // 60:02d}:{latest_minutes % 60:02d}"

def _norm(s: str) -> str:
    s = str(s or "")
    s = s.replace("\u00a0", " ")
    s = s.strip().lower()
    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_hhmm_safe(x: Any) -> Optional[str]:
    """
    Devuelve HH:MM o None si no parece hora.
    """
    s = str(x or "").strip().replace("\u00a0", "").strip()
    s = s.replace(".", ":")
    if len(s) >= 5 and s[2] == ":":
        s = s[:5]
    if not re.match(r"^\d{1,2}:\d{2}$", s):
        return None
    try:
        return _normalize_hhmm(s)
    except Exception:
        return None


def _normalize_hhmm(x) -> str:
    s = str(x or "").strip()

    if ":" in s:
        h, m = s.split(":", 1)
        if h.isdigit() and m.isdigit():
            return f"{int(h):02d}:{int(m):02d}"

    digits = "".join(re.findall(r"\d+", s))
    if not digits:
        raise ValueError(f"bad hhmm (no digits): {s!r}")

    if len(digits) == 4:
        hh, mm = digits[:2], digits[2:]
    elif len(digits) == 3:
        hh, mm = digits[:1], digits[1:]
    elif len(digits) <= 2:
        hh, mm = digits, "00"
    else:
        raise ValueError(f"bad hhmm (len={len(digits)}): {s!r}")

    return f"{int(hh):02d}:{int(mm):02d}"


def _add_minutes_hhmm(hhmm, minutes: int) -> str:
    hhmm2 = _normalize_hhmm(hhmm)
    h, m = hhmm2.split(":")
    total = int(h) * 60 + int(m) + minutes
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def _extract_day_num(day_text: str) -> Optional[int]:
    m = re.search(r"\b(\d{1,2})\b", (day_text or "").strip())
    return int(m.group(1)) if m else None


def _extract_dow(day_text: str) -> Optional[str]:
    s = _norm(day_text)
    for dow in _DOW_WORDS:
        if dow in s:
            return dow
    return None


def _extract_month(day_text: str) -> Optional[int]:
    s = _norm(day_text)
    for month_name, month_num in _MONTH_INDEX_ES.items():
        if re.search(rf"\b{re.escape(month_name)}\b", s):
            return month_num
    return None


def _tz() -> ZoneInfo:
    return ZoneInfo(getattr(settings, "TIMEZONE", "America/Argentina/Buenos_Aires"))


def _now_local() -> datetime:
    return datetime.now(_tz())


def _month_tab_from_now() -> str:
    """
    Usa el mes actual como pestaña (ENE/FEB/MAR/...), en mayúsculas.
    Si querés forzar pestaña, definí settings.SHEETS_MONTH_TAB.
    """
    forced = (getattr(settings, "SHEETS_MONTH_TAB", "") or "").strip()
    if forced:
        return forced.upper()

    now = _now_local()
    return _MONTH_TAB_ES[now.month]


def _sheet_day_label(dt: datetime) -> str:
    dias = {
        0: "LUNES",
        1: "MARTES",
        2: "MIERCOLES",
        3: "JUEVES",
        4: "VIERNES",
        5: "SABADO",
        6: "DOMINGO",
    }
    return f"{dias[dt.weekday()]} {dt.day}"


def _resolve_sheet_target(day_text: str) -> Tuple[str, str]:
    """
    Convierte day_text del chat a:
    - tab real del sheet (ENE/FEB/MAR...)
    - label real del encabezado del sheet (ej: 'MIERCOLES 4')

    Reglas:
    - si el usuario puso mes explícito, se respeta
    - si no puso mes, usa mes actual o siguiente
    - si hay día de semana, debe coincidir
    - si no se puede resolver, devuelve fallback
    """
    forced = (getattr(settings, "SHEETS_MONTH_TAB", "") or "").strip()
    if forced:
        return forced.upper(), day_text

    now = _now_local()
    raw = _norm(day_text)
    day_num = _extract_day_num(day_text)
    dow = _extract_dow(day_text)
    month_num = _extract_month(day_text)

    def try_build(y: int, m: int, d: int) -> Optional[datetime]:
        try:
            return datetime(y, m, d, 12, 0, tzinfo=_tz())
        except Exception:
            return None

    def matches_dow(dt: datetime) -> bool:
        if not dow:
            return True
        wanted = _norm(dow)
        if wanted == "miercoles":
            wanted = "miércoles"
        if wanted == "sabado":
            wanted = "sábado"
        return _norm(_sheet_day_label(dt)).startswith(_norm(wanted))

    # relativos
    if raw == "hoy":
        dt = now
        return _MONTH_TAB_ES[dt.month], _sheet_day_label(dt)

    if raw in ("manana", "mañana"):
        dt = now + timedelta(days=1)
        return _MONTH_TAB_ES[dt.month], _sheet_day_label(dt)

    if raw in ("pasado manana", "pasado mañana"):
        dt = now + timedelta(days=2)
        return _MONTH_TAB_ES[dt.month], _sheet_day_label(dt)

    # día numérico explícito
    if day_num is not None:
        # mes explícito
        if month_num is not None:
            candidate = try_build(now.year, month_num, day_num)
            if candidate and matches_dow(candidate) and candidate >= now.replace(hour=0, minute=0, second=0, microsecond=0):
                return _MONTH_TAB_ES[candidate.month], _sheet_day_label(candidate)

            candidate_next_year = try_build(now.year + 1, month_num, day_num)
            if candidate_next_year and matches_dow(candidate_next_year):
                return _MONTH_TAB_ES[candidate_next_year.month], _sheet_day_label(candidate_next_year)

            return _month_tab_from_now(), day_text

        # sin mes explícito: actual o siguiente
        candidate = try_build(now.year, now.month, day_num)
        if candidate and matches_dow(candidate) and candidate >= now.replace(hour=0, minute=0, second=0, microsecond=0):
            return _MONTH_TAB_ES[candidate.month], _sheet_day_label(candidate)

        if now.month == 12:
            y2, m2 = now.year + 1, 1
        else:
            y2, m2 = now.year, now.month + 1

        candidate2 = try_build(y2, m2, day_num)
        if candidate2 and matches_dow(candidate2):
            return _MONTH_TAB_ES[candidate2.month], _sheet_day_label(candidate2)

        return _month_tab_from_now(), day_text

    # solo día de semana
    if dow is not None:
        target_idx = {
            "lunes": 0,
            "martes": 1,
            "miercoles": 2,
            "miércoles": 2,
            "jueves": 3,
            "viernes": 4,
            "sabado": 5,
            "sábado": 5,
            "domingo": 6,
        }[_norm(dow)]

        delta = (target_idx - now.weekday()) % 7
        dt = now + timedelta(days=delta)
        if delta == 0:
            dt = now
        return _MONTH_TAB_ES[dt.month], _sheet_day_label(dt)

    return _month_tab_from_now(), day_text


def _cell_text(v: Any) -> str:
    return str(v or "").replace("\u00a0", " ").strip()


def _cell_occupied(v: str) -> bool:
    """Ocupado si hay cualquier texto (Nombre o X)."""
    return _cell_text(v) != ""


def _cell_is_free(v: Any) -> bool:
    return _cell_text(v) == ""


def _cell_is_x(v: Any) -> bool:
    return _norm(_cell_text(v)) == "x"


def _cell_has_customer(v: Any) -> bool:
    s = _cell_text(v)
    if not s:
        return False
    return _norm(s) != "x"


# =========================================================
# Interface Repo
# =========================================================

class SheetsRepo:
    def get_free_times_for_day(self, *, barber: str, day_text: str) -> List[str]:
        raise NotImplementedError

    def is_slot_free(
        self,
        *,
        barber: str,
        day_text: str,
        time_hhmm: str,
        blocks: int = 1,
        ignore_range: Optional[Dict[str, Any]] = None,
    ) -> bool:
        raise NotImplementedError

    def get_day_windows(
        self,
        *,
        barber: str,
        day_text: str,
        blocks: int,
        ignore_range: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        raise NotImplementedError

    def count_booked_slots(self, *, barber: str, day_text: str) -> int:
        raise NotImplementedError

    def is_day_fully_blocked(self, *, barber: str, day_text: str) -> bool:
        raise NotImplementedError

    def get_barber_status(self, *, barber: str, day_text: str) -> str:
        raise NotImplementedError

    def paint_blocks(
        self,
        *,
        barber: str,
        day_text: str,
        time_hhmm: str,
        blocks: int,
        customer_name: str,
        rgb: Optional[Dict[str, float]] = None,
        ignore_range: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def clear_blocks(self, *, tab: str, sheet_id: int, row: int, col: int, blocks: int) -> bool:
        raise NotImplementedError


# =========================================================
# Mock Repo (dev)
# =========================================================

class MockSheetsRepo(SheetsRepo):
    def get_free_times_for_day(self, *, barber: str, day_text: str) -> List[str]:
        if "jueves" in (day_text or "").lower():
            return ["10:00", "10:30", "11:00", "15:30", "16:00", "18:00"]
        return ["12:00", "12:30", "13:00", "17:00", "17:30"]

    def is_slot_free(
        self,
        *,
        barber: str,
        day_text: str,
        time_hhmm: str,
        blocks: int = 1,
        ignore_range: Optional[Dict[str, Any]] = None,
    ) -> bool:
        free = set(self.get_free_times_for_day(barber=barber, day_text=day_text))
        b = max(1, int(blocks or 1))
        for i in range(b):
            t = _add_minutes_hhmm(time_hhmm, 30 * i)
            if t not in free:
                return False
        return True

    def get_day_windows(
        self,
        *,
        barber: str,
        day_text: str,
        blocks: int,
        ignore_range: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        return self.get_free_times_for_day(barber=barber, day_text=day_text)
    
    def count_booked_slots(self, *, barber: str, day_text: str) -> int:
        return 0

    def is_day_fully_blocked(self, *, barber: str, day_text: str) -> bool:
        return False

    def get_barber_status(self, *, barber: str, day_text: str) -> str:
        return "working"

    def paint_blocks(
        self,
        *,
        barber: str,
        day_text: str,
        time_hhmm: str,
        blocks: int,
        customer_name: str,
        rgb: Optional[Dict[str, float]] = None,
        ignore_range: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {"ok": True, "tab": "MOCK", "sheet_id": 0, "row": 10, "col": 2, "blocks": int(blocks or 1)}

    def clear_blocks(self, *, tab: str, sheet_id: int, row: int, col: int, blocks: int) -> bool:
        return True

    def list_days_in_month(self, *, day_text: str) -> List[str]:
        return []

    def iter_days_from(self, *, day_text: str) -> List[str]:
        return []


# =========================================================
# Google Repo (REAL)
# =========================================================

class GoogleSheetsRepo(SheetsRepo):
    """
    Implementación REAL para tu layout:
    - Tabs por mes: ENE/FEB/MAR...
    - Días en bloques horizontales de 4 columnas:
      [Horarios, Barber1, Barber2, Barber3]
    - Cada día tiene:
      fila de título: "JUEVES 5"
      fila de headers: "Horarios | Franco | Sergio | Eze" (o Luka)
      filas de horarios: 12:00 ... 20:30
    """

    GRID_RANGE_A1 = "A1:AD100"

    def __init__(self):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        cred_src = (getattr(settings, "GOOGLE_SERVICE_ACCOUNT_JSON", "") or "").strip()
        if not cred_src:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON vacío")

        if os.path.exists(cred_src):
            with open(cred_src, "r", encoding="utf-8") as f:
                info = json.load(f)
        else:
            info = json.loads(cred_src)

        creds = Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self._service = build("sheets", "v4", credentials=creds)

        sid = (getattr(settings, "SHEETS_SPREADSHEET_ID", "") or "").strip()
        if not sid:
            raise RuntimeError("Falta settings.SHEETS_SPREADSHEET_ID (id del Google Sheet)")
        self._spreadsheet_id = sid

        self.GRID_TTL_SECONDS = int(getattr(settings, "SHEETS_GRID_TTL_SECONDS", 60) or 60)

        self._grid_cache: Dict[str, Tuple[float, List[List[str]]]] = {}
        self._sheet_id_cache: Dict[str, int] = {}

    # -------------------------
    # Lectura + cache del grid
    # -------------------------

    def _invalidate_tab_cache(self, tab: str) -> None:
        self._grid_cache.pop(tab, None)

    def _get_month_grid(self, tab: str) -> List[List[str]]:
        tab = tab.strip()
        now = time.time()

        hit = self._grid_cache.get(tab)
        if hit:
            ts, grid = hit
            age = now - ts
            if age < self.GRID_TTL_SECONDS:
                print("[DBG GRID] CACHE HIT tab=", tab, "age=", round(age, 2), "rows=", len(grid), "cols=", (len(grid[0]) if grid else 0))
                return grid
            else:
                print("[DBG GRID] CACHE EXPIRED tab=", tab, "age=", round(age, 2))

        rng = f"{tab}!{self.GRID_RANGE_A1}"
        print("[DBG GRID] FETCH range=", rng)

        _retry_delays = (1, 2, 4)
        resp = None
        for _attempt, _delay in enumerate((*_retry_delays, None)):
            try:
                resp = (
                    self._service.spreadsheets()
                    .values()
                    .get(spreadsheetId=self._spreadsheet_id, range=rng)
                    .execute()
                )
                break
            except Exception as _exc:
                _status = getattr(getattr(_exc, "resp", None), "status", None)
                if _status == 429 and _delay is not None:
                    print(f"[WARN GRID] 429 rate limit tab={tab} retry in {_delay}s (attempt {_attempt + 1})")
                    time.sleep(_delay)
                else:
                    raise
        values = (resp or {}).get("values") or []

        print("[DBG GRID] RAW values_rows=", len(values))
        if values:
            print("[DBG GRID] RAW first_row_len=", len(values[0]))
        else:
            print("[DBG GRID] RAW first_row_len= 0")

        max_cols = 30
        max_rows = 100

        grid: List[List[str]] = []
        for r in range(max_rows):
            row = values[r] if r < len(values) else []
            row2 = [str(row[c]) if c < len(row) else "" for c in range(max_cols)]
            grid.append(row2)

        print("[DBG GRID] BUILT rows=", len(grid), "cols=", (len(grid[0]) if grid else 0))

        for rr in range(min(8, len(grid))):
            print(f"[DBG GRID ROW {rr}]", grid[rr][:30])

        day_hits = []
        for r, row in enumerate(grid):
            for c, v in enumerate(row):
                raw = str(v or "")
                txt = raw.strip()
                if not txt:
                    continue

                nv = _norm(txt)
                if any(d in nv for d in _DOW_WORDS):
                    day_hits.append((r, c, txt, nv))

        print("[DBG GRID] DAY CELLS FOUND count=", len(day_hits))
        for item in day_hits[:40]:
            print("[DBG GRID DAY CELL]", item)

        self._grid_cache[tab] = (now, grid)
        return grid

    # -------------------------
    # Helpers layout
    # -------------------------

    def _find_day_anchor(self, grid: List[List[str]], day_text: str) -> Optional[Tuple[int, int]]:
        target_n = _norm(day_text)
        print("[DBG ANCHOR] raw day_text=", repr(day_text), "norm=", repr(target_n))

        found_titles = []

        for r, row in enumerate(grid):
            for c, v in enumerate(row):
                raw = str(v or "")
                if not raw.strip():
                    continue

                nv = _norm(raw)

                if any(d in nv for d in _DOW_WORDS):
                    found_titles.append((r, c, raw, nv))

                if nv == target_n:
                    print("[DBG ANCHOR] EXACT MATCH ->", (r, c), "raw=", repr(raw), "norm=", repr(nv))
                    return (r, c)

        print("[DBG ANCHOR] titles found sample=", found_titles[:20])

        parts = target_n.split()
        if parts:
            for r, c, raw, nv in found_titles:
                vv_parts = nv.split()
                ok = True
                for p in parts:
                    if p not in vv_parts:
                        ok = False
                        break
                if ok:
                    print("[DBG ANCHOR] PARTIAL MATCH ->", (r, c), "raw=", repr(raw), "norm=", repr(nv))
                    return (r, c)

        print("[DBG ANCHOR] NO MATCH for", repr(day_text), "norm=", repr(target_n))
        return None

    def _get_day_block_meta(self, grid: List[List[str]], day_text: str) -> Optional[Dict[str, Any]]:
        anchor = self._find_day_anchor(grid, day_text)
        if not anchor:
            return None

        day_row, anchor_col = anchor
        header_row = day_row + 1
        if header_row >= len(grid):
            return None

        header = grid[header_row]

        time_col = None
        for c in range(max(0, anchor_col - 2), min(len(header), anchor_col + 8)):
            if _norm(header[c]) == "horarios":
                time_col = c
                break

        if time_col is None:
            return None

        barber_cols: Dict[str, int] = {}
        c = time_col + 1
        empty_run = 0

        while c < len(header):
            hv = _norm(header[c])

            if hv == "horarios":
                break

            if hv == "":
                empty_run += 1
                if empty_run >= 2:
                    break
                c += 1
                continue

            empty_run = 0
            barber_cols[hv] = c
            c += 1

        return {
            "day_row": day_row,
            "header_row": header_row,
            "time_col": time_col,
            "times_start_row": day_row + 2,
            "barber_cols": barber_cols,
        }

    def _find_barber_col(self, grid, header_row, start_col, barber):
        b = _norm(barber)
        if header_row < 0 or header_row >= len(grid):
            return None

        header = grid[header_row]
        for c in range(max(0, start_col), len(header)):
            hv = _norm(header[c])
            if hv == "horarios" and c > start_col:
                break
            if hv == b:
                return c
        return None

    def _find_time_row(self, grid: List[List[str]], time_col: int, start_row: int, time_hhmm: str) -> Optional[int]:
        t = _normalize_hhmm_safe(time_hhmm)
        if t is None:
            return None

        r = start_row
        while r < len(grid):
            raw = grid[r][time_col]
            v = _normalize_hhmm_safe(raw)

            if str(raw or "").strip() == "":
                return None

            if v == t:
                return r

            r += 1

        return None

    def _cell_is_free(self, v: str) -> bool:
        return _cell_is_free(v)

    def _get_sheet_id_for_tab(self, tab: str) -> int:
        tab = tab.strip()
        if tab in self._sheet_id_cache:
            return int(self._sheet_id_cache[tab])

        meta = self._service.spreadsheets().get(spreadsheetId=self._spreadsheet_id).execute()
        sheets = meta.get("sheets") or []
        for sh in sheets:
            props = sh.get("properties") or {}
            title = str(props.get("title") or "").strip()
            sid = props.get("sheetId")
            if title == tab:
                self._sheet_id_cache[tab] = int(sid)
                return int(sid)

        raise RuntimeError(f"No encontré la pestaña/tab '{tab}' en el spreadsheet")

    def _cell_is_ignored(
        self,
        *,
        tab: str,
        sheet_id: int,
        row: int,
        col: int,
        ignore_range: Optional[Dict[str, Any]],
    ) -> bool:
        """
        Ignora únicamente las celdas que pertenecen al turno viejo
        al momento de reprogramar.
        """
        if not ignore_range:
            return False

        try:
            ign_tab = str(ignore_range.get("tab") or "")
            ign_sheet_id = int(ignore_range.get("sheet_id"))
            ign_row = int(ignore_range.get("row"))
            ign_col = int(ignore_range.get("col"))
            ign_blocks = int(ignore_range.get("blocks") or 1)
        except Exception:
            return False

        if ign_blocks <= 0:
            ign_blocks = 1

        if str(tab) != ign_tab:
            return False

        if int(sheet_id) != ign_sheet_id:
            return False

        if int(col) != ign_col:
            return False

        return ign_row <= int(row) < (ign_row + ign_blocks)

    def _iter_days_in_month(self, grid: List[List[str]]) -> List[str]:
        seen: Dict[int, str] = {}
        for row in grid:
            for v in row:
                vv = str(v or "").strip()
                if not vv:
                    continue
                nv = _norm(vv)
                if any(d in nv for d in _DOW_WORDS):
                    dn = _extract_day_num(vv)
                    if dn is not None:
                        seen[dn] = vv.upper()
        return [seen[k] for k in sorted(seen.keys())]

    def _adjust_start_col_to_horarios(self, grid: List[List[str]], header_row: int, start_col: int) -> int:
        for c in range(start_col, min(start_col + 7, len(grid[header_row]))):
            if _norm(grid[header_row][c]) == "horarios":
                return c
        return start_col

    def list_days_in_month(self, *, day_text: str) -> List[str]:
        tab, _ = _resolve_sheet_target(day_text)
        grid = self._get_month_grid(tab)
        return self._iter_days_in_month(grid)

    def iter_days_from(self, *, day_text: str) -> List[str]:
        tab, day_text_eff = _resolve_sheet_target(day_text)
        grid = self._get_month_grid(tab)
        days = self._iter_days_in_month(grid)

        target_num = _extract_day_num(day_text_eff)
        if target_num is None:
            return days

        for i, d in enumerate(days):
            if _extract_day_num(d) == target_num:
                return days[i:]

        return days

    def is_day_fully_absent_x(self, *, barber: str, day_text: str) -> bool:
        tab, day_text_eff = _resolve_sheet_target(day_text)
        grid = self._get_month_grid(tab)

        meta = self._get_day_block_meta(grid, day_text_eff)
        if not meta:
            return False

        barber_col = meta["barber_cols"].get(_norm(barber))
        if barber_col is None:
            return False

        time_col = meta["time_col"]
        r = meta["times_start_row"]

        total = 0
        x_count = 0
        while r < len(grid):
            raw = grid[r][time_col]
            hhmm = _normalize_hhmm_safe(raw)

            if str(raw or "").strip() == "":
                break
            if hhmm is None:
                break

            total += 1
            if _cell_is_x(grid[r][barber_col]):
                x_count += 1
            r += 1

        return total > 0 and x_count == total

    # -------------------------
    # API pública (lectura)
    # -------------------------

    def get_free_times_for_day(self, *, barber: str, day_text: str) -> List[str]:
        tab, day_text_eff = _resolve_sheet_target(day_text)
        grid = self._get_month_grid(tab)

        meta = self._get_day_block_meta(grid, day_text_eff)
        if not meta:
            print("[DBG SHEETS] get_free_times_for_day: NO META", "tab=", tab, "day_text=", repr(day_text), "effective_day_text=", repr(day_text_eff))
            return []

        barber_key = _norm(barber)
        barber_col = meta["barber_cols"].get(barber_key)
        if barber_col is None:
            print("[DBG SHEETS] get_free_times_for_day: NO BARBER COL",
                  "tab=", tab, "day_text=", repr(day_text), "effective_day_text=", repr(day_text_eff),
                  "barber=", repr(barber),
                  "barber_cols=", meta["barber_cols"])
            return []

        time_col = meta["time_col"]
        r = meta["times_start_row"]

        free_times: List[str] = []
        while r < len(grid):
            raw = grid[r][time_col]
            hhmm = _normalize_hhmm_safe(raw)

            if str(raw or "").strip() == "":
                break
            if hhmm is None:
                break

            if _cell_is_free(grid[r][barber_col]):
                free_times.append(hhmm)

            r += 1

        print("[DBG SHEETS] get_free_times_for_day: OK",
              "tab=", tab, "day_text=", repr(day_text), "effective_day_text=", repr(day_text_eff),
              "barber=", repr(barber),
              "time_col=", time_col, "barber_col=", barber_col,
              "free_times_len=", len(free_times),
              "sample=", free_times[:8])

        return free_times

    def is_slot_free(
        self,
        *,
        barber: str,
        day_text: str,
        time_hhmm: str,
        blocks: int = 1,
        ignore_range: Optional[Dict[str, Any]] = None,
    ) -> bool:
        tab, day_text_eff = _resolve_sheet_target(day_text)
        grid = self._get_month_grid(tab)

        meta = self._get_day_block_meta(grid, day_text_eff)
        if not meta:
            return False

        barber_col = meta["barber_cols"].get(_norm(barber))
        if barber_col is None:
            return False

        time_col = meta["time_col"]
        start_time_row = self._find_time_row(grid, time_col, meta["times_start_row"], time_hhmm)
        if start_time_row is None:
            return False

        b = max(1, int(blocks or 1))
        sheet_id = self._get_sheet_id_for_tab(tab)

        for i in range(b):
            rr = start_time_row + i
            if rr >= len(grid):
                return False

            hhmm_expected = _add_minutes_hhmm(time_hhmm, 30 * i)
            hhmm_cell = _normalize_hhmm_safe(grid[rr][time_col])

            if hhmm_cell != hhmm_expected:
                return False

            if not _cell_is_free(grid[rr][barber_col]):
                if not self._cell_is_ignored(
                    tab=tab,
                    sheet_id=sheet_id,
                    row=rr,
                    col=barber_col,
                    ignore_range=ignore_range,
                ):
                    return False

        return True
    def get_day_windows(
        self,
        *,
        barber: str,
        day_text: str,
        blocks: int,
        ignore_range: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        tab, day_text_eff = _resolve_sheet_target(day_text)
        grid = self._get_month_grid(tab)

        meta = self._get_day_block_meta(grid, day_text_eff)
        if not meta:
            print("[DBG] NO META day_text=", repr(day_text), "effective_day_text=", repr(day_text_eff), "tab=", tab)
            return []

        barber_col = meta["barber_cols"].get(_norm(barber))
        if barber_col is None:
            return []

        time_col = meta["time_col"]
        times_start_row = meta["times_start_row"]
        sheet_id = self._get_sheet_id_for_tab(tab)

        print("[DBG] META=", meta, "day_text=", repr(day_text), "effective_day_text=", repr(day_text_eff), "tab=", tab)
        print("[DBG] HEADER CELLS BLOCK=", meta["barber_cols"])
        print(
            "[DBG] FIRST TIME CELL raw=",
            repr(grid[times_start_row][time_col]),
            "at row=",
            times_start_row,
            "col=",
            time_col,
        )
        print("[DBG] IGNORE RANGE get_day_windows=", ignore_range)

        b = max(1, int(blocks or 1))
        windows: List[str] = []

        times: List[str] = []
        occ: List[bool] = []

        r = times_start_row
        while r < len(grid):
            raw = grid[r][time_col]
            if str(raw or "").strip() == "":
                break

            hhmm = _normalize_hhmm_safe(raw)
            if hhmm is None:
                break

            times.append(hhmm)

            occupied = _cell_occupied(grid[r][barber_col])
            if occupied and self._cell_is_ignored(
                tab=tab,
                sheet_id=sheet_id,
                row=r,
                col=barber_col,
                ignore_range=ignore_range,
            ):
                occupied = False

            occ.append(occupied)
            r += 1

        latest_start = _latest_start_for_blocks(b)
        latest_start_min = _hhmm_to_minutes(latest_start)

        for i in range(0, len(times) - b + 1):
            ok = True
            for j in range(b):
                if occ[i + j]:
                    ok = False
                    break

                expected = _add_minutes_hhmm(times[i], 30 * j)
                if times[i + j] != expected:
                    ok = False
                    break

            if ok:
                start_min = _hhmm_to_minutes(times[i])
                if start_min <= latest_start_min:
                    windows.append(times[i])

        return windows

    def count_booked_slots(self, *, barber: str, day_text: str) -> int:
        tab, day_text_eff = _resolve_sheet_target(day_text)
        grid = self._get_month_grid(tab)

        meta = self._get_day_block_meta(grid, day_text_eff)
        if not meta:
            return 0

        barber_col = meta["barber_cols"].get(_norm(barber))
        if barber_col is None:
            return 0

        time_col = meta["time_col"]
        r = meta["times_start_row"]

        booked = 0
        while r < len(grid):
            raw = grid[r][time_col]
            hhmm = _normalize_hhmm_safe(raw)

            if str(raw or "").strip() == "":
                break
            if hhmm is None:
                break

            if _cell_has_customer(grid[r][barber_col]):
                booked += 1
            r += 1

        return booked

    def is_day_fully_blocked(self, *, barber: str, day_text: str) -> bool:
        """
        Compatibilidad: día completamente ocupado por cualquier texto (X o clientes).
        """
        tab, day_text_eff = _resolve_sheet_target(day_text)
        grid = self._get_month_grid(tab)

        meta = self._get_day_block_meta(grid, day_text_eff)
        if not meta:
            return False

        barber_col = meta["barber_cols"].get(_norm(barber))
        if barber_col is None:
            return False

        time_col = meta["time_col"]
        r = meta["times_start_row"]

        total = 0
        blocked = 0
        while r < len(grid):
            raw = grid[r][time_col]
            hhmm = _normalize_hhmm_safe(raw)

            if str(raw or "").strip() == "":
                break
            if hhmm is None:
                break

            total += 1
            if _cell_occupied(grid[r][barber_col]):
                blocked += 1
            r += 1

        return total > 0 and blocked == total

    def get_barber_status(self, *, barber: str, day_text: str) -> str:
        """
        working | absent | vacation

        Regla:
          - 1..4 días completos con X consecutivos => absent
          - 5+ días completos con X consecutivos => vacation
          - 0 => working
        """
        tab, day_text_eff = _resolve_sheet_target(day_text)
        grid = self._get_month_grid(tab)
        days = self._iter_days_in_month(grid)

        target_num = _extract_day_num(day_text_eff)
        if target_num is None:
            return "working"

        idx = None
        for i, d in enumerate(days):
            if _extract_day_num(d) == target_num:
                idx = i
                break
        if idx is None:
            return "working"

        streak = 0
        for j in range(idx, len(days)):
            if self.is_day_fully_absent_x(barber=barber, day_text=days[j]):
                streak += 1
            else:
                break

        if streak >= 5:
            return "vacation"
        if streak >= 1:
            return "absent"
        return "working"

    # -------------------------
    # ESCRITURA REAL
    # -------------------------

    def paint_blocks(
        self,
        *,
        barber: str,
        day_text: str,
        time_hhmm: str,
        blocks: int,
        customer_name: str,
        rgb: Optional[Dict[str, float]] = None,
        ignore_range: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        tab, day_text_eff = _resolve_sheet_target(day_text)
        grid = self._get_month_grid(tab)

        meta = self._get_day_block_meta(grid, day_text_eff)
        if not meta:
            return {"ok": False, "error": "No encontré el día en el sheet", "tab": tab, "effective_day_text": day_text_eff}

        barber_col = meta["barber_cols"].get(_norm(barber))
        if barber_col is None:
            return {"ok": False, "error": "No encontré columna del barbero", "tab": tab, "effective_day_text": day_text_eff}

        time_col = meta["time_col"]
        start_time_row = self._find_time_row(grid, time_col, meta["times_start_row"], time_hhmm)
        if start_time_row is None:
            return {"ok": False, "error": "No encontré la hora en el bloque del día", "tab": tab, "effective_day_text": day_text_eff}

        b = max(1, int(blocks or 1))
        sheet_id = self._get_sheet_id_for_tab(tab)

        for i in range(b):
            rr = start_time_row + i
            hhmm_expected = _add_minutes_hhmm(time_hhmm, 30 * i)
            hhmm_cell = _normalize_hhmm_safe(grid[rr][time_col])

            if hhmm_cell != hhmm_expected:
                return {"ok": False, "error": "El bloque de horas no coincide (layout cortado)", "tab": tab, "effective_day_text": day_text_eff}

            if not _cell_is_free(grid[rr][barber_col]):
                if not self._cell_is_ignored(
                    tab=tab,
                    sheet_id=sheet_id,
                    row=rr,
                    col=barber_col,
                    ignore_range=ignore_range,
                ):
                    return {"ok": False, "error": "El turno ya no está libre", "tab": tab, "effective_day_text": day_text_eff}

        rows_payload = []
        for i in range(b):
            txt = customer_name if i == 0 else "X"
            cell: Dict[str, Any] = {
                "userEnteredValue": {"stringValue": str(txt)},
            }
            if rgb:
                cell["userEnteredFormat"] = {
                    "backgroundColor": {
                        "red": float(rgb.get("red", 1.0)),
                        "green": float(rgb.get("green", 1.0)),
                        "blue": float(rgb.get("blue", 1.0)),
                    }
                }
            rows_payload.append({"values": [cell]})

        req = {
            "updateCells": {
                "range": {
                    "sheetId": int(sheet_id),
                    "startRowIndex": int(start_time_row),
                    "endRowIndex": int(start_time_row + b),
                    "startColumnIndex": int(barber_col),
                    "endColumnIndex": int(barber_col + 1),
                },
                "rows": rows_payload,
                "fields": "userEnteredValue,userEnteredFormat.backgroundColor",
            }
        }

        try:
            (
                self._service.spreadsheets()
                .batchUpdate(spreadsheetId=self._spreadsheet_id, body={"requests": [req]})
                .execute()
            )
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "tab": tab, "effective_day_text": day_text_eff}

        self._invalidate_tab_cache(tab)

        return {
            "ok": True,
            "tab": tab,
            "sheet_id": int(sheet_id),
            "row": int(start_time_row),
            "col": int(barber_col),
            "blocks": int(b),
            "effective_day_text": day_text_eff,
        }

    def clear_blocks(self, *, tab: str, sheet_id: int, row: int, col: int, blocks: int) -> bool:
        b = max(1, int(blocks or 1))

        rows_payload = [
            {
                "values": [
                    {
                        "userEnteredValue": {"stringValue": ""},
                        "userEnteredFormat": {
                            "backgroundColor": {
                                "red": 1,
                                "green": 1,
                                "blue": 1
                            }
                        },
                    }
                ]
            }
            for _ in range(b)
        ]

        req = {
            "updateCells": {
                "range": {
                    "sheetId": int(sheet_id),
                    "startRowIndex": int(row),
                    "endRowIndex": int(row + b),
                    "startColumnIndex": int(col),
                    "endColumnIndex": int(col + 1),
                },
                "rows": rows_payload,
                "fields": "userEnteredValue,userEnteredFormat.backgroundColor",
            }
        }

        try:
            (
                self._service.spreadsheets()
                .batchUpdate(spreadsheetId=self._spreadsheet_id, body={"requests": [req]})
                .execute()
            )
            self._invalidate_tab_cache(tab)
            return True
        except Exception:
            return False


_SHEETS_REPO_SINGLETON: Optional[GoogleSheetsRepo] = None


def get_sheets_repo() -> SheetsRepo:
    global _SHEETS_REPO_SINGLETON
    if (getattr(settings, "SHEETS_MODE", "") or "").lower() == "google":
        if _SHEETS_REPO_SINGLETON is None:
            _SHEETS_REPO_SINGLETON = GoogleSheetsRepo()
        return _SHEETS_REPO_SINGLETON
    return MockSheetsRepo()