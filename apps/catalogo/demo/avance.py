"""Ayudas de LECTURA y de acción para que las demos avancen un ticket como lo haría cada persona en la pantalla.

No hay atajos ni estados escritos a mano: cada acción es la operación de dominio que usa la interfaz, ejecutada por
la persona a la que el dominio se la asignó (y que la operación vuelve a autorizar):

- decidir una aprobación (de un paso del flujo o de una ronda de gaceta) → `aprobaciones.decisiones.aplicar`, la vía
  de `aprobaciones:decidir` y de `tickets:decidir_pieza`;
- completar una actividad (con lo que se escribe o adjunta en el ítem) → `tickets.avance.guardar_y_avanzar`, la vía
  de `tickets:tarea_avanzar`; una tarea de corrección de gaceta se reenvía por esa misma vía;
- marcar la lista de verificación → `tareas.operaciones.marcar_verificacion`.

Quién actúa sale SIEMPRE de lo que el dominio dejó asignado (la persona responsable de la tarea, el participante
de la aprobación): nunca se elige aquí a otra persona.
"""

import struct
import zlib

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile


def ejecucion_en_espera(instancia):
    """La ejecución vigente de `instancia` si está esperando algo (tarea, aprobación, gaceta…), o `None`."""
    from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow

    instancia.refresh_from_db()
    if instancia.estado != InstanciaWorkflow.Estado.EN_ESPERA:
        return None
    ultima = instancia.ejecuciones_etapa.select_related("bloque_operativo").order_by("-orden").first()
    if ultima is None or ultima.estado != InstanciaEtapa.Estado.EN_ESPERA:
        return None
    return ultima


def nombre_del_paso(ejecucion):
    return ejecucion.bloque_operativo.nombre if ejecucion is not None and ejecucion.bloque_operativo_id else ""


def tarea_de(ejecucion):
    from apps.workflows.models import TareaWorkflow

    vinculo = TareaWorkflow.objects.filter(instancia_etapa=ejecucion).select_related("tarea").first()
    return vinculo.tarea if vinculo is not None else None


def aprobaciones_pendientes(esquema):
    from apps.aprobaciones.models import Aprobacion

    return list(esquema.participaciones.filter(estado=Aprobacion.Estado.PENDIENTE).order_by("orden", "pk"))


def esquema_del_paso(ejecucion):
    from apps.workflows.models import EsquemaAprobacionWorkflow

    vinculo = EsquemaAprobacionWorkflow.objects.filter(instancia_etapa=ejecucion).select_related("esquema").first()
    return vinculo.esquema if vinculo is not None else None


def decidir(esquema, decision, observacion, *, publicacion=None):
    """Cada participante PENDIENTE del esquema decide, en su orden, con la vía de la pantalla. Devuelve quiénes
    decidieron. Un participante que es un equipo no se resuelve aquí (no se elige a nadie por él)."""
    from apps.aprobaciones.decisiones import aplicar

    decidieron = []
    for _ in range(10):
        esquema.refresh_from_db()
        if esquema.resultado:
            break
        pendientes = aprobaciones_pendientes(esquema)
        persona = next((a for a in pendientes if a.aprobador_usuario_id), None)
        if persona is None:
            break
        aplicar(persona, persona.aprobador_usuario, decision=decision, observacion=observacion, publicacion=publicacion)
        decidieron.append(persona.aprobador_usuario)
    return decidieron


def responsable_de(tarea):
    """La persona que el dominio dejó a cargo de la tarea, o `ValidationError` si es solo un equipo."""
    if tarea.usuario_responsable_id is None:
        raise ValidationError(f"La tarea «{tarea.titulo}» es de un equipo: alguien del equipo debe tomarla.")
    return tarea.usuario_responsable


def marcar_verificacion(tarea, actor):
    from apps.tareas.operaciones import marcar_verificacion as marcar

    for item in tarea.verificaciones.filter(marcado=False).order_by("orden", "pk"):
        # Un punto con pregunta exige su conclusión: la demo deja una de ejemplo.
        conclusion = item.conclusion or (f"Revisado en la demo: {item.texto.lower()}." if item.pregunta else None)
        marcar(item, actor, marcado=True, conclusion=conclusion)


def completar(tarea, actor, *, elemento=None, valores=None, archivos=(), verificar=True):
    """Completa `tarea` por la vía de «Guardar y avanzar» (con lo que se escribe o adjunta en el ítem)."""
    from apps.tickets.avance import guardar_y_avanzar

    if verificar:
        marcar_verificacion(tarea, actor)
    return guardar_y_avanzar(tarea, actor, elemento=elemento, valores=valores, archivos=archivos)


def imagen_png(nombre, color, *, ancho=96, alto=96):
    """Un PNG real y pequeño de un solo color (archivo de PRUEBA de la demo, con vista previa en el visor)."""
    fila = b"\x00" + bytes(color) * ancho
    crudo = fila * alto

    def bloque(tipo, datos):
        return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)

    contenido = (
        b"\x89PNG\r\n\x1a\n"
        + bloque(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 2, 0, 0, 0))
        + bloque(b"IDAT", zlib.compress(crudo, 9))
        + bloque(b"IEND", b"")
    )
    return SimpleUploadedFile(nombre, contenido, content_type="image/png")
