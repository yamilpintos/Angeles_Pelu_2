from __future__ import annotations

import os
import traceback
from pathlib import Path
from time import perf_counter
from dotenv import load_dotenv

_BOOT_TS = perf_counter()


def _ms_since_start() -> str:
    return f"{((perf_counter() - _BOOT_TS) * 1000):.2f} ms"


def _dbg(msg: str) -> None:
    print(f"[SETTINGS DBG][{_ms_since_start()}] {msg}", flush=True)


def _mask_value(key: str, value: str) -> str:
    if value is None:
        return "None"

    value = str(value)

    secret_markers = (
        "TOKEN",
        "KEY",
        "SECRET",
        "PASSWORD",
        "PASS",
        "ACCOUNT_SID",
        "SERVICE_ROLE",
        "JSON",
    )

    if any(marker in key.upper() for marker in secret_markers):
        if not value:
            return '"" (empty)'
        if len(value) <= 8:
            return f"{value[:2]}*** (len={len(value)})"
        return f"{value[:4]}...{value[-4:]} (len={len(value)})"

    if value == "":
        return '"" (empty)'

    return repr(value)


_dbg("=== INICIO settings.py ===")
_dbg(f"__file__ = {__file__}")
_dbg(f"cwd = {Path.cwd()}")

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = BASE_DIR / ".env"

_dbg(f"BASE_DIR = {BASE_DIR}")
_dbg(f"ENV_PATH = {ENV_PATH}")
_dbg(f"ENV_PATH exists = {ENV_PATH.exists()}")

if ENV_PATH.exists():
    try:
        stat = ENV_PATH.stat()
        _dbg(f".env size = {stat.st_size} bytes")
        _dbg(f".env mtime = {stat.st_mtime}")
    except Exception as e:
        _dbg(f"No pude leer stat del .env: {e!r}")

_load_ts = perf_counter()
load_result = load_dotenv(dotenv_path=ENV_PATH, override=True)
_dbg(
    f"load_dotenv(override=True) => {load_result} "
    f"(tardó {((perf_counter() - _load_ts) * 1000):.2f} ms)"
)


# =========================================================
# Helper env
# =========================================================
def env(key: str, default: str | None = None) -> str:
    _t0 = perf_counter()
    raw = os.getenv(key)
    used_default = raw is None

    if raw is None:
        raw = default

    _dbg(
        f"env('{key}') -> source={'default' if used_default else 'os.environ'}; "
        f"value={_mask_value(key, raw)}"
    )

    if raw is None:
        _dbg(f"env('{key}') ERROR: missing and no default")
        raise RuntimeError(f"Missing env var: {key}")

    out = str(raw)
    _dbg(
        f"env('{key}') final={_mask_value(key, out)} "
        f"(tardó {((perf_counter() - _t0) * 1000):.2f} ms)"
    )
    return out


def env_opt(key: str, default: str | None = None) -> str:
    _t0 = perf_counter()
    raw = os.getenv(key)
    used_default = raw is None

    if raw is None:
        raw = default

    out = "" if raw is None else str(raw)

    _dbg(
        f"env_opt('{key}') -> source={'default' if used_default else 'os.environ'}; "
        f"raw={_mask_value(key, raw)}; final={_mask_value(key, out)} "
        f"(tardó {((perf_counter() - _t0) * 1000):.2f} ms)"
    )
    return out


# =========================================================
# Settings
# =========================================================
_dbg("Definiendo clase Settings...")


class Settings:
    _class_build_ts = perf_counter()
    _dbg("Entrando a evaluación de atributos de clase Settings")

    # -----------------------------------------------------
    # App
    # -----------------------------------------------------
    APP_ENV: str = env("APP_ENV", "dev")

    TIMEZONE: str = env("TIMEZONE", "America/Argentina/Buenos_Aires")
    SALON_NAME: str = env("SALON_NAME", "Peluquería Ángeles Unisex")

    BARBERS: list[str] = [
        x.strip() for x in env("BARBERS", "Franco,Sergio,Luka").split(",") if x.strip()
    ]
    _dbg(f"BARBERS parseado = {BARBERS!r}")

    # -----------------------------------------------------
    # OpenAI
    # -----------------------------------------------------
    OPENAI_API_KEY: str = env_opt("OPENAI_API_KEY", "").strip()
    OPENAI_MODEL: str = env("OPENAI_MODEL", "gpt-4o-mini").strip()

    # -----------------------------------------------------
    # Google Sheets
    # -----------------------------------------------------
    SHEETS_MODE: str = env("SHEETS_MODE", "mock").lower().strip()
    SHEETS_SPREADSHEET_ID: str = env_opt("SHEETS_SPREADSHEET_ID", "").strip()
    GOOGLE_SERVICE_ACCOUNT_JSON: str = env_opt("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    SHEETS_MONTH_TAB: str = env_opt("SHEETS_MONTH_TAB", "").strip().upper()
    SHEETS_GRID_TTL_SECONDS: int = int(env("SHEETS_GRID_TTL_SECONDS", "60"))
    AVAILABILITY_CACHE_TTL_SECONDS: int = int(env("AVAILABILITY_CACHE_TTL_SECONDS", "60"))

    # -----------------------------------------------------
    # Supabase
    # -----------------------------------------------------
    BOOKINGS_MODE: str = env("BOOKINGS_MODE", "supabase").lower().strip()
    SUPABASE_URL: str = env_opt("SUPABASE_URL", "").strip()
    SUPABASE_SERVICE_ROLE_KEY: str = env_opt("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    SUPABASE_BOOKINGS_TABLE: str = env("SUPABASE_BOOKINGS_TABLE", "bookings").strip()
    SUPABASE_SESSIONS_TABLE: str = env("SUPABASE_SESSIONS_TABLE", "chat_sessions").strip()

    # -----------------------------------------------------
    # WhatsApp Cloud API
    # -----------------------------------------------------
    WHATSAPP_ACCESS_TOKEN: str = env_opt("WHATSAPP_ACCESS_TOKEN", "").strip()
    WHATSAPP_PHONE_NUMBER_ID: str = env_opt("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    WHATSAPP_WABA_ID: str = env_opt("WHATSAPP_WABA_ID", "").strip()
    WHATSAPP_VERIFY_TOKEN: str = env_opt("WHATSAPP_VERIFY_TOKEN", "").strip()
    WHATSAPP_API_VERSION: str = env("WHATSAPP_API_VERSION", "v24.0").strip()

    # -----------------------------------------------------
    # Twilio legado
    # -----------------------------------------------------
    TWILIO_ACCOUNT_SID: str = env_opt("TWILIO_ACCOUNT_SID", "").strip()
    TWILIO_AUTH_TOKEN: str = env_opt("TWILIO_AUTH_TOKEN", "").strip()
    TWILIO_WHATSAPP_FROM: str = env_opt("TWILIO_WHATSAPP_FROM", "").strip()

    _dbg(
        "Atributos de clase Settings evaluados "
        f"(tardó {((perf_counter() - _class_build_ts) * 1000):.2f} ms)"
    )

    def __init__(self) -> None:
        _dbg("Instanciando Settings()")
        _dbg(
            "Resumen inicial: "
            f"APP_ENV={self.APP_ENV!r}, "
            f"TIMEZONE={self.TIMEZONE!r}, "
            f"SHEETS_MODE={self.SHEETS_MODE!r}, "
            f"BOOKINGS_MODE={self.BOOKINGS_MODE!r}, "
            f"OPENAI_MODEL={self.OPENAI_MODEL!r}, "
            f"WHATSAPP_API_VERSION={self.WHATSAPP_API_VERSION!r}"
        )

    # -----------------------------------------------------
    # Validaciones
    # -----------------------------------------------------
    def validate(self) -> None:
        _v0 = perf_counter()
        _dbg("=== INICIO validate() ===")

        _dbg(
            "Estado antes de validar: "
            f"SHEETS_MODE={self.SHEETS_MODE!r}, "
            f"BOOKINGS_MODE={self.BOOKINGS_MODE!r}, "
            f"APP_ENV={self.APP_ENV!r}"
        )

        if self.SHEETS_MODE == "google":
            _dbg("Validando bloque Google Sheets...")
            _dbg(
                f"SHEETS_SPREADSHEET_ID={_mask_value('SHEETS_SPREADSHEET_ID', self.SHEETS_SPREADSHEET_ID)}"
            )
            _dbg(
                "GOOGLE_SERVICE_ACCOUNT_JSON="
                f"{_mask_value('GOOGLE_SERVICE_ACCOUNT_JSON', self.GOOGLE_SERVICE_ACCOUNT_JSON)}"
            )
            if not self.SHEETS_SPREADSHEET_ID:
                _dbg("ERROR: falta SHEETS_SPREADSHEET_ID")
                raise RuntimeError("Falta SHEETS_SPREADSHEET_ID en .env")
            if not self.GOOGLE_SERVICE_ACCOUNT_JSON:
                _dbg("ERROR: falta GOOGLE_SERVICE_ACCOUNT_JSON")
                raise RuntimeError("Falta GOOGLE_SERVICE_ACCOUNT_JSON en .env")
            _dbg("OK bloque Google Sheets")

        if self.APP_ENV.lower() == "prod":
            _dbg("Validando bloque prod/OpenAI...")
            _dbg(f"OPENAI_API_KEY={_mask_value('OPENAI_API_KEY', self.OPENAI_API_KEY)}")
            if not self.OPENAI_API_KEY:
                _dbg("ERROR: falta OPENAI_API_KEY")
                raise RuntimeError("Falta OPENAI_API_KEY en .env (prod)")
            _dbg("OK bloque prod/OpenAI")

        if self.BOOKINGS_MODE == "supabase":
            _dbg("Validando bloque Supabase...")
            _dbg(f"SUPABASE_URL={_mask_value('SUPABASE_URL', self.SUPABASE_URL)}")
            _dbg(
                "SUPABASE_SERVICE_ROLE_KEY="
                f"{_mask_value('SUPABASE_SERVICE_ROLE_KEY', self.SUPABASE_SERVICE_ROLE_KEY)}"
            )
            if not self.SUPABASE_URL:
                _dbg("ERROR: falta SUPABASE_URL")
                raise RuntimeError("Falta SUPABASE_URL en .env")
            if not self.SUPABASE_SERVICE_ROLE_KEY:
                _dbg("ERROR: falta SUPABASE_SERVICE_ROLE_KEY")
                raise RuntimeError("Falta SUPABASE_SERVICE_ROLE_KEY en .env")
            _dbg("OK bloque Supabase")

        _dbg("Validando WhatsApp webhook...")
        _dbg(
            f"WHATSAPP_VERIFY_TOKEN={_mask_value('WHATSAPP_VERIFY_TOKEN', self.WHATSAPP_VERIFY_TOKEN)}"
        )
        if not self.WHATSAPP_VERIFY_TOKEN:
            _dbg("ERROR: falta WHATSAPP_VERIFY_TOKEN")
            raise RuntimeError("Falta WHATSAPP_VERIFY_TOKEN en .env")
        _dbg("OK verify token")

        sending_config_started = bool(
            self.WHATSAPP_ACCESS_TOKEN or self.WHATSAPP_PHONE_NUMBER_ID
        )
        _dbg(f"sending_config_started = {sending_config_started}")

        if sending_config_started:
            _dbg("Validando bloque de envío WhatsApp...")
            _dbg(
                f"WHATSAPP_ACCESS_TOKEN={_mask_value('WHATSAPP_ACCESS_TOKEN', self.WHATSAPP_ACCESS_TOKEN)}"
            )
            _dbg(
                "WHATSAPP_PHONE_NUMBER_ID="
                f"{_mask_value('WHATSAPP_PHONE_NUMBER_ID', self.WHATSAPP_PHONE_NUMBER_ID)}"
            )
            if not self.WHATSAPP_ACCESS_TOKEN:
                _dbg("ERROR: falta WHATSAPP_ACCESS_TOKEN")
                raise RuntimeError("Falta WHATSAPP_ACCESS_TOKEN en .env")
            if not self.WHATSAPP_PHONE_NUMBER_ID:
                _dbg("ERROR: falta WHATSAPP_PHONE_NUMBER_ID")
                raise RuntimeError("Falta WHATSAPP_PHONE_NUMBER_ID en .env")
            _dbg("OK bloque de envío WhatsApp")

        _dbg(f"=== validate() OK ({((perf_counter() - _v0) * 1000):.2f} ms) ===")


try:
    _dbg("Creando settings = Settings()")
    settings = Settings()

    _dbg("Ejecutando settings.validate()")
    settings.validate()

    _dbg("=== settings.py cargado correctamente ===")
    _dbg(f"Tiempo total bootstrap settings.py = {_ms_since_start()}")

except Exception as e:
    _dbg("=== ERROR durante carga de settings.py ===")
    _dbg(f"Tipo de error: {type(e).__name__}")
    _dbg(f"Mensaje: {e}")
    traceback.print_exc()
    raise