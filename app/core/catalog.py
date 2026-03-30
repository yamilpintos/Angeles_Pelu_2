# app/core/catalog.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

# Google Sheets usa colores RGB en rango 0..1
RGB = Tuple[float, float, float]


def _rgb255(r: int, g: int, b: int) -> RGB:
    return (r / 255.0, g / 255.0, b / 255.0)


def format_price(value: int) -> str:
    return f"${value:,.0f}".replace(",", ".")


@dataclass(frozen=True)
class ServiceItem:
    key: str
    display_name: str
    duration_min: int
    duration_text: str
    price: int
    price_senior: int  # jubilados o +65
    rgb: RGB
    allowed_barbers: Optional[List[str]] = None

    @property
    def blocks(self) -> int:
        # 30 minutos por bloque (redondeo hacia arriba)
        return max(1, (self.duration_min + 29) // 30)

    @property
    def is_senior_discounted(self) -> bool:
        return self.price_senior < self.price


# =========================================================
# CATÁLOGO (SERVICIOS MASCULINOS)
# =========================================================
SERVICES: Dict[str, ServiceItem] = {
    "CORTE_HOMBRE_NINO": ServiceItem(
        key="CORTE_HOMBRE_NINO",
        display_name="Corte Hombre/Niño",
        duration_min=30,
        duration_text="30 minutos",
        price=20000,
        price_senior=18000,
        rgb=_rgb255(198, 239, 206),  # verde suave
    ),
    "SOLO_DEGRADE": ServiceItem(
        key="SOLO_DEGRADE",
        display_name="Solo Degrade",
        duration_min=30,
        duration_text="30 minutos",
        price=18000,
        price_senior=16000,
        rgb=_rgb255(189, 215, 238),  # azul suave
    ),
    "CORTE_CON_LAVADO": ServiceItem(
        key="CORTE_CON_LAVADO",
        display_name="Corte c/ Lavado",
        duration_min=30,
        duration_text="30 minutos",
        price=25000,
        price_senior=22000,
        rgb=_rgb255(255, 242, 204),  # amarillo suave
    ),
    "RAPADO_HOMBRE": ServiceItem(
        key="RAPADO_HOMBRE",
        display_name="Rapado Hombre",
        duration_min=30,
        duration_text="30 minutos",
        price=15000,
        price_senior=15000,
        rgb=_rgb255(244, 204, 204),  # rojo suave
    ),
    "BARBA": ServiceItem(
        key="BARBA",
        display_name="Barba",
        duration_min=30,
        duration_text="30 minutos",
        price=15000,
        price_senior=15000,
        rgb=_rgb255(217, 210, 233),  # violeta suave
    ),
    "BARBA_CON_PANO": ServiceItem(
        key="BARBA_CON_PANO",
        display_name="Barba con Paño",
        duration_min=30,
        duration_text="30 minutos",
        price=20000,
        price_senior=18000,
        rgb=_rgb255(208, 224, 227),  # celeste/gris suave
    ),
    "CORTE_MAS_BARBA": ServiceItem(
        key="CORTE_MAS_BARBA",
        display_name="Corte + Barba",
        duration_min=60,
        duration_text="1 Hora",
        price=25000,
        price_senior=22000,
        rgb=_rgb255(142, 169, 219),  # azul más marcado
    ),
    "CORTE_MAS_PANO": ServiceItem(
        key="CORTE_MAS_PANO",
        display_name="Corte + Paño",
        duration_min=60,
        duration_text="1 Hora",
        price=30000,
        price_senior=28000,
        rgb=_rgb255(180, 167, 214),  # violeta más marcado
    ),
    "RAPADO_MAS_BARBA": ServiceItem(
        key="RAPADO_MAS_BARBA",
        display_name="Rapado + Barba",
        duration_min=30,
        duration_text="30 minutos",
        price=22000,
        price_senior=20000,
        rgb=_rgb255(248, 203, 173),  # naranja suave
    ),
    "COLOR_MECHAS_GLOBAL_MAS_CORTE": ServiceItem(
        key="COLOR_MECHAS_GLOBAL_MAS_CORTE",
        display_name="Color (Mechas/Global) + corte",
        duration_min=360,  # 6 horas
        duration_text="6 Horas",
        price=80000,
        price_senior=80000,
        rgb=_rgb255(255, 229, 153),  # amarillo más fuerte
        allowed_barbers=["Franco", "Sergio"],
    ),
}


# =========================================================
# API PÚBLICA (para booking / sheets / dialogue)
# =========================================================

def get_service(service_key: str) -> Optional[ServiceItem]:
    return SERVICES.get((service_key or "").strip())


def blocks_for(service_key: str) -> int:
    it = get_service(service_key)
    return it.blocks if it else 1


def rgb_for(service_key: str) -> Optional[RGB]:
    it = get_service(service_key)
    return it.rgb if it else None


def allowed_barbers_for(service_key: str) -> Optional[List[str]]:
    it = get_service(service_key)
    if not it or not it.allowed_barbers:
        return None
    return list(it.allowed_barbers)


def price_for(service_key: str, *, age: Optional[int] = None) -> Optional[int]:
    """
    Devuelve precio según edad (>=65 -> price_senior).
    Si no hay service_key válido, devuelve None.
    """
    it = get_service(service_key)
    if not it:
        return None
    if age is not None and age >= 65:
        return it.price_senior
    return it.price


def services_prompt_text() -> str:
    """
    Texto para inyectar en Dialogue.
    La IA debe elegir SOLO una service_key válida de esta lista.
    También se informa la política de precios para jubilados / +65.
    """
    lines: List[str] = []
    lines.append("SERVICIOS_MASCULINOS_DISPONIBLES (elegí SOLO una service_key de esta lista):")

    for it in SERVICES.values():
        line = (
            f'- {it.key} = "{it.display_name}" '
            f'| duración: {it.duration_text} '
            f'| bloques: {it.blocks} '
            f'| precio: {format_price(it.price)} '
            f'| jubilados o +65: {format_price(it.price_senior)}'
        )
        if it.allowed_barbers:
            line += f' | peluqueros habilitados: {", ".join(it.allowed_barbers)}'
        lines.append(line)

    lines.append("")
    lines.append("REGLA COMERCIAL:")
    lines.append("- Algunos servicios tienen precio diferencial para jubilados o clientes de 65 años o más.")
    lines.append("- Si age >= 65, al informar precios podés usar el valor 'jubilados o +65'.")
    lines.append("- No inventes descuentos: usá solo los valores indicados en este catálogo.")
    lines.append("- Si un servicio tiene peluqueros habilitados específicos, no asignes ni confirmes otro peluquero para ese servicio.")
    lines.append("- Si el cliente pide 'cualquiera', interpretalo dentro de los peluqueros habilitados para ese servicio, si existe esa restricción.")

    return "\n".join(lines)


def service_rules_prompt_text() -> str:
    """
    Reglas específicas por servicio para inyectar en Dialogue
    como contexto adicional de negocio.
    """
    lines: List[str] = []
    lines.append("RESTRICCIONES_DE_SERVICIO_Y_PELUQUERO:")

    has_rules = False
    for it in SERVICES.values():
        if it.allowed_barbers:
            has_rules = True
            lines.append(
                f'- "{it.display_name}" ({it.key}) solo puede realizarse con: {", ".join(it.allowed_barbers)}.'
            )

    if not has_rules:
        lines.append("- No hay restricciones especiales de peluquero por servicio.")

    lines.append("")
    lines.append("INSTRUCCIONES:")
    lines.append("- Si el cliente elige un peluquero no permitido para ese servicio, no lo tomes como válido.")
    lines.append("- En ese caso, pedí o sugerí uno de los peluqueros habilitados.")
    lines.append("- No inventes excepciones ni flexibilices estas reglas.")

    return "\n".join(lines)


def services_human_text() -> str:
    """
    Texto amigable para enviar al usuario (si quiere ver el catálogo).
    """
    lines: List[str] = []
    lines.append("📌 Por ahora atendemos *solo servicios masculinos*.")
    lines.append("Servicios disponibles:")

    for it in SERVICES.values():
        base = f"• {it.display_name} — {format_price(it.price)}"
        base += f" | Jubilados o +65: {format_price(it.price_senior)}"
        base += f" | Duración: {it.duration_text}"

        if it.allowed_barbers:
            base += f" | Solo con: {', '.join(it.allowed_barbers)}"

        lines.append(base)

    lines.append("")
    lines.append("🎂 *Jubilados o clientes de 65 años o más* acceden al valor indicado en cada servicio cuando corresponda.")

    return "\n".join(lines)