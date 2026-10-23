"""Estado de configuración de Básico (Studio) — ÚNICA fuente de verdad.

Deriva el estado de cada apartado de Básico (Identidad · Inicio · Responsables · Visibilidad · Tiempos y
prórrogas) a partir de los DATOS PERSISTIDOS y de los validadores que ya existen en el dominio
(`programacion.errores_de_compatibilidad`, `programacion.errores_de_responsable_inicial`,
`operaciones.errores_de_prorroga`). No crea reglas de negocio: solo las lee.

Estados (los mismos códigos que las secciones del Studio):
- COMPLETO: lo obligatorio está guardado y es válido.
- EN_PROGRESO: hay configuración, pero falta algo obligatorio.
- ATENCION: lo guardado es inválido o hubo un error.
- SIN_CONFIGURAR: no hay configuración relevante.
- OPCIONAL: el apartado no es obligatorio y no se usó.

Un apartado NO cambia de estado por visitarlo, por estar habilitado en el asistente ni por tener cambios sin
guardar en el navegador: solo por lo que hay en la base de datos. El estado de Básico se deriva de sus apartados
OBLIGATORIOS (más cualquier apartado que requiera atención). Solo lectura.
"""

from dataclasses import dataclass, field

from apps.catalogo.models import ProgramacionProceso, Servicio

SIN_CONFIGURAR, EN_PROGRESO, COMPLETO, OPCIONAL, ATENCION = "empty", "progress", "complete", "optional", "attention"

ETIQUETAS = {
    SIN_CONFIGURAR: "Sin configurar", EN_PROGRESO: "En progreso", COMPLETO: "Completo", OPCIONAL: "Opcional",
    ATENCION: "Requiere atención",
}


@dataclass
class Apartado:
    clave: str
    etiqueta: str
    estado: str
    texto: str
    obligatorio: bool
    aplica: bool = True
    mensajes: list = field(default_factory=list)

    @property
    def estado_etiqueta(self):
        return ETIQUETAS[self.estado]


def _identidad(servicio):
    datos = [bool((servicio.nombre or "").strip()), bool(servicio.categoria_id), servicio.tipo in Servicio.Tipo.values]
    if all(datos):
        return COMPLETO, f"{servicio.get_tipo_display()} · {servicio.categoria.nombre}", []
    if not any(datos):
        return SIN_CONFIGURAR, "Falta el nombre y la categoría", []
    faltan = [n for n, ok in zip(("el nombre", "la categoría", "el tipo"), datos) if not ok]
    return EN_PROGRESO, "Falta " + " y ".join(faltan), []


def _inicio(servicio, programacion):
    from apps.catalogo.programacion import errores_de_compatibilidad, errores_de_responsable_inicial

    if programacion is None or not programacion.activa:
        # Manual no necesita nada más: es el modo por omisión de un Proceso.
        return COMPLETO, "Manual: una persona lo inicia cuando lo necesita", []
    compatibilidad = errores_de_compatibilidad(servicio)
    if compatibilidad:
        return ATENCION, "La programación no es compatible con la configuración actual", compatibilidad
    if programacion.ultimo_error:
        return ATENCION, "La última ejecución programada no se pudo generar", [programacion.ultimo_error]
    if programacion.responsable_inicial_id is None or not programacion.dia_creacion or not programacion.periodo:
        return EN_PROGRESO, "Programado: falta completar la programación", []
    responsable = errores_de_responsable_inicial(programacion.responsable_inicial, servicio)
    if responsable:
        return ATENCION, responsable[0], responsable
    # Bloque 4.1: un equipo responsable inicial necesita a alguien que pueda tomar las ejecuciones.
    from apps.catalogo.operaciones import errores_de_atencion_programada

    sin_atencion = errores_de_atencion_programada(programacion)
    if sin_atencion:
        return ATENCION, "Nadie del equipo responsable inicial puede tomar las ejecuciones", sin_atencion
    return COMPLETO, (
        f"Programado: {programacion.get_frecuencia_display().lower()}, día {programacion.dia_creacion}, "
        f"{programacion.get_periodo_display().lower()}"
    ), []


def _responsables(servicio, exige_ruta, hay_ruta):
    """Responsables: opcionales mientras exista una ruta para atender el ticket (`operaciones.hay_ruta_de_atencion`:
    alcance global o un responsable con permiso de atender). Sin esa ruta, publicar se bloquea."""
    cantidad = servicio.responsables.filter(activo=True).count()
    if cantidad and exige_ruta and not hay_ruta:
        mensaje = "Ningún responsable tiene permiso para atender tickets"
        return ATENCION, mensaje, [mensaje]
    if cantidad:
        return COMPLETO, f"{cantidad} responsable{'s' if cantidad != 1 else ''}", []
    if exige_ruta and not hay_ruta:
        return EN_PROGRESO, "Falta quién lo atienda: agrega un responsable (persona o equipo)", []
    if exige_ruta:
        return OPCIONAL, "Sin responsables: lo atiende quien tiene alcance global", []
    return OPCIONAL, "Sin responsables generales", []


def _visibilidad(servicio):
    if servicio.alcance_visibilidad != Servicio.AlcanceVisibilidad.RESTRINGIDO:
        return COMPLETO, "Público interno", []
    concesiones = servicio.visibilidad.filter(activo=True).count()
    if concesiones:
        return COMPLETO, f"Restringido · {concesiones} con acceso", []
    # Falta un requisito (conceder acceso o hacerlo público), no es una configuración inválida: RN-038 parte de
    # RESTRINGIDO sin concesiones en todo borrador nuevo.
    return EN_PROGRESO, "Restringido y sin nadie con acceso: concede acceso o hazlo público", []


def _tiempos(servicio):
    from apps.catalogo.operaciones import errores_de_prorroga

    errores = errores_de_prorroga(servicio)
    if errores:
        return ATENCION, errores[0], errores
    partes = []
    if servicio.tiempo_objetivo_cantidad:
        partes.append(f"{servicio.tiempo_objetivo_cantidad} {servicio.get_tiempo_objetivo_unidad_display().lower()}")
    if servicio.politica_prorroga and servicio.politica_prorroga != Servicio.PoliticaProrroga.NO_PERMITE:
        partes.append(servicio.get_politica_prorroga_display())
    if partes:
        return COMPLETO, " · ".join(partes), []
    return OPCIONAL, "Sin compromiso de tiempo", []


def apartados(servicio):
    """Los cinco apartados de Básico, en el orden del asistente, con `aplica` según el registro."""
    proceso = servicio.tipo == Servicio.Tipo.PROCESO and not servicio.es_ticket_general
    programacion = (
        ProgramacionProceso.objects.filter(servicio=servicio).select_related(
            "responsable_inicial__usuario", "responsable_inicial__equipo"
        ).first() if proceso else None
    )
    programado = programacion is not None and programacion.activa
    # Ruta de atención (Bloque 2): la exige la publicación salvo en el Ticket General y en un Proceso programado.
    from apps.catalogo.operaciones import hay_ruta_de_atencion

    exige_ruta = not servicio.es_ticket_general and not programado
    hay_ruta = hay_ruta_de_atencion(servicio) if exige_ruta else True

    def crear(clave, etiqueta, calculo, *, obligatorio, aplica=True):
        estado, texto, mensajes = calculo() if aplica else (OPCIONAL, "", [])
        return Apartado(clave, etiqueta, estado, texto, obligatorio, aplica, list(mensajes))

    return [
        crear("identidad", "Identidad", lambda: _identidad(servicio), obligatorio=True),
        crear("inicio", "Inicio", lambda: _inicio(servicio, programacion), obligatorio=True, aplica=proceso),
        crear(
            "responsables", "Responsables", lambda: _responsables(servicio, exige_ruta, hay_ruta),
            obligatorio=exige_ruta and not hay_ruta,
        ),
        # Un Proceso programado no se solicita: su visibilidad no aplica (se oculta, 4.G1).
        crear("visibilidad", "Visibilidad", lambda: _visibilidad(servicio), obligatorio=True, aplica=not programado),
        crear("tiempos", "Tiempos y prórrogas", lambda: _tiempos(servicio), obligatorio=False),
    ]


def agregado(lista):
    """Estado de Básico: atención si algún apartado la requiere; completo si todos los OBLIGATORIOS lo están
    (aunque haya opcionales sin usar); si no, en progreso (o sin configurar si nada obligatorio tiene datos).
    Devuelve `(estado, texto)`."""
    aplicables = [a for a in lista if a.aplica]
    atencion = [a.etiqueta for a in aplicables if a.estado == ATENCION]
    if atencion:
        return ATENCION, "Revisa " + ", ".join(atencion)
    obligatorios = [a for a in aplicables if a.obligatorio]
    if all(a.estado == COMPLETO for a in obligatorios):
        # Explícito: los opcionales sin configurar no impiden «Completa», pero se nombran para que no parezca
        # que falta algo obligatorio.
        sin_usar = [a.etiqueta for a in aplicables if a.estado == OPCIONAL]
        return COMPLETO, "Completa" + (" · opcional sin configurar: " + ", ".join(sin_usar) if sin_usar else "")
    if all(a.estado == SIN_CONFIGURAR for a in obligatorios):
        return SIN_CONFIGURAR, "Sin configurar"
    faltan = [a.etiqueta for a in obligatorios if a.estado != COMPLETO]
    return EN_PROGRESO, "Falta " + ", ".join(faltan)
