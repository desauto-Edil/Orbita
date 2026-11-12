"""Fases de la demostración posteriores a preparar (Bloque 5.6): verificar, publicar, ejemplos y retirar.

Todas actúan SOLO sobre un `RecursoDemo` (lo que la demo creó) y con las operaciones de dominio de siempre.

- `verificar`: no cambia nada. Compara lo que existe con el contrato, simula la fase `publicar` completa
  (programación incluida) en una transacción que se deshace y lee la programación. Quien llama además la envuelve en una
  transacción que siempre se deshace.
- `publicar`: la programación del caso programado se crea JUNTO con la publicación, en la misma transacción
  (nunca queda una programación activa sobre un proceso sin publicar). Después, `publicar_cambios`: la misma
  operación del botón «Publicar cambios» del Studio.
- `ejemplos`: un ticket por caso. El programado SOLO con `generar_ejecucion_programada` y solo si su periodo YA
  venció (nunca el reconciliador global ni fechas alteradas); los demás, radicados por la persona indicada como
  solicitante, con las operaciones del portal. Imperandina: su ticket de PRUEBA se crea de inmediato con sus ideas
  como borradores (`demo.imperandina.ticket_de_prueba`). Desarrollo de software: el ticket del solicitante avanza
  hasta el plan en revisión (`demo.desarrollo.ticket_de_ejemplo`).
- `avanzar` (solo Imperandina): lleva su ticket de prueba a los estados posteriores a la gaceta
  (`demo.imperandina_avanzado.avanzar`).
- `retirar`: cancela los tickets abiertos que el actor puede cancelar, pausa la programación y despublica el
  Servicio. No borra nada.
"""

from datetime import datetime, time, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.catalogo import intenciones, publicacion
from apps.catalogo.demo import contrato
from apps.catalogo.demo.participantes import Participantes
from apps.catalogo.gacetas import errores_de_uso_por_item
from apps.catalogo.models import (
    BloqueOperativo,
    ConfiguracionEjecucionVersion,
    DefinicionGaceta,
    ProgramacionProceso,
    TransicionBloqueOperativo,
)
from apps.catalogo.operaciones import desactivar_servicio
from apps.catalogo.parrilla import definicion_de
from apps.catalogo.programacion import (
    asegurar_responsable_inicial,
    configurar_programacion,
    desactivar_programacion,
)

ABIERTOS = ("RADICADO", "EN_ATENCION")


def _tickets(servicio):
    from apps.tickets.models import Ticket

    return Ticket.objects.filter(detalle_servicio__servicio=servicio)


def _version_vigente(formulario):
    """La versión activa o, sin ella, la última (el borrador de un caso aún no publicado)."""
    if formulario is None:
        return None
    return formulario.version_activa or formulario.versiones.order_by("-numero").first()


def _configuracion(servicio):
    borrador = servicio.configuraciones_ejecucion.filter(
        estado=ConfiguracionEjecucionVersion.Estado.BORRADOR
    ).order_by("-numero").first()
    return borrador or servicio.configuracion_ejecucion_activa


def _comparar_campos(version, specs, etiqueta):
    errores = []
    if version is None:
        return [f"{etiqueta}: no existe."] if specs else []
    campos = {c.clave: c for c in version.campos.all()}
    for spec in specs:
        campo = campos.get(spec.clave)
        if campo is None:
            errores.append(f"{etiqueta}: falta «{spec.etiqueta}» ({spec.clave}).")
        elif campo.tipo != spec.tipo or campo.obligatorio != spec.obligatorio:
            errores.append(f"{etiqueta}: «{spec.etiqueta}» no tiene el tipo u obligatoriedad del contrato.")
    return errores


def discrepancias(recurso):
    """Diferencias entre lo que existe y el contrato del caso (lista vacía = coincide). Solo lectura."""
    caso = contrato.CONTRATOS[recurso.caso]
    servicio = recurso.servicio
    servicio.refresh_from_db()  # se compara lo persistido, nunca una copia en memoria
    p = Participantes.desde_parametros(recurso.parametros)
    errores = []
    if recurso.version_contrato != contrato.VERSION_CONTRATO:
        errores.append(
            f"Se preparó con el contrato v{recurso.version_contrato}; el vigente es v{contrato.VERSION_CONTRATO}."
        )
    if servicio.tipo != caso.tipo:
        errores.append(f"El tipo cambió a {servicio.get_tipo_display()}.")
    errores += _comparar_campos(_version_vigente(servicio.formulario) if caso.campos else None, caso.campos, "Formulario")
    entregables = {d.nombre: d for d in servicio.definiciones_entregables.filter(activo=True)}
    for spec in caso.entregables:
        definicion = entregables.get(spec.nombre)
        if definicion is None or definicion.tipo != spec.tipo or definicion.obligatorio != spec.obligatorio:
            errores.append(f"Salida: el entregable «{spec.nombre}» falta o cambió.")
    if caso.coleccion:
        definicion = definicion_de(servicio)
        if definicion is None or not definicion.activa:
            errores.append("Formulario: falta la colección de ítems.")
        else:
            errores += _comparar_campos(_version_vigente(definicion.formulario), caso.columnas, "Colección")
    if caso.gaceta is not None:
        gaceta = DefinicionGaceta.objects.filter(servicio=servicio, activa=True, nombre=caso.gaceta.nombre).first()
        if gaceta is None:
            errores.append(f"Salida: falta la gaceta «{caso.gaceta.nombre}».")
        else:
            por_item = any(b.opcion == "gaceta_item" for b in caso.bloques)  # la revisa cada recorrido
            if por_item and errores_de_uso_por_item(gaceta.configuracion):
                errores.append("Salida: la gaceta ya no es compatible con la revisión por ítem.")
    for extra in caso.gacetas_extra:
        if not DefinicionGaceta.objects.filter(servicio=servicio, activa=True, nombre=extra.nombre).exists():
            errores.append(f"Salida: falta la gaceta «{extra.nombre}».")
    if caso.fases:
        errores += _discrepancias_del_flujo(servicio, caso, p)
    return errores


def _discrepancias_del_flujo(servicio, caso, p):
    configuracion = _configuracion(servicio)
    if configuracion is None:
        return ["Flujo: no hay configuración."]
    errores = []
    bloques = {b.nombre: b for b in configuracion.bloques.select_related("contenedor", "fase")}
    esperados = [s for s in caso.bloques if not (s.requiere and getattr(p, s.requiere) is None)]
    for spec in esperados:
        bloque = bloques.get(spec.nombre)
        if bloque is None:
            errores.append(f"Flujo: falta «{spec.nombre}».")
            continue
        contenedor = bloque.contenedor.nombre if bloque.contenedor_id else ""
        if (bloque.tipo, bloque.rama or "", contenedor, bloque.fase.nombre) != (
            intenciones.traducir(spec.opcion), spec.rama, spec.contenedor, spec.fase
        ):
            errores.append(f"Flujo: «{spec.nombre}» no está donde dice el contrato.")
    for contenedor in {s.contenedor for s in esperados if s.contenedor}:
        if contenedor not in bloques:
            continue
        reales = list(
            BloqueOperativo.objects.filter(contenedor=bloques[contenedor]).order_by("orden", "pk").values_list("nombre", flat=True)
        )
        if reales != [s.nombre for s in esperados if s.contenedor == contenedor]:
            errores.append(f"Flujo: el orden de «{contenedor}» no es el del contrato.")
        entrada = next((s for s in esperados if s.contenedor == contenedor and s.entra_al_enviar), None)
        if entrada is not None and entrada.nombre in bloques:
            paso = (bloques[contenedor].configuracion or {}).get("paso_al_enviar")
            if paso != bloques[entrada.nombre].clave:
                errores.append(f"Flujo: los ítems de «{contenedor}» no entran por «{entrada.nombre}».")
    for ruta in caso.rutas:
        if ruta.requiere and getattr(p, ruta.requiere) is None:
            continue
        origen = bloques.get(ruta.origen)
        if origen is None:
            continue
        filtro = {"bloque_origen": origen, "finaliza": ruta.finaliza}
        if ruta.destino:
            filtro["bloque_destino__nombre"] = ruta.destino
        if ruta.resultado:
            filtro["resultado_aprobacion"] = ruta.resultado
        if not TransicionBloqueOperativo.objects.filter(**filtro).exists():
            errores.append(f"Flujo: falta la ruta de «{ruta.origen}» a «{ruta.destino or 'FINALIZAR'}».")
    return errores


def _estado(recurso):
    if recurso.retirado_en is not None:
        return f"retirado el {timezone.localtime(recurso.retirado_en):%Y-%m-%d %H:%M}"
    return "publicado" if recurso.servicio.activo else "preparado, sin publicar"


class _Simulacion(Exception):
    pass


def simular_publicacion(recurso, actor):
    """`(publicable, errores)` de la fase `publicar` tal cual (programación incluida), dentro de una
    transacción que siempre se deshace: la misma idea que `publicacion.simular`, que no conoce la programación
    que la demo crea al publicar."""
    try:
        with transaction.atomic():
            publicar(recurso, actor)
            raise _Simulacion
    except _Simulacion:
        publicable, errores = True, []
    except (ValidationError, PermissionDenied) as exc:
        publicable, errores = False, list(getattr(exc, "messages", None) or [str(exc)])
    recurso.refresh_from_db()
    recurso.servicio.refresh_from_db()  # la simulación cambió la copia en memoria
    return publicable, errores


def verificar(recurso, actor, *, hoy=None):
    """Informe del caso, sin cambios (`simular_publicacion` deshace lo que prueba)."""
    from apps.tickets.programadas import proxima_ejecucion

    servicio = recurso.servicio
    lineas = [f"Servicio #{servicio.pk} «{servicio.nombre}»: {_estado(recurso)}."]
    errores = discrepancias(recurso)
    lineas += [f"  ✗ {e}" for e in errores] or ["  ✓ Coincide con el contrato de ORBITA_DEMO.md."]
    pendientes = [f"{p.etiqueta} v{p.borrador.numero}" for p in publicacion.piezas(servicio) if p.pendiente]
    if pendientes:
        lineas.append(f"  Borradores por publicar: {', '.join(pendientes)}.")
    publicado = servicio.activo
    publicable, bloqueos = simular_publicacion(recurso, actor)
    if contrato.CONTRATOS[recurso.caso].programado and not publicado:
        lineas.append("  La programación mensual se crea al publicar (junto con la publicación).")
    lineas += ["  ✓ Publicable." if publicable else "  ✗ No se puede publicar todavía:"]
    lineas += [f"    - {b}" for b in bloqueos]
    programacion = ProgramacionProceso.objects.filter(servicio=servicio).first()
    if programacion is not None:
        estado = "activa" if programacion.activa else "pausada"
        lineas.append(
            f"  Programación {estado}: día {programacion.dia_creacion}, {programacion.get_periodo_display().lower()}, "
            f"desde {programacion.activada_desde}."
        )
        if programacion.activa:
            fecha, periodo, vencida = proxima_ejecucion(programacion, hoy or timezone.localdate())
            lineas.append(
                f"  {'Ejecución vencida y pendiente' if vencida else 'Próxima ejecución'}: {fecha} ({periodo.etiqueta})."
            )
    conteo = {}
    for estado in _tickets(servicio).values_list("estado", flat=True):
        conteo[estado] = conteo.get(estado, 0) + 1
    lineas.append(
        "  Tickets: " + (", ".join(f"{k.lower()} {v}" for k, v in sorted(conteo.items())) if conteo else "ninguno") + "."
    )
    return lineas


@transaction.atomic
def publicar(recurso, actor, *, dia_creacion=None, periodo=None):
    """Publica el caso con `publicacion.publicar_cambios`. El programado crea (o reactiva) su programación en la
    MISMA transacción. Idempotente: un caso publicado y sin borradores no cambia.

    `dia_creacion` / `periodo` (opcionales) cambian el calendario elegido al preparar y quedan guardados. Es la
    regla normal de la programación: cambiar el calendario la reinicia HOY (`configurar_programacion`), así que
    con el día de hoy la ejecución del periodo vence hoy y `ejemplos` puede generarla sin alterar ninguna fecha."""
    servicio = recurso.servicio
    p = Participantes.desde_parametros(recurso.parametros)
    lineas = []
    if contrato.CONTRATOS[recurso.caso].programado:
        if dia_creacion is not None or periodo:
            p.dia_creacion = dia_creacion if dia_creacion is not None else p.dia_creacion
            p.periodo = periodo or p.periodo
            faltan = p.faltantes(recurso.caso)
            if faltan:
                raise ValidationError(f"Calendario no válido: {', '.join(faltan)}.")
            recurso.parametros = {**recurso.parametros, "dia_creacion": p.dia_creacion, "periodo": p.periodo}
            recurso.save(update_fields=["parametros", "actualizado_en"])
        if p.planificador is None or not p.planificador.is_active:
            raise ValidationError("El planificador elegido al preparar ya no existe o no está activo.")
        responsable = asegurar_responsable_inicial(servicio, actor, ("USUARIO", p.planificador))
        programacion = ProgramacionProceso.objects.filter(servicio=servicio).first()
        vigente = (
            programacion is not None and programacion.activa and programacion.dia_creacion == p.dia_creacion
            and programacion.periodo == p.periodo and programacion.responsable_inicial_id == responsable.pk
        )
        if not vigente:
            programacion = configurar_programacion(
                servicio, actor, dia_creacion=p.dia_creacion, periodo=p.periodo, responsable_inicial=responsable,
            )
            lineas.append(
                f"Programación mensual activa: día {programacion.dia_creacion}, "
                f"{programacion.get_periodo_display().lower()}, desde {programacion.activada_desde}."
            )
    era_activo = servicio.activo
    activadas = publicacion.publicar_cambios(servicio, actor)
    servicio.refresh_from_db()
    if activadas:
        lineas.append(f"Activado: {', '.join(activadas)}.")
    lineas.append("Publicado." if not era_activo else ("Cambios publicados." if activadas else "Ya estaba publicado."))
    if recurso.retirado_en is not None:
        recurso.retirado_en = None
        recurso.save(update_fields=["retirado_en", "actualizado_en"])
    return lineas


# --- Ejemplos ---------------------------------------------------------------------


def _respuestas_de_ejemplo(codigo, campos, hoy, area):
    """`{campo_id: valor}` de ejemplo para el formulario de un caso, o `None` si falta un dato real (el área)."""
    if codigo == contrato.WEBINAR:
        if area is None:
            return None
        valores = {
            "objetivo": "Presentar a los equipos las novedades del portafolio.",
            "tipo_de_evento": "conferencia", "area": str(area.pk), "tema": "Novedades del portafolio",
            "fecha_tentativa": (hoy + timedelta(days=30)).isoformat(), "publico_objetivo": ["colaboradores"],
            "expositor": "Equipo de producto", "modalidad": "virtual",
            "contenidos": "Agenda, demostración y preguntas.", "difusion": ["correo", "intranet"],
            REQUIERE_AUDIOVISUAL: True, "requerimientos_audiovisuales": "Grabación y micrófonos inalámbricos.",
        }
    elif codigo == contrato.PIEZA_GRAFICA:
        valores = {
            "tipo_de_pieza": "post", "formato_y_medidas": "1080 x 1080 px",
            "texto_copy": "Conoce las novedades de este mes.", "canal": ["redes"],
            "fecha_necesaria": (hoy + timedelta(days=10)).isoformat(),
        }
    else:
        valores = {
            "cliente_oportunidad": "Cliente de ejemplo", "objetivo": "Presentar la propuesta de servicios.",
            "audiencia": "Gerencia comercial del cliente", "contenidos_requeridos": "Contexto, propuesta y casos.",
            "enlaces_de_referencia": "https://www.example.com/referencia",
            # Con la zona horaria local: un valor sin zona se guardaría como fecha-hora «naive».
            "fecha_de_la_reunion": timezone.make_aware(
                datetime.combine(hoy + timedelta(days=7), time(10, 0))
            ).isoformat(),
        }
    return {campo.pk: valores[campo.clave] for campo in campos if campo.clave in valores}


REQUIERE_AUDIOVISUAL = contrato.REQUIERE_AUDIOVISUAL


def _ejemplo_programado(recurso, hoy):
    from apps.tickets import programadas
    from apps.tickets.models import EjecucionProgramada

    servicio = recurso.servicio
    programacion = ProgramacionProceso.objects.filter(servicio=servicio).first()
    if not servicio.activo or programacion is None or not programacion.activa:
        return ["Primero publica el caso (--fase publicar): crea la programación mensual."]
    hoy = hoy or timezone.localdate()
    periodo = programadas.periodo_pendiente(programacion, hoy)
    if periodo is None:
        ultima = EjecucionProgramada.objects.filter(servicio=servicio).order_by("-periodo_inicio").first()
        fecha, siguiente, _ = programadas.proxima_ejecucion(programacion, hoy)
        lineas = []
        if ultima is not None:
            lineas.append(f"Última ejecución: {ultima.ticket.codigo} «{ultima.ticket.etiqueta}».")
        lineas.append(
            f"No hay ninguna ejecución vencida hoy ({hoy}). La próxima se crea el {fecha} ({siguiente.etiqueta}). "
            "generar_ejecucion_programada no genera fuera del día programado ni antes de la activación "
            f"({programacion.activada_desde}); no se fuerza."
        )
        return lineas
    ejecucion = programadas.generar_ejecucion_programada(programacion, hoy=hoy)
    if ejecucion is None:
        return ["No se generó ninguna ejecución (otra ejecución ya la había creado)."]
    return [f"Ejecución generada: {ejecucion.ticket.codigo} «{ejecucion.ticket.etiqueta}»."]


def _ejemplo_solicitado(recurso, solicitante, area, hoy):
    from apps.tickets.models import Ticket
    from apps.tickets.operaciones import crear_borrador, guardar_respuestas_borrador, radicar_ticket

    servicio = recurso.servicio
    if not servicio.activo:
        return ["Primero publica el caso (--fase publicar)."]
    if solicitante is None:
        return ["Indica --solicitante: la persona que radica el ticket de ejemplo."]
    existente = _tickets(servicio).filter(solicitante=solicitante).exclude(
        estado__in=(Ticket.Estado.BORRADOR, Ticket.Estado.CANCELADO)
    ).first()
    if existente is not None:
        return [f"Ya existe el ticket de ejemplo {existente.codigo} de {solicitante.email}."]
    hoy = hoy or timezone.localdate()
    version = servicio.formulario.version_activa
    respuestas = _respuestas_de_ejemplo(recurso.caso, version.campos.all(), hoy, area)
    if respuestas is None:
        return ["El formulario pide un Área: indica --area-ejemplo (o --area-visibilidad al preparar)."]
    ticket = crear_borrador(solicitante, servicio)
    guardar_respuestas_borrador(ticket, solicitante, respuestas)
    radicar_ticket(ticket, solicitante)
    ticket.refresh_from_db()
    return [f"Ticket radicado por {solicitante.email}: {ticket.codigo}."]


@transaction.atomic
def ejemplos(recurso, actor, *, solicitante=None, area=None, hoy=None):
    if recurso.retirado_en is not None:
        return ["El caso está retirado: publícalo de nuevo para generar ejemplos."]
    if recurso.caso == contrato.IMPERANDINA:
        from apps.catalogo.demo.imperandina import ticket_de_prueba

        return ticket_de_prueba(recurso, hoy)
    if recurso.caso == contrato.SOLAR_ANDINA:
        from apps.catalogo.demo.solar_andina import ticket_de_prueba as ticket_solar

        return ticket_solar(recurso, hoy)
    if recurso.caso == contrato.PARRILLA:
        return _ejemplo_programado(recurso, hoy)
    if area is None:
        area = Participantes.desde_parametros(recurso.parametros).area_visibilidad
    if recurso.caso in (contrato.DESARROLLO, contrato.DISENO):
        from apps.catalogo.demo.desarrollo import ticket_de_ejemplo
        from apps.catalogo.demo.participantes import resolver_usuario

        if solicitante is None:  # el del contrato (scortes)
            referencia = contrato.CONTRATOS[recurso.caso].solicitante
            solicitante = resolver_usuario(referencia, f"Solicitante «{referencia}»")

        return ticket_de_ejemplo(recurso, solicitante=solicitante, area=area, hoy=hoy)
    return _ejemplo_solicitado(recurso, solicitante, area, hoy)


@transaction.atomic
def avanzar(recurso, actor):
    """Ejemplo AVANZADO: Imperandina (`demo.imperandina_avanzado`) y Desarrollo de software (`demo.desarrollo`)."""
    if recurso.retirado_en is not None:
        return ["El caso está retirado: publícalo de nuevo para avanzarlo."]
    if recurso.caso in (contrato.DESARROLLO, contrato.DISENO):
        from apps.catalogo.demo.desarrollo import avanzar as avanzar_desarrollo

        return avanzar_desarrollo(recurso)
    if recurso.caso != contrato.IMPERANDINA:
        return ["La fase avanzar solo aplica a --casos imperandina, desarrollo y diseno."]
    from apps.catalogo.demo.imperandina_avanzado import avanzar as avanzar_imperandina

    return avanzar_imperandina(recurso)


@transaction.atomic
def cierre(recurso, actor):
    """Demo de CIERRE: el ticket de ejemplo de Desarrollo de software o de Diseño de nuevas soluciones, con la gaceta
    del plan ya resuelta y el flujo terminado, listo para resolverse (`demo.desarrollo.hasta_el_cierre`)."""
    if recurso.retirado_en is not None:
        return ["El caso está retirado: publícalo de nuevo."]
    if recurso.caso not in (contrato.DESARROLLO, contrato.DISENO):
        return ["La fase cierre solo aplica a --casos desarrollo y diseno."]
    from apps.catalogo.demo.desarrollo import hasta_el_cierre

    return hasta_el_cierre(recurso)


# --- Retirar ------------------------------------------------------------------------


@transaction.atomic
def retirar(recurso, actor, *, motivo="Retiro de la demostración de Órbita."):
    """Cancela los tickets abiertos del caso que `actor` puede cancelar, pausa la programación y despublica el
    Servicio. Sin borrados físicos: el historial, los tickets y la configuración se conservan."""
    from apps.tickets.operaciones import cancelar_ticket

    servicio = recurso.servicio
    lineas = []
    for ticket in _tickets(servicio).filter(estado__in=ABIERTOS).order_by("pk"):
        try:
            with transaction.atomic():
                cancelar_ticket(ticket, actor, motivo)
            lineas.append(f"Cancelado {ticket.codigo}.")
        except (PermissionDenied, ValidationError) as exc:
            texto = "; ".join(getattr(exc, "messages", [str(exc)]))
            lineas.append(f"Sin cancelar {ticket.codigo}: {texto} (lo cancela su solicitante o responsable).")
    programacion = ProgramacionProceso.objects.filter(servicio=servicio, activa=True).first()
    if programacion is not None:
        desactivar_programacion(servicio, actor)
        lineas.append("Programación pausada: no se generan más ejecuciones.")
    if servicio.activo:
        desactivar_servicio(servicio, actor)
        lineas.append("Servicio despublicado.")
    if recurso.retirado_en is None:
        recurso.retirado_en = timezone.now()
        recurso.save(update_fields=["retirado_en", "actualizado_en"])
    lineas.append(
        "Se conservan la configuración, la plantilla de fases, la categoría y el historial (no hay borrados físicos)."
    )
    return lineas
