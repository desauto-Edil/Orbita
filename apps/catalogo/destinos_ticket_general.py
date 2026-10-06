"""Destinos del Ticket General (Sprint 4.C2) — configuración y validación.

El Ticket General ya tiene UN Servicio interno (`apps.catalogo.ticket_general`);
ahora cada ticket puede dirigirse a un DESTINO configurado. Este módulo es la
única autoridad de:

- qué destinos existen, cuáles están activos y cuáles son UTILIZABLES hoy;
- cómo se administran (crear, cambiar responsable, activar, desactivar, definir
  el destino de reserva) con `catalogo.administrar`, auditado en `RegistroAuditoria`.

DESTINO ≠ RESPONSABLE: el destino es a quién va dirigida la solicitud (Área, Equipo
o Persona reales — no hay tablas paralelas de organización); el responsable es
quien debe atenderla (un usuario o un equipo, que es lo que `Ticket` ya modela;
nunca un Área). No hay campo "activo" nuevo para la organización: se usan las reglas
reales del dominio (`Area.activo`, `Equipo.activo`, `Usuario.is_active`).

Quién va al ticket y cuándo (selección, fallback, foto, historial) vive en
`apps.tickets.direccionamiento`; aquí solo la configuración. Orden de bloqueos de
las operaciones administrativas: configuración → destino.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.catalogo.models import ConfiguracionTicketGeneral, DestinoTicketGeneral
from apps.catalogo.operaciones import _exigir_administracion
from apps.core.auditoria import auditar_guardado, registrar_evento
from apps.core.models import Area, Equipo, MiembroEquipo, RegistroAuditoria

Tipo = DestinoTicketGeneral.Tipo

TITULO_DE_GRUPO = {Tipo.AREA: "Áreas", Tipo.EQUIPO: "Equipos", Tipo.USUARIO: "Personas"}
_ORDEN_DE_GRUPO = {Tipo.AREA: 0, Tipo.EQUIPO: 1, Tipo.USUARIO: 2}

_SELECCION = ("area", "equipo", "usuario", "responsable_usuario", "responsable_equipo")


def _nombre_de_usuario(usuario):
    return usuario.get_full_name() or usuario.get_username()


def _consulta():
    return DestinoTicketGeneral.objects.select_related(*_SELECCION)


# --- Validación (reglas reales del dominio) -----------------------------------


def motivo_responsable_invalido(*, responsable_usuario=None, responsable_equipo=None):
    """Por qué ese responsable no puede atender tickets hoy, o `None`. Un usuario
    debe estar activo; un equipo, activo y con al menos un miembro activo."""
    if (responsable_usuario is None) == (responsable_equipo is None):
        return "Indica exactamente un responsable: una persona o un equipo."
    if responsable_usuario is not None:
        if not responsable_usuario.is_active:
            return f"{_nombre_de_usuario(responsable_usuario)} está inactivo y no puede ser responsable."
        return None
    if not responsable_equipo.activo:
        return f"El equipo {responsable_equipo.nombre} está inactivo y no puede ser responsable."
    if not MiembroEquipo.objects.filter(
        equipo=responsable_equipo, activo=True, usuario__is_active=True
    ).exists():
        return f"El equipo {responsable_equipo.nombre} no tiene miembros activos que puedan atender."
    return None


def motivo_no_utilizable(destino):
    """Por qué `destino` no puede recibir tickets nuevos AHORA (mensaje para
    mostrar), o `None` si es utilizable: activo, con su Área/Equipo/Persona
    activos según el modelo real y con un responsable válido. No mira el estado
    `activo` del propio destino: eso lo decide quien llama."""
    objeto = destino.objeto
    if destino.tipo == Tipo.AREA and not objeto.activo:
        return f"El área {objeto.nombre} está inactiva."
    if destino.tipo == Tipo.EQUIPO and not objeto.activo:
        return f"El equipo {objeto.nombre} está inactivo."
    if destino.tipo == Tipo.USUARIO and not objeto.is_active:
        return f"{_nombre_de_usuario(objeto)} está inactivo."
    return motivo_responsable_invalido(
        responsable_usuario=destino.responsable_usuario, responsable_equipo=destino.responsable_equipo
    )


def es_utilizable(destino):
    return destino.activo and motivo_no_utilizable(destino) is None


# --- Consulta ----------------------------------------------------------------------


def destinos_utilizables():
    """Destinos activos y utilizables hoy, agrupables por tipo (Áreas, Equipos,
    Personas) y ordenados por nombre. Solo configuración vigente: nunca
    reconstruye tickets históricos."""
    destinos = [d for d in _consulta().filter(activo=True) if motivo_no_utilizable(d) is None]
    return sorted(destinos, key=lambda d: (_ORDEN_DE_GRUPO[d.tipo], d.etiqueta.lower()))


def destino_predeterminado_utilizable():
    """El destino de reserva si está configurado Y es utilizable; si no, `None`
    (entonces el destino es obligatorio)."""
    configuracion = ConfiguracionTicketGeneral.actual()
    if configuracion.destino_predeterminado_id is None:
        return None
    destino = _consulta().filter(pk=configuracion.destino_predeterminado_id).first()
    return destino if destino is not None and es_utilizable(destino) else None


def hay_destinos_utilizables():
    """¿Se puede radicar hoy un Ticket General? Hace falta al menos un destino
    elegible o un destino de reserva utilizable."""
    return bool(destinos_utilizables()) or destino_predeterminado_utilizable() is not None


def opciones_de_seleccion():
    """Lo que ve quien crea el ticket: una sola lista agrupada (Áreas · Equipos ·
    Personas) con los destinos utilizables, y si puede dejarla sin elegir
    ("No estoy seguro"), lo cual solo se ofrece si hay un destino de reserva."""
    utilizables = destinos_utilizables()
    grupos = []
    for tipo in (Tipo.AREA, Tipo.EQUIPO, Tipo.USUARIO):
        opciones = [{"id": d.pk, "etiqueta": d.etiqueta} for d in utilizables if d.tipo == tipo]
        if opciones:
            grupos.append({"tipo": tipo, "titulo": TITULO_DE_GRUPO[tipo], "opciones": opciones})
    return {
        "grupos": grupos,
        "permite_omitir": destino_predeterminado_utilizable() is not None,
    }


def listar_para_administracion():
    """Todos los destinos (activos e inactivos) con su estado de utilización y el
    marcador del destino de reserva, para la tarjeta de administración."""
    predeterminado_id = ConfiguracionTicketGeneral.actual().destino_predeterminado_id
    filas = []
    for destino in _consulta().all():
        motivo = motivo_no_utilizable(destino)
        filas.append(
            {
                "destino": destino,
                "motivo": motivo,
                "utilizable": destino.activo and motivo is None,
                "predeterminado": destino.pk == predeterminado_id,
            }
        )
    filas.sort(key=lambda f: (_ORDEN_DE_GRUPO[f["destino"].tipo], f["destino"].etiqueta.lower()))
    return filas


def candidatos_para_administracion():
    """Lo elegible al crear un destino o un responsable: solo lo que ya está
    activo en el dominio real (se leen las tablas de organización, no se copian)."""
    Usuario = get_user_model()
    usados = {
        Tipo.AREA: set(DestinoTicketGeneral.objects.filter(tipo=Tipo.AREA).values_list("area_id", flat=True)),
        Tipo.EQUIPO: set(DestinoTicketGeneral.objects.filter(tipo=Tipo.EQUIPO).values_list("equipo_id", flat=True)),
        Tipo.USUARIO: set(DestinoTicketGeneral.objects.filter(tipo=Tipo.USUARIO).values_list("usuario_id", flat=True)),
    }
    return {
        "areas": Area.objects.filter(activo=True).exclude(pk__in=usados[Tipo.AREA]).order_by("nombre"),
        "equipos": Equipo.objects.filter(activo=True).exclude(pk__in=usados[Tipo.EQUIPO]).order_by("nombre"),
        "usuarios": Usuario.objects.filter(is_active=True).exclude(pk__in=usados[Tipo.USUARIO]).order_by("username"),
        "responsables_equipos": Equipo.objects.filter(activo=True).order_by("nombre"),
        "responsables_usuarios": Usuario.objects.filter(is_active=True).order_by("username"),
    }


# --- Administración (catalogo.administrar) -----------------------------------------


def _bloquear(destino=None):
    """Orden fijo: configuración → destino. Devuelve (configuración, destino|None)."""
    configuracion = ConfiguracionTicketGeneral.bloquear()
    if destino is None:
        return configuracion, None
    return configuracion, DestinoTicketGeneral.objects.select_for_update().get(pk=destino.pk)


def _datos(destino):
    return {
        "activo": destino.activo,
        "responsable_usuario_id": destino.responsable_usuario_id,
        "responsable_equipo_id": destino.responsable_equipo_id,
    }


def _auditar(destino, actor, anterior):
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=destino,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=_datos(destino),
    )


def _separar_responsable(responsable):
    if responsable is None:
        return None, None
    if isinstance(responsable, Equipo):
        return None, responsable
    if isinstance(responsable, get_user_model()):
        return responsable, None
    raise ValidationError("El responsable debe ser una persona o un equipo.")


@transaction.atomic
def crear_destino(actor, *, tipo, objeto, responsable=None):
    """Alta de un destino. `objeto` es el Área, Equipo o usuario real al que se
    dirigirá la solicitud; `responsable` (usuario o equipo) es quien la atenderá.
    En un destino EQUIPO o USUARIO sin responsable explícito, lo es el propio
    destino; un destino AREA lo exige (un Área no atiende tickets). Nace activo."""
    _exigir_administracion(actor)
    _bloquear()
    esperado = {Tipo.AREA: Area, Tipo.EQUIPO: Equipo, Tipo.USUARIO: get_user_model()}.get(tipo)
    if esperado is None or not isinstance(objeto, esperado):
        raise ValidationError("Elige un área, un equipo o una persona como destino.")
    responsable_usuario, responsable_equipo = _separar_responsable(responsable)
    if responsable is None:
        if tipo == Tipo.AREA:
            raise ValidationError("Un destino de área necesita un responsable: una persona o un equipo.")
        if tipo == Tipo.EQUIPO:
            responsable_equipo = objeto
        else:
            responsable_usuario = objeto
    if DestinoTicketGeneral.objects.filter(tipo=tipo, **{tipo.lower(): objeto}).exists():
        raise ValidationError("Ya existe un destino para esa opción: edítalo o actívalo.")
    destino = DestinoTicketGeneral(
        tipo=tipo, responsable_usuario=responsable_usuario, responsable_equipo=responsable_equipo,
        **{tipo.lower(): objeto},
    )
    motivo = motivo_no_utilizable(destino)
    if motivo is not None:
        raise ValidationError(motivo)
    destino.save()
    auditar_guardado(destino, actor)
    return destino


@transaction.atomic
def cambiar_responsable(actor, destino, *, responsable):
    """Cambia quién atiende los tickets NUEVOS de este destino. Los tickets ya
    radicados conservan su responsable: nada se reasigna aquí."""
    _exigir_administracion(actor)
    _, destino = _bloquear(destino)
    responsable_usuario, responsable_equipo = _separar_responsable(responsable)
    motivo = motivo_responsable_invalido(
        responsable_usuario=responsable_usuario, responsable_equipo=responsable_equipo
    )
    if motivo is not None:
        raise ValidationError(motivo)
    anterior = _datos(destino)
    destino.responsable_usuario = responsable_usuario
    destino.responsable_equipo = responsable_equipo
    if _datos(destino) == anterior:
        return destino
    destino.save(update_fields=["responsable_usuario", "responsable_equipo", "actualizado_en"])
    _auditar(destino, actor, anterior)
    return destino


@transaction.atomic
def activar_destino(actor, destino):
    """Activa el destino; solo si hoy sería utilizable (Área/Equipo/Persona
    activos y responsable válido)."""
    _exigir_administracion(actor)
    _, destino = _bloquear(destino)
    if destino.activo:
        return destino
    motivo = motivo_no_utilizable(destino)
    if motivo is not None:
        raise ValidationError(motivo)
    anterior = _datos(destino)
    destino.activo = True
    destino.save(update_fields=["activo", "actualizado_en"])
    _auditar(destino, actor, anterior)
    return destino


@transaction.atomic
def desactivar_destino(actor, destino):
    """Desactiva el destino: deja de ofrecerse y de aceptar tickets nuevos; los
    ya radicados no cambian. Si era el destino de reserva, deja de serlo."""
    _exigir_administracion(actor)
    configuracion, destino = _bloquear(destino)
    if not destino.activo:
        return destino
    anterior = _datos(destino)
    destino.activo = False
    destino.save(update_fields=["activo", "actualizado_en"])
    _auditar(destino, actor, anterior)
    if configuracion.destino_predeterminado_id == destino.pk:
        _fijar_predeterminado(configuracion, None, actor)
    return destino


def _fijar_predeterminado(configuracion, destino, actor):
    anterior = {"destino_predeterminado_id": configuracion.destino_predeterminado_id}
    configuracion.destino_predeterminado = destino
    configuracion.save(update_fields=["destino_predeterminado", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=configuracion,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos={"destino_predeterminado_id": destino.pk if destino else None},
    )


@transaction.atomic
def definir_predeterminado(actor, destino):
    """Define el destino de reserva ("No estoy seguro"); `None` lo quita. Solo un
    destino activo y utilizable puede serlo. Hay uno solo por construcción: vive
    en la fila única de configuración."""
    _exigir_administracion(actor)
    configuracion = ConfiguracionTicketGeneral.bloquear()
    if destino is not None:
        destino = DestinoTicketGeneral.objects.select_for_update().get(pk=destino.pk)
        if not destino.activo:
            raise ValidationError("El destino de reserva debe estar activo.")
        motivo = motivo_no_utilizable(destino)
        if motivo is not None:
            raise ValidationError(motivo)
    if configuracion.destino_predeterminado_id == (destino.pk if destino else None):
        return configuracion
    _fijar_predeterminado(configuracion, destino, actor)
    return configuracion
