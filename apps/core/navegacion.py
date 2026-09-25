"""Navegación del dock. Application Shell (Incremento 0.5, rediseñado en
2.UI.1) — sin CU propio.

Declara únicamente destinos reales: no hay entradas para módulos futuros
(Procesos, Gacetas, Conocimiento, Analítica) mientras no exista una URL
real detrás — evita URLs ficticias y entradas ocultas. Agregar un módulo
en su sprint correspondiente es sumar una entrada aquí, no rediseñar el
shell.

"Servicios" (1.1, CU-011) es el primer módulo funcional real agregado
desde 0.5 — confirma que la extensibilidad ya estaba bien pensada. No
lleva `permiso_codigo`: RQF-036 es "Rol: Usuario" (cualquier autenticado
puede navegar el catálogo). El filtrado de *qué servicios concretos* ve
cada usuario no ocurre aquí — ocurre en `apps.catalogo.visibilidad`, que
es un mecanismo de datos (`ServicioVisibilidad`), no de autorización
funcional. No se mezclan: ver `apps/catalogo/admin.py` para esa
distinción explícita.

"Mis tickets" (2.1, CU-015) tampoco lleva `permiso_codigo`: cualquier
autenticado tiene (o puede tener) tickets propios — la relación
solicitante↔Ticket, no un permiso funcional, gobierna qué ve cada usuario
(`apps.tickets.autorizacion`).

"Cola de atención" (2.3, CU-017) es el primer módulo real que aplica la
decisión documentada arriba: su visibilidad se resuelve con
`apps.core.autorizacion.alcances_autorizados(usuario, "tickets.atender")`
(verdadero si el usuario tiene el permiso en algún alcance — GLOBAL, AREA
o UNIDAD), no con `usuario_tiene_permiso()` sin argumentos, que
ocultaría el enlace a un Gestor con el permiso solo en un Área o Unidad.
Deliberadamente sin `namespace` (mismo criterio que "Inicio"): "Mis
tickets" ya usa `namespace="tickets"` para resaltarse en todas las
subpáginas del módulo (borrador/detalle/etc.) — darle el mismo namespace
a "Cola de atención" resaltaría ambos ítems a la vez en `tickets:cola`.
Se resalta solo por coincidencia exacta de `url_name` (mecanismo que
`layout/dock.html` ya soporta sin cambios).

"Administración" es la única excepción: su destino hoy es Django Admin,
que ya está gobernado nativamente por `is_staff` (decisión de Sprint 0) —
no se crea un `Permiso` de Órbita para duplicar esa gate. Además, a
partir de 2.UI.1 no vive en el dock principal (que se mantiene a máximo
~5 accesos): se marca `"en_mas": True` para que `layout/dock.html` la
renderice dentro del popover "Más" en vez de ocupar un puesto permanente
en el dock flotante.
"""

from apps.core.autorizacion import alcances_autorizados


def elementos_navegacion(usuario):
    """Ítems de navegación visibles para `usuario`, en el orden a mostrar.

    `namespace`, cuando está presente, se usa en `layout/dock.html` para
    resaltar el ítem activo también en subpáginas del módulo (ej. el detalle
    de un servicio, no solo el listado) — Inicio no lo lleva a propósito:
    "core" agrupa también Perfil/login/logout, que no deben resaltar Inicio.

    `en_mas`, cuando está presente y es verdadero, indica que el ítem no se
    renderiza como acceso directo del dock sino dentro del popover "Más"
    (junto a Perfil y Cerrar sesión, que no pasan por esta función porque no
    tienen condición de autorización propia).
    """
    elementos = [
        {"etiqueta": "Inicio", "url_name": "core:inicio", "icono": "home"},
        {
            "etiqueta": "Mis tickets",
            "url_name": "tickets:mis_tickets",
            "icono": "tickets",
            "namespace": "tickets",
        },
        {
            "etiqueta": "Servicios",
            "url_name": "catalogo:lista",
            "icono": "servicios",
            "namespace": "catalogo",
        },
    ]
    if usuario.is_authenticated:
        alcances = alcances_autorizados(usuario, "tickets.atender")
        if alcances["global"] or alcances["areas"] or alcances["unidades_negocio"]:
            elementos.append(
                {
                    "etiqueta": "Cola de atención",
                    "url_name": "tickets:cola",
                    "icono": "cola",
                }
            )
    if usuario.is_authenticated and usuario.is_staff:
        elementos.append(
            {
                "etiqueta": "Administración",
                "url_name": "admin:index",
                "icono": "settings",
                "namespace": "admin",
                "en_mas": True,
            }
        )
    return elementos


def item_activo(elementos, resolver_match):
    """Determina, de una sola vez, cuál `url_name` de `elementos` debe
    resaltarse como activo para la vista actual — para que `layout/dock.html`
    no tenga que repetir la comparación por cada `<a>`.

    Coincidencia exacta de `url_name` tiene prioridad sobre coincidencia por
    `namespace`: dos ítems pueden compartir namespace (ej. "Mis tickets" y
    "Cola de atención", ambos bajo `tickets`) sin que visitar uno resalte
    también al otro — solo cae a la coincidencia por namespace cuando
    ningún ítem coincide de forma exacta con la vista actual.
    """
    if resolver_match is None:
        return None
    for item in elementos:
        if item["url_name"] == resolver_match.view_name:
            return item["url_name"]
    for item in elementos:
        if item.get("namespace") and item["namespace"] == resolver_match.namespace:
            return item["url_name"]
    return None
