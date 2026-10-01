"""Autorización de Aprobaciones — CU-024/025, RN-025.

Mismo criterio ROL+ALCANCE+RELACIÓN CON EL OBJETO que el resto del
proyecto, mismo patrón exacto que `apps/tareas/autorizacion.py` (su
precedente más directo): consultar ≠ gestionar (función supervisora,
reasignar) ≠ decidir (relación directa con LA `Aprobacion` concreta, nunca
solo por alcance — RN-025 literal: "autorización efectiva sobre dicha
aprobación").

Solo 2 permisos, mismo criterio ya aprobado para Tareas:

- `aprobaciones.consultar` — visibilidad amplia por alcance.
- `aprobaciones.gestionar` — función supervisora: reasignar a un tercero.

Decidir (aprobar/rechazar/devolver) NO exige un permiso nuevo — exige ser
el aprobador designado de ESA fila concreta (`es_aprobador_directo`), o
integrante activo del equipo designado cuando `tipo_aprobador=EQUIPO`. Un
permiso GLOBAL de `aprobaciones.gestionar` nunca sustituye esto (mismo
criterio explícito ya aplicado a `puede_completar_tarea`: la función
supervisora no permite decidir trabajo ajeno).

**Alcance GLOBAL únicamente** — mismo hallazgo documental que Tareas:
ningún RQF/RN de Aprobaciones documenta un Área/Unidad propia o heredada.

Conflicto de interés: NO implementado (diagnóstico de 3.4 — ningún CU/RQF
lo exige). No hay ninguna función `impedir_autoaprobacion` aquí; si se
decide en el futuro, es el lugar natural donde engancharía (compararía la
`Aprobacion` contra `instancia.iniciado_por` del lado de
`apps.workflows`, nunca aquí — este módulo no conoce Workflow)."""

from apps.aprobaciones.models import Aprobacion
from apps.core.autorizacion import usuario_tiene_permiso
from apps.core.models import MiembroEquipo

PERMISO_CONSULTAR = "aprobaciones.consultar"
PERMISO_GESTIONAR = "aprobaciones.gestionar"


def _autenticado(usuario):
    return bool(getattr(usuario, "is_authenticated", False))


def es_aprobador_directo(usuario, aprobacion):
    if not _autenticado(usuario):
        return False
    if aprobacion.aprobador_usuario_id == usuario.id:
        return True
    if aprobacion.aprobador_equipo_id is None:
        return False
    return MiembroEquipo.objects.filter(
        equipo_id=aprobacion.aprobador_equipo_id, usuario=usuario, activo=True
    ).exists()


def _le_toca_en_secuencia(aprobacion):
    """SECUENCIAL: solo la PENDIENTE de menor `orden` del esquema es
    accionable (decisión aprobada — sin campo `activa` persistido, se
    deriva del estado agregado). PARALELA: sin restricción de orden,
    cualquier PENDIENTE es accionable."""
    esquema = aprobacion.esquema
    if esquema.modo != esquema.Modo.SECUENCIAL:
        return True
    menor_pendiente = esquema.participaciones.filter(estado=Aprobacion.Estado.PENDIENTE).order_by("orden").first()
    return menor_pendiente is not None and menor_pendiente.pk == aprobacion.pk


def puede_aprobar(usuario, aprobacion):
    """RN-025 — autorización efectiva sobre ESTA aprobación concreta."""
    if aprobacion.estado != Aprobacion.Estado.PENDIENTE:
        return False
    if not es_aprobador_directo(usuario, aprobacion):
        return False
    return _le_toca_en_secuencia(aprobacion)


def puede_reasignar_aprobacion(usuario, aprobacion):
    return usuario_tiene_permiso(usuario, PERMISO_GESTIONAR)


def puede_consultar_aprobacion(usuario, aprobacion):
    return (
        es_aprobador_directo(usuario, aprobacion)
        or usuario_tiene_permiso(usuario, PERMISO_CONSULTAR)
        or usuario_tiene_permiso(usuario, PERMISO_GESTIONAR)
    )
