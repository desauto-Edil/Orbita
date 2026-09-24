"""Operaciones de dominio de versionamiento y activación (CU-012/013,
RQF-046/047, RN-013).

Funciones simples, no una clase Facade: cada una coordina pasos
secuenciales sobre un único agregado (`Formulario` + sus versiones), no
varios subsistemas independientes — no se justificó Facade (propuesta 1.2,
sección G).
"""

from django.db import transaction

from apps.catalogo.models.formularios import Campo, FormularioVersion, OpcionCampo, ReglaCondicional
from apps.core.auditoria import registrar_evento, serializar
from apps.core.models import RegistroAuditoria


def _siguiente_numero(formulario):
    ultimo = FormularioVersion.objects.filter(formulario=formulario).order_by("-numero").first()
    return (ultimo.numero + 1) if ultimo else 1


@transaction.atomic
def crear_nueva_version(formulario, actor, clonar_desde=None):
    """Crea una `FormularioVersion` BORRADOR nueva para `formulario`.

    Si `clonar_desde` no se indica, clona desde `formulario.version_activa`
    (la base natural para seguir iterando). Si no hay versión activa (primer
    formulario), crea una versión 1 vacía.

    `clonar_desde` es también el mecanismo para "recuperar" el contenido de
    una versión HISTORICA (RQF-046/047): se clona a un borrador nuevo en vez
    de reactivarla directamente — ver `activar_version`.
    """
    origen = clonar_desde if clonar_desde is not None else formulario.version_activa

    nueva = FormularioVersion.objects.create(
        formulario=formulario,
        numero=_siguiente_numero(formulario),
        estado=FormularioVersion.Estado.BORRADOR,
    )

    if origen is not None:
        mapa_campos = {}
        for campo in origen.campos.order_by("orden", "id"):
            clon = Campo.objects.create(
                version=nueva,
                tipo=campo.tipo,
                etiqueta=campo.etiqueta,
                ayuda=campo.ayuda,
                obligatorio=campo.obligatorio,
                orden=campo.orden,
                configuracion=campo.configuracion,
            )
            mapa_campos[campo.pk] = clon
            for opcion in campo.opciones.order_by("orden", "id"):
                OpcionCampo.objects.create(
                    campo=clon, valor=opcion.valor, etiqueta=opcion.etiqueta, orden=opcion.orden
                )
        for regla in ReglaCondicional.objects.filter(campo_origen__version=origen):
            ReglaCondicional.objects.create(
                campo_origen=mapa_campos[regla.campo_origen_id],
                operador=regla.operador,
                valor=regla.valor,
                campo_objetivo=mapa_campos[regla.campo_objetivo_id],
                efecto=regla.efecto,
            )

    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=nueva,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=None,
        datos_nuevos=serializar(nueva),
    )
    return nueva


@transaction.atomic
def activar_version(formulario, version, actor):
    """Activa `version` como versión vigente de `formulario` (RQF-047).

    Exige `version.estado == BORRADOR` — una HISTORICA no se reactiva
    directamente (propuesta 1.2, confirmado por el usuario); para recuperar
    contenido histórico, usar `crear_nueva_version(clonar_desde=...)` y
    activar el borrador resultante. La versión previamente activa pasa a
    HISTORICA.
    """
    if version.formulario_id != formulario.pk:
        raise ValueError("La versión no pertenece a este formulario.")
    if version.estado != FormularioVersion.Estado.BORRADOR:
        raise ValueError("Solo se puede activar una versión en estado BORRADOR.")

    datos_anteriores = {"version_activa_id": formulario.version_activa_id}

    anterior = formulario.version_activa
    if anterior is not None:
        anterior.estado = FormularioVersion.Estado.HISTORICA
        anterior.save(update_fields=["estado", "actualizado_en"])

    version.estado = FormularioVersion.Estado.ACTIVA
    version.save(update_fields=["estado", "actualizado_en"])

    formulario.version_activa = version
    formulario.save(update_fields=["version_activa", "actualizado_en"])

    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=formulario,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=datos_anteriores,
        datos_nuevos={"version_activa_id": version.pk},
    )
