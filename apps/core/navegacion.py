"""Navegación del sidebar. Application Shell (Incremento 0.5) — sin CU propio.

Declara únicamente destinos reales: no hay entradas para módulos futuros
(Tickets, Mi trabajo, Procesos, Gacetas, Conocimiento, Analítica) mientras
no exista una URL real detrás — evita URLs ficticias y entradas ocultas.
Agregar un módulo en su sprint correspondiente es sumar una entrada aquí,
no rediseñar el sidebar.

"Servicios" (1.1, CU-011) es el primer módulo funcional real agregado desde
0.5 — confirma que la extensibilidad ya estaba bien pensada. No lleva
`permiso_codigo`: RQF-036 es "Rol: Usuario" (cualquier autenticado puede
navegar el catálogo). El filtrado de *qué servicios concretos* ve cada
usuario no ocurre aquí — ocurre en `apps.catalogo.visibilidad`, que es un
mecanismo de datos (`ServicioVisibilidad`), no de autorización funcional.
No se mezclan: ver `apps/catalogo/admin.py` para esa distinción explícita.

"Tickets" (2.1, CU-015) tampoco lleva `permiso_codigo`: cualquier
autenticado tiene (o puede tener) tickets propios — la relación
solicitante↔Ticket, no un permiso funcional, gobierna qué ve cada usuario
(`apps.tickets.autorizacion`).

Decisión documentada para cuando exista el primer módulo con `Permiso`
propio que SÍ deba gatear el ítem de nav (a diferencia de "Servicios"):
su visibilidad deberá resolverse con
`apps.core.autorizacion.alcances_autorizados(usuario, permiso_codigo)`
(verdadero si el usuario tiene el permiso en algún alcance), no con
`usuario_tiene_permiso()` sin argumentos — esa función, sin contexto,
pregunta específicamente por GLOBAL (ver `autorizacion.py`), lo que
ocultaría el enlace a alguien con el permiso solo en un Área o Unidad. No
se implementa ese caso todavía porque ningún módulo real lo requiere.

"Administración" es la única excepción: su destino hoy es Django Admin,
que ya está gobernado nativamente por `is_staff` (decisión de Sprint 0) —
no se crea un `Permiso` de Órbita para duplicar esa gate.
"""


def elementos_navegacion(usuario):
    """Ítems de navegación visibles para `usuario`, en el orden a mostrar.

    `namespace`, cuando está presente, se usa en `layout/sidebar.html` para
    resaltar el ítem activo también en subpáginas del módulo (ej. el detalle
    de un servicio, no solo el listado) — Inicio no lo lleva a propósito:
    "core" agrupa también Perfil/login/logout, que no deben resaltar Inicio.
    """
    elementos = [
        {"etiqueta": "Inicio", "url_name": "core:inicio", "icono": "home"},
        {
            "etiqueta": "Servicios",
            "url_name": "catalogo:lista",
            "icono": "servicios",
            "namespace": "catalogo",
        },
        {
            "etiqueta": "Tickets",
            "url_name": "tickets:mis_borradores",
            "icono": "tickets",
            "namespace": "tickets",
        },
    ]
    if usuario.is_authenticated and usuario.is_staff:
        elementos.append(
            {
                "etiqueta": "Administración",
                "url_name": "admin:index",
                "icono": "settings",
                "namespace": "admin",
            }
        )
    return elementos
