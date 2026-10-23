"""Configuración de GACETAS y REGLAS DE FINALIZACIÓN de un Proceso — Fase 3 del modelo configurable.

Única autoridad del contrato JSON de `DefinicionGaceta.configuracion` y de `ReglaFinalizacion.configuracion`,
de su validación y de las operaciones de administración (crear, editar, retirar), auditadas. La ejecución vive en
`apps.gacetas`; la evaluación de las reglas de finalización, en `apps.tickets.finalizacion`.

Contrato de una gaceta (todo opcional salvo colecciones y aprobadores):

    colecciones            [clave de colección, …] del Proceso cuyos ítems participan
    condicion_ingreso      {"variable": "item.<columna>", "operador": …, "valor": …} o null
    ingreso                MANUAL | AL_CUMPLIR_CONDICION | AL_TERMINAR_RECORRIDO
                           (cómo entra un ítem además del bloque GACETA y de la presentación manual)
    apertura               AL_INICIAR (se abre al iniciar el ticket) | POR_FLUJO (la abre un bloque GACETA)
    exige_item_completo    bool: solo entra un ítem completo según sus columnas obligatorias
    revisores              [{"tipo": "USUARIO"|"EQUIPO", "usuario_id"|"equipo_id"}] — registran observaciones
    aprobadores            ídem — deciden (aprobar, devolver, rechazar, excluir) con `apps.aprobaciones`
    politica               TODOS | CUALQUIERA (con varios aprobadores)
    plazo_revision         {"cantidad", "unidad": DIAS|HORAS, "habiles"} desde la apertura (cierre del periodo)
    plazo_correccion       ídem, desde cada devolución u observación
    aceptacion_automatica  bool: al vencer el periodo, se aceptan los ítems en revisión sin observaciones
    permite_exclusion      bool: los aprobadores pueden EXCLUIR (rechazar siempre se puede)
    justificacion_al_aprobar  bool
    al_resolver_pendientes IMPEDIR | EXCLUIR | RECHAZAR (qué pasa con lo pendiente al resolver)
    resolucion_automatica  bool: resolver sola cuando se cierra el periodo y se puede
    responsable_correccion_columna  clave de una columna USUARIO/EQUIPO del ítem (vacío: responsable del ticket)
    coordinador            Fase 4.A: {"tipo": "USUARIO"|"EQUIPO", "usuario_id"|"equipo_id"} o null. Quién coordina
                           la gaceta (presenta, cierra el periodo, resuelve, excluye). Null: quien atiende el
                           ticket, como hasta ahora (`gacetas.autorizacion.puede_coordinar`).
    columnas_editables     Bloque 3.1: [clave de columna, …] que el responsable de una tarea de corrección ABIERTA
                           puede editar en SU ítem (`apps.tickets.edicion_por_tarea`). Vacía = la corrección no
                           concede edición del ítem. Cada clave debe existir en alguna colección de la gaceta.
    obligatoria            Fase 4.A: bool. Con la regla «Las gacetas están resueltas», una gaceta OBLIGATORIA que
                           nunca se abrió impide resolver el ticket; una opcional no bloquea solo por existir.
                           Ausente en las configuraciones congeladas antes de 4.A = opcional (comportamiento previo).
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from apps.catalogo import claves
from apps.catalogo.models import DefinicionGaceta, DefinicionParrilla, ReglaFinalizacion, Servicio
from apps.catalogo.operaciones import _exigir_administracion
from apps.core.auditoria import registrar_evento, serializar
from apps.core.models import Equipo, RegistroAuditoria, Usuario
from apps.tickets import tiempos

INGRESOS = ("MANUAL", "AL_CUMPLIR_CONDICION", "AL_TERMINAR_RECORRIDO")
APERTURAS = ("AL_INICIAR", "POR_FLUJO")
PENDIENTES = ("IMPEDIR", "EXCLUIR", "RECHAZAR")
POLITICAS = ("TODOS", "CUALQUIERA")
OPERADORES = (
    "IGUAL_A", "DISTINTO_DE", "CONTIENE", "NO_CONTIENE", "MAYOR_QUE", "MENOR_QUE", "ESTA_VACIO", "NO_ESTA_VACIO",
)

POR_DEFECTO = {
    "colecciones": [],
    "condicion_ingreso": None,
    "ingreso": "MANUAL",
    "apertura": "AL_INICIAR",
    "exige_item_completo": True,
    "revisores": [],
    "aprobadores": [],
    "politica": "TODOS",
    "plazo_revision": None,
    "plazo_correccion": None,
    "aceptacion_automatica": False,
    "permite_exclusion": False,
    "justificacion_al_aprobar": False,
    "al_resolver_pendientes": "IMPEDIR",
    "resolucion_automatica": False,
    "responsable_correccion_columna": "",
    "coordinador": None,
    "obligatoria": False,
    "columnas_editables": [],
}


def _plazo(valor, etiqueta, errores):
    if valor in (None, "", {}):
        return None
    cantidad, unidad = (valor or {}).get("cantidad"), (valor or {}).get("unidad")
    if isinstance(cantidad, bool) or not isinstance(cantidad, int) or not 1 <= cantidad <= tiempos.MAX_CANTIDAD:
        errores.append(f"El {etiqueta} debe ser un número entero de 1 a {tiempos.MAX_CANTIDAD}.")
        return None
    if unidad not in tiempos.UNIDADES:
        errores.append(f"El {etiqueta} necesita una unidad: días u horas.")
        return None
    return {"cantidad": cantidad, "unidad": unidad, "habiles": bool(valor.get("habiles"))}


def _actores(lista, etiqueta, errores):
    actores = []
    for dato in lista or []:
        tipo = (dato or {}).get("tipo")
        if tipo == "USUARIO" and dato.get("usuario_id"):
            actores.append({"tipo": "USUARIO", "usuario_id": int(dato["usuario_id"])})
        elif tipo == "EQUIPO" and dato.get("equipo_id"):
            actores.append({"tipo": "EQUIPO", "equipo_id": int(dato["equipo_id"])})
        else:
            errores.append(f"Hay {etiqueta} sin persona ni equipo.")
    # Sin repetidos, conservando el orden.
    vistos, unicos = set(), []
    for actor in actores:
        llave = (actor["tipo"], actor.get("usuario_id") or actor.get("equipo_id"))
        if llave not in vistos:
            vistos.add(llave)
            unicos.append(actor)
    return unicos


def normalizar(configuracion):
    """`(configuracion, errores)`: la configuración canónica (con todos los valores por defecto) y los errores
    de forma. No mira el Proceso (eso es `errores_de_definicion`)."""
    cfg = dict(POR_DEFECTO)
    cfg.update({k: v for k, v in (configuracion or {}).items() if k in POR_DEFECTO})
    errores = []
    cfg["colecciones"] = [c for c in dict.fromkeys(cfg.get("colecciones") or []) if isinstance(c, str) and c]
    if not cfg["colecciones"]:
        errores.append("Elige al menos una colección de ítems que participe en la gaceta.")
    condicion = cfg.get("condicion_ingreso")
    if condicion:
        variable = str(condicion.get("variable") or "")
        if not variable.startswith("item.") or condicion.get("operador") not in OPERADORES:
            errores.append("La condición de ingreso debe usar una columna del ítem y un operador válido.")
            condicion = None
        else:
            condicion = {"variable": variable, "operador": condicion["operador"], "valor": str(condicion.get("valor") or "")}
    cfg["condicion_ingreso"] = condicion or None
    for llave, permitidos in (("ingreso", INGRESOS), ("apertura", APERTURAS), ("al_resolver_pendientes", PENDIENTES),
                              ("politica", POLITICAS)):
        if cfg[llave] not in permitidos:
            errores.append(f"Valor no válido para «{llave}».")
            cfg[llave] = POR_DEFECTO[llave]
    cfg["revisores"] = _actores(cfg["revisores"], "revisores", errores)
    cfg["aprobadores"] = _actores(cfg["aprobadores"], "aprobadores", errores)
    if not cfg["aprobadores"]:
        errores.append("Indica al menos un aprobador (persona o equipo) de la gaceta.")
    cfg["plazo_revision"] = _plazo(cfg["plazo_revision"], "plazo de revisión", errores)
    cfg["plazo_correccion"] = _plazo(cfg["plazo_correccion"], "plazo de corrección", errores)
    for llave in ("exige_item_completo", "aceptacion_automatica", "permite_exclusion", "justificacion_al_aprobar",
                  "resolucion_automatica"):
        cfg[llave] = bool(cfg[llave])
    if cfg["aceptacion_automatica"] and cfg["plazo_revision"] is None:
        errores.append("La aceptación automática necesita un plazo de revisión.")
    cfg["responsable_correccion_columna"] = str(cfg.get("responsable_correccion_columna") or "")
    cfg["obligatoria"] = bool(cfg.get("obligatoria"))
    from apps.catalogo.actividades import columnas_editables

    cfg["columnas_editables"] = columnas_editables(cfg.get("columnas_editables"), errores)
    coordinador = cfg.get("coordinador")
    if coordinador:
        unico = _actores([coordinador], "coordinadores", errores)
        cfg["coordinador"] = unico[0] if unico else None
    else:
        cfg["coordinador"] = None
    return cfg, errores


def _columnas(servicio, clave):
    from apps.catalogo.configuracion_ejecucion import _columnas_de_coleccion

    return _columnas_de_coleccion(servicio, clave)


def errores_de_definicion(servicio, configuracion):
    """Errores de una configuración frente al Proceso: colecciones y columnas que existan, personas y equipos
    activos. Lista vacía = válida."""
    cfg, errores = normalizar(configuracion)
    existentes = {d.clave: d for d in DefinicionParrilla.objects.filter(servicio=servicio)}
    faltan = [c for c in cfg["colecciones"] if c not in existentes]
    if faltan:
        errores.append(f"La gaceta usa colecciones que el proceso no tiene ({', '.join(faltan)}).")
    columnas = {}
    for clave in cfg["colecciones"]:
        if clave in existentes:
            columnas.update(_columnas(servicio, clave))
    if cfg["condicion_ingreso"]:
        columna = cfg["condicion_ingreso"]["variable"].split(".", 1)[1]
        if columna not in columnas:
            errores.append("La condición de ingreso usa una columna que no tienen las colecciones de la gaceta.")
    if cfg["responsable_correccion_columna"]:
        campo = columnas.get(cfg["responsable_correccion_columna"])
        if campo is None or campo.tipo not in ("USUARIO", "EQUIPO"):
            errores.append("El responsable de la corrección debe salir de una columna de persona o equipo.")
    faltan = [clave for clave in cfg["columnas_editables"] if clave not in columnas]
    if faltan:
        errores.append(f"La corrección permite editar columnas que las colecciones de la gaceta no tienen ({', '.join(faltan)}).")
    for etiqueta, lista in (
        ("revisor", cfg["revisores"]), ("aprobador", cfg["aprobadores"]),
        ("coordinador", [cfg["coordinador"]] if cfg["coordinador"] else []),
    ):
        for actor in lista:
            if actor["tipo"] == "USUARIO" and not Usuario.objects.filter(pk=actor["usuario_id"], is_active=True).exists():
                errores.append(f"Un {etiqueta} de la gaceta ya no está activo.")
            if actor["tipo"] == "EQUIPO" and not Equipo.objects.filter(pk=actor["equipo_id"], activo=True).exists():
                errores.append(f"Un equipo {etiqueta} de la gaceta ya no está activo.")
    return errores


def definiciones_activas(servicio):
    return list(DefinicionGaceta.objects.filter(servicio=servicio, activa=True).order_by("orden", "pk"))


def errores_de_gacetas(servicio):
    """Para publicar: cada gaceta activa debe seguir siendo válida."""
    errores = []
    for definicion in definiciones_activas(servicio):
        errores.extend(f"Gaceta «{definicion.nombre}»: {e}" for e in errores_de_definicion(servicio, definicion.configuracion))
    return errores


@transaction.atomic
def configurar_gaceta(servicio, actor, *, nombre, descripcion="", configuracion, definicion=None):
    """Crea (sin `definicion`) o edita una gaceta del Proceso. Valida, sube la versión y audita. Solo afecta a
    los tickets que se creen después."""
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.tipo != Servicio.Tipo.PROCESO or servicio.es_ticket_general:
        raise ValidationError("Las gacetas solo existen en un proceso.")
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValidationError("La gaceta necesita un nombre.")
    errores = errores_de_definicion(servicio, configuracion)
    if errores:
        raise ValidationError(errores)
    cfg, _ = normalizar(configuracion)
    if definicion is None:
        existentes = DefinicionGaceta.objects.filter(servicio=servicio).values_list("clave", flat=True)
        orden = (DefinicionGaceta.objects.filter(servicio=servicio).aggregate(m=Max("orden"))["m"] or 0) + 1
        definicion = DefinicionGaceta.objects.create(
            servicio=servicio, clave=claves.clave_unica(claves.clave_desde_texto(nombre, por_defecto="gaceta"), existentes),
            nombre=nombre, descripcion=descripcion or "", orden=orden, configuracion=cfg,
        )
        registrar_evento(
            accion=RegistroAuditoria.Accion.CREAR, instancia=definicion, origen=RegistroAuditoria.Origen.USUARIO,
            usuario=actor, datos_anteriores=None, datos_nuevos=serializar(definicion),
        )
        return definicion
    definicion = DefinicionGaceta.objects.select_for_update().get(pk=definicion.pk, servicio=servicio)
    anterior = serializar(definicion)
    if (definicion.nombre, definicion.descripcion, definicion.configuracion) == (nombre, descripcion or "", cfg):
        return definicion
    definicion.nombre, definicion.descripcion, definicion.configuracion = nombre, descripcion or "", cfg
    definicion.version += 1
    definicion.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=definicion, origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor, datos_anteriores=anterior, datos_nuevos=serializar(definicion),
    )
    return definicion


@transaction.atomic
def cambiar_estado_gaceta(definicion, actor, *, activa):
    """Retirar (desactivar) o reactivar una gaceta. Retirar no afecta a los tickets que ya la tienen. Una gaceta
    que un bloque GACETA de una configuración vigente o en borrador usa no se retira."""
    _exigir_administracion(actor)
    definicion = DefinicionGaceta.objects.select_for_update().get(pk=definicion.pk)
    if definicion.activa == bool(activa):
        return definicion
    if not activa:
        from apps.catalogo.models import BloqueOperativo

        en_uso = BloqueOperativo.objects.filter(
            version__servicio=definicion.servicio, version__estado__in=("ACTIVA", "BORRADOR"), tipo="GACETA",
            configuracion__gaceta=definicion.clave,
        ).exists()
        if en_uso:
            raise ValidationError("Un bloque del flujo usa esta gaceta: quítalo antes de retirarla.")
    definicion.activa = bool(activa)
    definicion.save(update_fields=["activa", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=definicion, origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor, datos_anteriores={"activa": not activa}, datos_nuevos={"activa": bool(activa)},
    )
    return definicion


# --- Reglas de finalización ------------------------------------------------------------------------------

RESULTADOS_ITEM = ("APROBADO", "COMPLETADO", "RECHAZADO", "EXCLUIDO")

REGLAS_POR_DEFECTO = {
    "actividades_cerradas": False,
    "recorridos_terminados": False,
    "aprobaciones_resueltas": False,
    "gacetas_resueltas": True,
    "sin_correcciones": True,
    "alcance_cerrado": False,
    "items_con_resultado": False,
    "resultados_admitidos": list(RESULTADOS_ITEM),
    "minimo_aprobados": 0,
}


def normalizar_reglas(configuracion):
    cfg = dict(REGLAS_POR_DEFECTO)
    cfg.update({k: v for k, v in (configuracion or {}).items() if k in REGLAS_POR_DEFECTO})
    errores = []
    for llave, valor in REGLAS_POR_DEFECTO.items():
        if isinstance(valor, bool):
            cfg[llave] = bool(cfg[llave])
    admitidos = [r for r in (cfg.get("resultados_admitidos") or []) if r in RESULTADOS_ITEM]
    if cfg["items_con_resultado"] and not admitidos:
        errores.append("Indica qué resultados finales de los ítems se admiten para cerrar.")
    cfg["resultados_admitidos"] = admitidos or list(RESULTADOS_ITEM)
    minimo = cfg.get("minimo_aprobados") or 0
    if isinstance(minimo, bool) or not isinstance(minimo, int) or minimo < 0:
        errores.append("El mínimo de ítems aprobados debe ser un número entero, cero o mayor.")
        minimo = 0
    cfg["minimo_aprobados"] = minimo
    return cfg, errores


def reglas_de(servicio):
    regla = ReglaFinalizacion.objects.filter(servicio=servicio).first()
    return normalizar_reglas(regla.configuracion if regla is not None else None)[0]


@transaction.atomic
def configurar_reglas_finalizacion(servicio, actor, configuracion):
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.tipo != Servicio.Tipo.PROCESO or servicio.es_ticket_general:
        raise ValidationError("Las reglas de finalización solo existen en un proceso.")
    cfg, errores = normalizar_reglas(configuracion)
    if errores:
        raise ValidationError(errores)
    regla, creada = ReglaFinalizacion.objects.get_or_create(servicio=servicio, defaults={"configuracion": cfg})
    anterior = None if creada else dict(regla.configuracion)
    if not creada:
        if regla.configuracion == cfg:
            return regla
        regla.configuracion = cfg
        regla.save(update_fields=["configuracion", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR if creada else RegistroAuditoria.Accion.ACTUALIZAR, instancia=regla,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor, datos_anteriores=anterior, datos_nuevos=cfg,
    )
    return regla
