"""Autorización de Workflows (CU-020, RQF-063/065/069/070).

Tres permisos: `workflows.administrar` (3.1, X.5 — crear/editar Workflow,
crear versión, activar: gobierna la DEFINICIÓN), `workflows.consultar`
(3.1, X.5 — solo lectura) y `workflows.ejecutar` (3.2, W.8 — inicio/avance/
reanudación de una `InstanciaWorkflow` cuando la operación la origina un
usuario). `administrar` NO implica `ejecutar` ni viceversa (W.8, corrección
aprobada: son capacidades distintas — diseñar un workflow no autoriza a
dispararlo, y poder dispararlo no autoriza a rediseñarlo) — a diferencia de
`administrar`→`consultar`, que sí es una relación natural ya aprobada en
3.1.

Los tres se resuelven en alcance GLOBAL vía
`apps.core.autorizacion.usuario_tiene_permiso` — mismo mecanismo y misma
limitación ya aceptada para Catálogo/Formulario (interfaz administrativa
provisional). `apps/workflows/motor.py` NO llama a estas funciones
internamente (mismo criterio que `apps/workflows/versionamiento.py` en
3.1): la autorización es responsabilidad de quien invoca al motor con un
`actor` real (`origen=USUARIO`), nunca del motor mismo — cuando el origen
es SISTEMA no hay `usuario` que autorizar (ver
`apps.core.auditoria.registrar_evento`, misma distinción actor/origen)."""

from apps.core.autorizacion import usuario_tiene_permiso

PERMISO_ADMINISTRAR = "workflows.administrar"
PERMISO_CONSULTAR = "workflows.consultar"
PERMISO_EJECUTAR = "workflows.ejecutar"


def puede_administrar_workflows(usuario):
    return usuario_tiene_permiso(usuario, PERMISO_ADMINISTRAR)


def puede_consultar_workflows(usuario):
    # Quien puede administrar puede, por supuesto, también consultar — no
    # es una tercera decisión, es la relación natural entre los dos
    # permisos ya aprobados en X.5.
    return usuario_tiene_permiso(usuario, PERMISO_CONSULTAR) or puede_administrar_workflows(usuario)


def puede_ejecutar_workflows(usuario):
    # A propósito NO delega en `puede_administrar_workflows` (W.8,
    # corrección aprobada): administrar y ejecutar son capacidades
    # independientes, sin relación implícita en ningún sentido.
    return usuario_tiene_permiso(usuario, PERMISO_EJECUTAR)
