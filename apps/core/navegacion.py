"""Navegación global de Órbita (Fase visual V0) — sin CU propio.

Hay exactamente DOS niveles de navegación global:

  NIVEL 0  Header global (identidad, búsqueda, notificaciones, menú de
           usuario con "Mi perfil" y "Cerrar sesión"). No contiene módulos.
  NIVEL 1  Dock global adaptativo: Inicio · Mis tickets · Trabajo · Más.

Tabs, segmentos y filtros dentro de una pantalla son controles locales del
módulo, no navegación global. Este archivo es la ÚNICA fuente de qué
destinos ve cada usuario; `layout/dock.html` solo los pinta.

Regla de autorización (principio del proyecto: ROL + ALCANCE + RELACIÓN CON
EL OBJETO): ningún destino se decide comparando nombres/IDs de rol. Cada uno
depende de una capacidad real que ya existe en el dominio:

  Inicio, Mis tickets   Cualquier autenticado (la relación solicitante↔Ticket
                        gobierna qué ve cada quien, no un permiso global).
  Trabajo               `tickets.atender` en algún alcance (→ Cola) O trabajo
                        personal pendiente: tareas asignadas/disponibles no
                        completadas o aprobaciones pendientes (→ Mi trabajo).
                        Si no se cumple ninguna, el destino no se renderiza y
                        el dock se recalcula sin hueco.
  Más › Gestión         Diseñador (Flujos + Servicios, antes Studio y
                        Workflows avanzados como destinos separados):
                        `catalogo.administrar` o `workflows.consultar|
                        administrar`. Qué ve cada quien dentro lo decide
                        `apps/core/disenador.py` por capacidades.
  Más › Administración  Configuración (por ahora Django Admin, interfaz
                        administrativa provisional desde Sprint 0): `is_staff`,
                        la compuerta nativa de Django Admin. No se crea un
                        Permiso de Órbita para duplicarla ni se usa
                        "Administrador" como autorización implícita.

"Más" solo se renderiza si el usuario tiene al menos un destino dentro; los
grupos vacíos tampoco. El Diseñador nunca va dentro de Trabajo.

Trabajo agrupa dos vistas hermanas — Cola (tickets disponibles para tomar por
su relación con el equipo) y Mi trabajo (lo que requiere algo del usuario).
El destino del dock abre Cola si el usuario tiene acceso a ella y, si no,
Mi trabajo; las pestañas locales solo existen cuando ambas son útiles.

Este módulo no depende del código de ningún dominio de forma permanente: las
consultas de "trabajo personal" se importan de forma perezosa y son las mismas
que ya alimentan la pantalla Mi trabajo, sin reglas nuevas.
"""

from apps.core.autorizacion import alcances_autorizados
from apps.core.disenador import accede_al_disenador

VISTA_COLA = "tickets:cola"
# Solicitar un ticket no es navegar "Mis tickets": mientras dura el recorrido
# de solicitud (entrar → completar → revisar → enviar → confirmar) ningún
# destino del dock queda resaltado por pertenecer al namespace `tickets`.
VISTAS_SOLICITUD = (
    "tickets:solicitar",
    "tickets:iniciar",
    "tickets:borrador",
    "tickets:solicitud_estado",
    "tickets:revisar",
    "tickets:enviar",
    "tickets:enviada",
)
VISTA_MI_TRABAJO = "core:mi_trabajo"

# Orden fijo de los grupos de "Más".
GRUPOS_MAS = ("Gestión", "Administración")


def _acceso_a_cola(usuario):
    """`tickets.atender` en cualquier alcance (GLOBAL, AREA o UNIDAD): no
    `usuario_tiene_permiso()` sin argumentos, que ocultaría el destino a
    quien tiene el permiso solo en un Área o Unidad."""
    alcances = alcances_autorizados(usuario, "tickets.atender")
    return bool(alcances["global"] or alcances["areas"] or alcances["unidades_negocio"])


def _tiene_trabajo_personal(usuario):
    """¿Hay algo en Mi trabajo que requiera acción del usuario? Misma
    población que lista `core.views.mi_trabajo_view` (tareas asignadas o
    disponibles para tomar, y aprobaciones pendientes), excluyendo tareas ya
    completadas: una tarea terminada no "requiere algo de mí"."""
    from apps.aprobaciones.consultas import aprobaciones_pendientes_para
    from apps.tareas.consultas import tareas_asignadas_a, tareas_disponibles_para_tomar
    from apps.tareas.models import Tarea

    if aprobaciones_pendientes_para(usuario).exists():
        return True
    return (
        tareas_asignadas_a(usuario).exclude(estado=Tarea.Estado.COMPLETADA).exists()
        or tareas_disponibles_para_tomar(usuario).exclude(estado=Tarea.Estado.COMPLETADA).exists()
    )


def _construir_elementos(usuario, acceso_cola, trabajo_personal):
    """Lista de destinos a partir de las capacidades ya resueltas (se evalúan
    una sola vez por request, ver `construir_navegacion`).

    Claves de cada destino:
      etiqueta / url_name / icono   lo que se pinta.
      destinos                      `url_name` adicionales que lo resaltan
                                    como activo (Trabajo agrupa dos vistas).
      namespaces / prefijos         resaltado por namespace de URL o por
                                    prefijo de nombre de vista (subpáginas).
      en_mas + grupo                el destino vive dentro de "Más".
    """
    elementos = [
        {"etiqueta": "Inicio", "url_name": "core:inicio", "icono": "home"},
        {
            "etiqueta": "Mis tickets",
            "url_name": "tickets:mis_tickets",
            "icono": "tickets",
            "namespaces": ("tickets",),
            "excluir_vistas": VISTAS_SOLICITUD,
        },
    ]
    if acceso_cola or trabajo_personal:
        elementos.append(
            {
                "etiqueta": "Trabajo",
                "url_name": VISTA_COLA if acceso_cola else VISTA_MI_TRABAJO,
                "icono": "trabajo",
                "destinos": (VISTA_COLA, VISTA_MI_TRABAJO),
                # Detalles de Tarea/Aprobación pertenecen a "Trabajo".
                "namespaces": ("tareas", "aprobaciones"),
            }
        )

    if accede_al_disenador(usuario):
        elementos.append(
            {
                "etiqueta": "Diseñador",
                "url_name": "core:disenador",
                "icono": "studio",
                # Studio ("catalogo:studio*") y el editor técnico de Flujos
                # (namespace `workflows`) son partes del Diseñador. Sin
                # `namespaces` para "catalogo": el catálogo público
                # ("catalogo:lista", "catalogo:detalle") comparte namespace con
                # Studio y no debe resaltar este destino.
                "prefijos": ("core:disenador", "catalogo:studio"),
                "namespaces": ("workflows", "formularios", "flujos"),
                "en_mas": True,
                "grupo": "Gestión",
            }
        )
    if usuario.is_staff:
        elementos.append(
            {
                "etiqueta": "Configuración",
                "url_name": "admin:index",
                "icono": "settings",
                "namespaces": ("admin",),
                "en_mas": True,
                "grupo": "Administración",
            }
        )
    return elementos


def elementos_navegacion(usuario):
    """Destinos visibles para `usuario`, en el orden a mostrar (primero los
    del dock, luego los de "Más" por grupo). Vacío si no está autenticado."""
    if not usuario.is_authenticated:
        return []
    acceso_cola = _acceso_a_cola(usuario)
    return _construir_elementos(usuario, acceso_cola, acceso_cola or _tiene_trabajo_personal(usuario))


def _coincide_exacto(item, vista):
    return vista in item.get("destinos", (item["url_name"],))


def _coincide_amplio(item, resolver_match):
    if resolver_match.view_name in item.get("excluir_vistas", ()):
        return False
    if any(resolver_match.namespace == ns for ns in item.get("namespaces", ())):
        return True
    return any(resolver_match.view_name.startswith(prefijo) for prefijo in item.get("prefijos", ()))


def item_activo(elementos, resolver_match):
    """`url_name` del destino que corresponde a la vista actual, o None.

    La coincidencia exacta de vista tiene prioridad sobre la de namespace o
    prefijo: dos destinos pueden compartir namespace sin que visitar uno
    resalte también al otro."""
    if resolver_match is None:
        return None
    for item in elementos:
        if _coincide_exacto(item, resolver_match.view_name):
            return item["url_name"]
    for item in elementos:
        if _coincide_amplio(item, resolver_match):
            return item["url_name"]
    return None


def grupos_mas(elementos):
    """Destinos de "Más" agrupados, solo grupos con al menos un destino."""
    grupos = []
    for nombre in GRUPOS_MAS:
        items = [e for e in elementos if e.get("en_mas") and e.get("grupo") == nombre]
        if items:
            grupos.append({"etiqueta": nombre, "items": items})
    return grupos


def construir_navegacion(usuario, resolver_match):
    """Todo lo que necesitan el dock, "Más" y las pestañas de Trabajo, con
    cada capacidad evaluada una sola vez por request.

    Las pestañas locales [Cola] [Mi trabajo] solo existen cuando el usuario
    tiene AMBAS vistas (con una sola, la pestaña no aporta nada). Solo se
    consulta el trabajo personal "de más" cuando se está dentro de Trabajo y
    hace falta saber si mostrar las pestañas."""
    if not usuario.is_authenticated:
        return {
            "nav_items": [],
            "nav_item_activo": None,
            "nav_dock": [],
            "nav_mas_grupos": [],
            "nav_mas_activo": False,
            "nav_trabajo_tabs": [],
        }

    vista = resolver_match.view_name if resolver_match else None
    en_trabajo = vista in (VISTA_COLA, VISTA_MI_TRABAJO)

    acceso_cola = _acceso_a_cola(usuario)
    # Con acceso a Cola, "Trabajo" ya es visible: el trabajo personal solo
    # se consulta si hace falta (no hay Cola) o para decidir las pestañas.
    trabajo_personal = _tiene_trabajo_personal(usuario) if (not acceso_cola or en_trabajo) else False

    elementos = _construir_elementos(usuario, acceso_cola, acceso_cola or trabajo_personal)
    activo = item_activo(elementos, resolver_match)
    for item in elementos:
        item["activo"] = item["url_name"] == activo

    grupos = grupos_mas(elementos)
    pestanas = []
    if en_trabajo and acceso_cola and trabajo_personal:
        pestanas = [
            {"etiqueta": "Cola", "url_name": VISTA_COLA, "activa": vista == VISTA_COLA},
            {"etiqueta": "Mi trabajo", "url_name": VISTA_MI_TRABAJO, "activa": vista == VISTA_MI_TRABAJO},
        ]

    return {
        "nav_items": elementos,
        "nav_item_activo": activo,
        "nav_dock": [e for e in elementos if not e.get("en_mas")],
        "nav_mas_grupos": grupos,
        "nav_mas_activo": any(i["activo"] for g in grupos for i in g["items"]),
        "nav_trabajo_tabs": pestanas,
    }
