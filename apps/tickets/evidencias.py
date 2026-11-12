"""Listas de evidencias (`Campo` CHECKLIST) — marcar cada punto con su prueba.

Un campo CHECKLIST tiene PUNTOS (sus `OpcionCampo`); cada punto se marca con UNA prueba, un archivo o un enlace
(`tickets.MarcaEvidencia`). Vive en dos lugares:

- en el FORMULARIO DE INICIO del ticket (`elemento` vacío): lo diligencia quien diligencia el formulario
  (solicitante en el borrador, o quien completa los datos de inicio en la ejecución) por el camino de siempre
  (`operaciones._guardar_respuestas` → `aplicar_en_respuesta`);
- en una COLUMNA de un ítem: lo marca quien puede editar esa columna del ítem, con los mismos locks y
  protecciones que adjuntar un archivo (`parrilla._bloquear_elemento`: quien opera la colección o el responsable
  de una tarea abierta del ítem —incluido cualquier miembro del equipo de una actividad de equipo— limitado a sus
  columnas).

Completo = todos los puntos con una marca vigente (`esta_completo`): es lo que exigen una columna obligatoria, las
columnas requeridas de una actividad y la radicación. LA EVIDENCIA ES EL CHECKLIST: si una actividad que exige
evidencia es de un ítem cuya colección tiene una columna CHECKLIST, la actividad cumple con los checklists del ítem
completos (`evidencia_de_tarea`, que usa `tareas.operaciones.tiene_evidencia`) y no con un adjunto suelto.

Nunca se borra nada: desmarcar (o reemplazar la prueba) retira la marca anterior (`retirada_en`). Todo se audita.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import Campo
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tickets.models import MarcaEvidencia

CHECKLIST = Campo.TipoCampo.CHECKLIST


# --- Lectura ------------------------------------------------------------------------------------------------------


def puntos_de(campo):
    """`[(valor, etiqueta)]` de los puntos del checklist, en su orden."""
    return list(campo.opciones.order_by("orden", "id").values_list("valor", "etiqueta"))


def marcas_vigentes(*, ticket=None, elemento=None, campo=None):
    consulta = MarcaEvidencia.objects.filter(retirada_en__isnull=True).select_related("marcada_por")
    if elemento is not None:
        consulta = consulta.filter(elemento=elemento)
    elif ticket is not None:
        consulta = consulta.filter(ticket=ticket, elemento__isnull=True)
    if campo is not None:
        consulta = consulta.filter(campo=campo)
    return list(consulta)


def esta_completo(campo, marcas):
    """¿Todos los puntos de `campo` tienen una marca vigente entre `marcas`?"""
    marcados = {m.punto for m in marcas if m.campo_id == campo.pk and m.retirada_en is None}
    try:
        ESTRATEGIAS_POR_TIPO[CHECKLIST].validar_valor(campo, marcados)
    except ValidationError:
        return False
    return True


def checklist(campo, marcas):
    """Presentación de un checklist: `{"campo", "puntos": [{"valor", "etiqueta", "marca"}], "hechos", "total",
    "completo"}`. `marcas` son las vigentes del ticket o del ítem (se filtran por campo)."""
    por_punto = {m.punto: m for m in marcas if m.campo_id == campo.pk and m.retirada_en is None}
    puntos = [{"valor": v, "etiqueta": e, "marca": por_punto.get(v)} for v, e in puntos_de(campo)]
    hechos = sum(1 for p in puntos if p["marca"] is not None)
    return {"campo": campo, "puntos": puntos, "hechos": hechos, "total": len(puntos),
            "completo": bool(puntos) and hechos == len(puntos)}


def checklists_de_elemento(elemento, columnas=None):
    """Checklists de las columnas CHECKLIST de un ítem (vacío si su colección no tiene ninguna)."""
    from apps.tickets.parrilla import columnas_de

    columnas = [c for c in (columnas if columnas is not None else columnas_de(elemento.parrilla)) if c.tipo == CHECKLIST]
    if not columnas:
        return []
    marcas = marcas_vigentes(elemento=elemento)
    return [checklist(c, marcas) for c in columnas]


def evidencia_de_tarea(tarea):
    """LA EVIDENCIA ES EL CHECKLIST. Si `tarea` trabaja sobre un ítem cuya colección tiene columnas CHECKLIST,
    devuelve `(True, completo)`: la evidencia de la tarea son esos checklists (todos completos). Si no, `(False,
    None)` y la evidencia sigue siendo un adjunto de la tarea, como siempre."""
    from apps.tickets.models import ElementoParrilla
    from apps.tickets.vinculos import origen_de_tareas

    origen = origen_de_tareas([tarea.pk]).get(tarea.pk) or {}
    elemento = (
        ElementoParrilla.objects.select_related("parrilla").filter(pk=origen["elemento_id"]).first()
        if origen.get("elemento_id") else None
    )
    if elemento is None:
        return False, None
    listas = checklists_de_elemento(elemento)
    if not listas:
        return False, None
    return True, all(lista["completo"] for lista in listas)


# --- Escritura en un ítem -------------------------------------------------------------------------------------------


def _columna_checklist(parrilla, campo):
    from apps.tickets.parrilla import columnas_de

    campo = next((c for c in columnas_de(parrilla) if c.pk == getattr(campo, "pk", campo)), None)
    if campo is None:
        raise ValidationError("El campo no pertenece a la estructura de este ticket.")
    if campo.tipo != CHECKLIST:
        raise ValidationError(f"«{campo.etiqueta}» no es una lista de evidencias.")
    return campo


def _punto_valido(campo, punto):
    etiquetas = dict(puntos_de(campo))
    if punto not in etiquetas:
        raise ValidationError(f"«{campo.etiqueta}»: ese punto no existe en la lista de evidencias.")
    return etiquetas[punto]


def _auditar(marca, actor, accion, anterior, nuevo):
    registrar_evento(
        accion=accion, instancia=marca,
        origen=RegistroAuditoria.Origen.USUARIO if actor is not None else RegistroAuditoria.Origen.SISTEMA,
        usuario=actor, datos_anteriores=anterior, datos_nuevos=nuevo,
    )


def _crear_marca(*, ticket, elemento, campo, punto, actor, archivo=None, enlace=""):
    """Valida la prueba, retira la marca vigente del punto (si la hay: reemplazar = retirar + marcar) y crea la
    nueva. Quien llama ya bloqueó y autorizó."""
    from apps.tickets.operaciones import _validar_archivo_tecnico

    etiqueta = _punto_valido(campo, punto)
    enlace = (enlace or "").strip()
    if archivo:
        _validar_archivo_tecnico(archivo)
    try:
        ESTRATEGIAS_POR_TIPO[CHECKLIST].validar_prueba(campo, archivo=archivo, enlace=enlace)
    except ValidationError as exc:
        raise ValidationError(f"«{etiqueta}»: {' '.join(exc.messages)}")
    filtro = {"elemento": elemento} if elemento is not None else {"ticket": ticket, "elemento__isnull": True}
    ahora = timezone.now()
    for anterior in MarcaEvidencia.objects.select_for_update().filter(
        campo=campo, punto=punto, retirada_en__isnull=True, **filtro
    ):
        anterior.retirada_en, anterior.retirada_por = ahora, actor
        anterior.save(update_fields=["retirada_en", "retirada_por", "actualizado_en"])
    marca = MarcaEvidencia(
        ticket=ticket, elemento=elemento, campo=campo, punto=punto, marcada_por=actor, enlace="" if archivo else enlace,
    )
    if archivo:
        marca.archivo = archivo
        marca.nombre_original = archivo.name[:255]
        marca.tipo_mime = (getattr(archivo, "content_type", "") or "")[:255]
        marca.tamano_bytes = archivo.size
    try:
        marca.save()
        _auditar(marca, actor, RegistroAuditoria.Accion.CREAR, None, {
            "campo": campo.clave, "punto": punto, "elemento_id": getattr(elemento, "pk", None),
            "prueba": marca.nombre_original or marca.enlace,
        })
    except Exception:
        if marca.archivo:
            marca.archivo.delete(save=False)
        raise
    return marca


@transaction.atomic
def marcar_punto_elemento(elemento, campo, punto, actor, *, archivo=None, enlace=""):
    """Marca un punto del checklist `campo` de un ítem vigente con su prueba. Mismos locks, autorización y
    protecciones que adjuntar un archivo a una columna del ítem (`parrilla.adjuntar_archivo_elemento`)."""
    from apps.tickets import parrilla

    ticket, coleccion, elemento, permitidas = parrilla._bloquear_elemento(elemento, actor)
    if elemento.retirado_en is not None:
        raise ValidationError("El elemento está retirado y ya no se puede modificar.")
    parrilla._exigir_en_alcance(elemento)
    campo = _columna_checklist(coleccion, campo)
    parrilla._exigir_columna_permitida(campo, permitidas)
    parrilla._exigir_columna_modificable(ticket, coleccion, elemento, campo)
    marca = _crear_marca(
        ticket=ticket, elemento=elemento, campo=campo, punto=punto, actor=actor, archivo=archivo, enlace=enlace,
    )
    parrilla._ingreso_automatico(elemento, actor)
    return marca


@transaction.atomic
def retirar_marca(marca, actor):
    """Desmarca un punto (retiro LÓGICO de su prueba). En un ítem, con la misma autorización que marcarlo; en el
    formulario de inicio, quien puede diligenciarlo (`_exigir_formulario_editable`)."""
    from apps.tickets import parrilla

    marca = MarcaEvidencia.objects.select_related("elemento", "campo").get(pk=marca.pk)
    if marca.elemento_id is not None:
        ticket, coleccion, elemento, permitidas = parrilla._bloquear_elemento(marca.elemento, actor)
        parrilla._exigir_en_alcance(elemento)
        parrilla._exigir_columna_permitida(marca.campo, permitidas)
        parrilla._exigir_columna_modificable(ticket, coleccion, elemento, marca.campo)
    else:
        _exigir_formulario_editable(marca.ticket, actor)
    marca = MarcaEvidencia.objects.select_for_update().get(pk=marca.pk)
    if marca.retirada_en is not None:
        return marca
    marca.retirada_en, marca.retirada_por = timezone.now(), actor
    marca.save(update_fields=["retirada_en", "retirada_por", "actualizado_en"])
    _auditar(marca, actor, RegistroAuditoria.Accion.ACTUALIZAR, {"retirada_en": None},
             {"retirada_en": marca.retirada_en, "campo": marca.campo.clave, "punto": marca.punto})
    return marca


# --- Formulario de inicio -----------------------------------------------------------------------------------------


def _exigir_formulario_editable(ticket, actor):
    """Quién diligencia el formulario de inicio: el solicitante mientras es BORRADOR, o quien completa los datos de
    inicio durante la ejecución (`autorizacion.puede_diligenciar_datos_inicio`, mientras no se completen)."""
    from apps.tickets.autorizacion import es_propietario_borrador
    from apps.tickets.models import Ticket

    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.estado == Ticket.Estado.BORRADOR:
        if not es_propietario_borrador(actor, ticket):
            raise PermissionDenied("Solo el solicitante puede modificar este borrador.")
        return ticket
    from apps.tickets.autorizacion import puede_diligenciar_datos_inicio
    from apps.tickets.datos_inicio import datos_completos

    if ticket.estado not in (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION) or datos_completos(ticket):
        raise ValidationError("Los datos de inicio de este ticket ya no admiten cambios.")
    if not puede_diligenciar_datos_inicio(actor, ticket):
        raise PermissionDenied("No puedes modificar los datos de inicio de este ticket.")
    return ticket


def aplicar_en_respuesta(ticket, campo, cambios, actor):
    """Aplica al formulario de inicio los cambios de UN checklist enviados con el formulario: `cambios` es
    `{punto: {"archivo": archivo|None, "enlace": str, "quitar": bool}}`. Lo llama `operaciones._guardar_respuestas`
    (que ya autorizó y bloquea en su transacción). Un punto sin archivo ni enlace ni «quitar» no cambia."""
    for punto, cambio in (cambios or {}).items():
        if cambio.get("quitar"):
            for marca in MarcaEvidencia.objects.select_for_update().filter(
                ticket=ticket, elemento__isnull=True, campo=campo, punto=punto, retirada_en__isnull=True,
            ):
                marca.retirada_en, marca.retirada_por = timezone.now(), actor
                marca.save(update_fields=["retirada_en", "retirada_por", "actualizado_en"])
                _auditar(marca, actor, RegistroAuditoria.Accion.ACTUALIZAR, {"retirada_en": None},
                         {"retirada_en": marca.retirada_en, "campo": campo.clave, "punto": punto})
            continue
        archivo, enlace = cambio.get("archivo"), (cambio.get("enlace") or "").strip()
        if not archivo and not enlace:
            continue
        _crear_marca(ticket=ticket, elemento=None, campo=campo, punto=punto, actor=actor, archivo=archivo, enlace=enlace)


def respuesta_completa(ticket, campo):
    return esta_completo(campo, marcas_vigentes(ticket=ticket, campo=campo))


def cambios_desde_post(post, files, campo, prefijo):
    """Lee del POST/FILES los cambios de un checklist: por punto `<prefijo>__<punto>__archivo` (archivo),
    `<prefijo>__<punto>__enlace` (texto) y `<prefijo>__<punto>__quitar`. Solo devuelve los puntos tocados."""
    cambios = {}
    for valor, _etiqueta in puntos_de(campo):
        base = f"{prefijo}__{valor}"
        archivo = files.get(f"{base}__archivo")
        enlace = (post.get(f"{base}__enlace") or "").strip()
        quitar = bool(post.get(f"{base}__quitar"))
        if archivo or enlace or quitar:
            cambios[valor] = {"archivo": archivo, "enlace": enlace, "quitar": quitar}
    return cambios


# --- Autorización de lectura -------------------------------------------------------------------------------------


def puede_ver_marca(usuario, marca):
    """Ver/descargar la prueba: quien consulta el ticket o puede ver la ficha del ítem (la misma autorización de la
    ficha y de los archivos del ítem), o —en el formulario de inicio— quien lo diligencia."""
    from apps.tickets.autorizacion import es_propietario_borrador, puede_consultar_ticket

    ticket = marca.ticket
    if puede_consultar_ticket(usuario, ticket) or es_propietario_borrador(usuario, ticket):
        return True
    if marca.elemento_id is not None:
        from apps.tickets.espacio import puede_ver_ficha

        return puede_ver_ficha(usuario, ticket, marca.elemento)
    return False
