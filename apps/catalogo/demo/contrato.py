"""Contrato de los cuatro casos de la demostración (`docs/demo/ORBITA_DEMO.md`, Parte C), como DATOS.

La preparación los construye con las operaciones de dominio y la verificación compara lo que existe contra
ellos. Las claves de campos y columnas son EXPLÍCITAS (no se dejan generar desde la etiqueta) porque las
actividades, la gaceta y las decisiones las referencian.

La configuración de cada actividad, aprobación o gaceta es una función de un `Contexto` (personas y equipos
elegidos por parámetro, claves de la colección y de la gaceta creadas), nunca ids fijos.

`VERSION_CONTRATO` se guarda en `RecursoDemo.version_contrato`: si cambia el contrato, la verificación lo avisa
(un caso ya preparado no se reconstruye solo).
"""

from dataclasses import dataclass, field

# v2 (UX-4): la parrilla envía cada idea a aprobación (sin tarea «Idea» por ítem), exige datos por etapa,
# fecha dentro del periodo y la corrección de la gaceta llega a quien produjo la pieza.
# v3 (UX-4b): la lluvia de ideas se envía COMPLETA a aprobación (`envio = CONJUNTO`) y ese envío cierra el alcance.
VERSION_CONTRATO = 3

PARRILLA = "PARRILLA"
WEBINAR = "WEBINAR"
PIEZA_GRAFICA = "PIEZA_GRAFICA"
PRESENTACIONES = "PRESENTACIONES"
CASOS = (PARRILLA, WEBINAR, PIEZA_GRAFICA, PRESENTACIONES)
# Nombre corto de cada caso en la línea de comandos.
ALIAS = {
    "parrilla": PARRILLA, "webinar": WEBINAR, "pieza": PIEZA_GRAFICA, "presentaciones": PRESENTACIONES,
    "imperandina": "IMPERANDINA",
}
# Los casos que el comando toma sin `--casos` (la demo original). Imperandina se pide de forma explícita.
ALIAS_POR_DEFECTO = ("parrilla", "webinar", "pieza", "presentaciones")


@dataclass(frozen=True)
class CampoSpec:
    etiqueta: str
    clave: str
    tipo: str
    obligatorio: bool = False
    configuracion: dict = field(default_factory=dict)
    opciones: tuple = ()  # ((valor, etiqueta), …) para LISTA / MULTILISTA
    es_fecha_requerida: bool = False
    ayuda: str = ""


@dataclass(frozen=True)
class ReglaSpec:
    origen: str  # clave del campo
    operador: str
    valor: str
    objetivo: str
    efecto: str


@dataclass(frozen=True)
class EntregableSpec:
    nombre: str
    tipo: str
    obligatorio: bool = False
    descripcion: str = ""


@dataclass(frozen=True)
class BloqueSpec:
    nombre: str
    opcion: str  # código de `apps.catalogo.intenciones` («¿Qué necesitas que suceda?»)
    fase: str
    configuracion: object = None  # Contexto -> dict
    contenedor: str = ""  # nombre del recorrido que lo contiene (rama ITEM)
    descripcion: str = ""
    entregable: str = ""  # nombre de la definición de entregable (bloque ENTREGABLE)
    revisa: str = ""  # nombre del bloque ENTREGABLE que revisa (APROBACION)
    requiere: str = ""  # atributo de `Participantes` sin el cual el bloque no se crea (opcional del contrato)
    entra_al_enviar: bool = False  # UX-4: paso por el que entra un ítem ENVIADO a su recorrido (`paso_al_enviar`)

    @property
    def rama(self):
        return "ITEM" if self.contenedor else ""


@dataclass(frozen=True)
class RutaSpec:
    origen: str
    destino: str = ""  # vacío + finaliza = FINALIZAR
    resultado: str = ""  # APROBADA | DEVUELTA | RECHAZADA (aprobaciones)
    finaliza: bool = False
    variable: str = ""  # decisiones
    operador: str = ""
    valor: str = ""
    es_fallback: bool = False
    requiere: str = ""

    def reglas(self):
        reglas = {"finaliza": self.finaliza}
        if self.resultado:
            reglas["resultado_aprobacion"] = self.resultado
        if self.es_fallback:
            reglas["es_fallback"] = True
        if self.variable:
            reglas.update(variable=self.variable, operador=self.operador, valor=self.valor)
        return reglas


@dataclass(frozen=True)
class Caso:
    codigo: str
    nombre: str
    tipo: str  # SERVICIO | PROCESO
    descripcion: str
    instrucciones: str = ""
    responsable: str = ""  # atributo de `Participantes`: (tipo, objeto) o persona
    visibilidad: bool = True  # se ofrece a los empleados (un programado no se solicita)
    tiempo_objetivo: tuple = ()  # (cantidad, unidad, hábiles)
    entrega: tuple = ()  # (política, días de observación)
    formulario: str = ""  # nombre del formulario de inicio
    campos: tuple = ()
    reglas: tuple = ()
    entregables: tuple = ()
    coleccion: str = ""  # nombre de cada ítem
    columnas: tuple = ()
    gaceta: object = None  # GacetaSpec
    finalizacion: dict = field(default_factory=dict)
    fases: tuple = ()
    bloques: tuple = ()
    rutas: tuple = ()
    programado: bool = False
    # Demo Imperandina: un caso puede fijar su categoría, su nombre exacto (sin el sufijo de la demo), sus
    # personas (`(atributo, username)`), sus equipos (`EquipoSpec`), su calendario y las ideas de su ticket de prueba.
    categoria: str = ""
    nombre_exacto: bool = False
    personas: tuple = ()
    equipos: tuple = ()
    calendario: tuple = ()  # (día de creación, periodo)
    ideas: tuple = ()  # IdeaSpec


@dataclass(frozen=True)
class EquipoSpec:
    """Equipo EXISTENTE o que la demo crea si no existe (por nombre), con estos integrantes (usernames que ya
    existen). Nunca se duplican equipos ni membresías y nunca se retira a nadie."""

    nombre: str
    descripcion: str
    miembros: tuple
    atributo: str  # atributo de `Participantes` donde queda el equipo


@dataclass(frozen=True)
class IdeaSpec:
    """Una idea del ticket de prueba: queda como BORRADOR de la lluvia de ideas (no se envía ni se aprueba)."""

    dia: int  # día del mes del periodo de la ejecución
    tipo: str
    medio: str
    copy: str
    contenido: str


@dataclass(frozen=True)
class GacetaSpec:
    nombre: str
    descripcion: str
    configuracion: object  # Contexto -> dict


# --- Ayudas de configuración --------------------------------------------------


def persona(usuario):
    return {"tipo": "USUARIO", "usuario_id": usuario.pk}


def actor_de(referencia):
    """`(tipo, objeto)` → actor de gaceta `{"tipo", "usuario_id"|"equipo_id"}`."""
    tipo, objeto = referencia
    return {"tipo": "USUARIO", "usuario_id": objeto.pk} if tipo == "USUARIO" else {"tipo": "EQUIPO", "equipo_id": objeto.pk}


def aprobador(usuario):
    return {
        "modo": "SECUENCIAL", "politica": "",
        "participantes": [{"tipo": "USUARIO", "usuario_id": usuario.pk, "equipo_id": None}],
    }


def tarea_de(usuario, **extra):
    return {"tipo_actor": "USUARIO", "usuario_id": usuario.pk, **extra}


def tarea_del_responsable(**extra):
    return {"tipo_actor": "RESPONSABLE_TICKET", **extra}


ARCHIVOS_PIEZA = {
    "extensiones_permitidas": ["png", "jpg", "jpeg", "gif", "webp", "pdf", "mp4", "mov", "m4v", "webm"],
    "tamano_maximo_mb": 200,
}
ARCHIVOS_REFERENCIA = {"extensiones_permitidas": ["png", "jpg", "jpeg", "pdf", "pptx", "docx"], "tamano_maximo_mb": 20}


# --- Caso 1 — Parrilla mensual de publicaciones (Proceso programado) ------------

COPY, TIPO_PIEZA, CONTENIDO, FECHA_PUBLICACION, ARCHIVO_PIEZA, RESPONSABLE_PUBLICACION = (
    "copy", "tipo_de_pieza", "contenido_de_la_pieza", "fecha_de_publicacion", "archivo_de_la_pieza",
    "responsable_de_publicacion",
)
RECORRIDO = "Recorrido de cada publicación"

PARRILLA_CASO = Caso(
    codigo=PARRILLA,
    nombre="Parrilla mensual de publicaciones",
    tipo="PROCESO",
    descripcion="Cada mes se planean, producen, revisan y publican las piezas de la parrilla de contenidos.",
    responsable="planificador",
    visibilidad=False,
    tiempo_objetivo=(30, "DIAS", False),
    coleccion="Publicación",
    columnas=(
        CampoSpec("Copy", COPY, "TEXTO_LARGO", obligatorio=True),
        CampoSpec(
            "Tipo de pieza", TIPO_PIEZA, "LISTA", obligatorio=True,
            opciones=(("post", "Post"), ("reel", "Reel"), ("historia", "Historia"), ("carrusel", "Carrusel")),
        ),
        CampoSpec("Contenido de la pieza", CONTENIDO, "TEXTO_LARGO", obligatorio=True,
                  ayuda="Descripción de lo que hay que producir."),
        CampoSpec("Fecha de publicación", FECHA_PUBLICACION, "FECHA", obligatorio=True,
                  configuracion={"dentro_del_periodo": True},
                  ayuda="Debe estar dentro del mes de la parrilla."),
        CampoSpec("Archivo de la pieza", ARCHIVO_PIEZA, "ARCHIVO", obligatorio=True, configuracion=ARCHIVOS_PIEZA,
                  ayuda="Lo adjunta quien produce la pieza, desde su tarea de Producción."),
        CampoSpec("Responsable de publicación", RESPONSABLE_PUBLICACION, "USUARIO", configuracion={"solo_activos": True},
                  ayuda="Opcional: informativo para el equipo; la tarea de Publicación es de la persona configurada."),
    ),
    gaceta=GacetaSpec(
        "Revisión colaborativa de piezas",
        "Cada pieza aprobada internamente se revisa aquí antes de publicarse; los demás ítems siguen a su ritmo.",
        lambda ctx: {
            "colecciones": [ctx.coleccion],
            "condicion_ingreso": None,
            "ingreso": "MANUAL",  # la presenta el paso del recorrido (5.1)
            "apertura": "AL_INICIAR",
            "exige_item_completo": True,  # exige el archivo producido
            "revisores": [actor_de(r) for r in ctx.p.revisores],
            "aprobadores": [actor_de(a) for a in ctx.p.aprobadores_gaceta],
            "politica": "TODOS",
            "plazo_revision": None,  # 5.1: una gaceta por ítem no tiene periodo
            "plazo_correccion": {"cantidad": 2, "unidad": "DIAS", "habiles": True},
            "aceptacion_automatica": False,
            "permite_exclusion": True,
            "justificacion_al_aprobar": False,
            "al_resolver_pendientes": "IMPEDIR",
            "resolucion_automatica": False,
            "responsable_correccion_columna": "",
            # UX-4: la corrección la recibe quien produjo la pieza (la última actividad del ítem).
            "responsable_correccion_origen": "ULTIMA_ACTIVIDAD",
            "coordinador": None,
            "obligatoria": True,
            "columnas_editables": [COPY, CONTENIDO, ARCHIVO_PIEZA],
            # UX-4b: en la revisión colaborativa solo se ve la pieza (su archivo) y la fecha de publicación.
            "columnas_visibles": [FECHA_PUBLICACION, ARCHIVO_PIEZA],
        },
    ),
    finalizacion={
        "actividades_cerradas": True, "recorridos_terminados": True, "aprobaciones_resueltas": False,
        "gacetas_resueltas": True, "sin_correcciones": True, "alcance_cerrado": True, "items_con_resultado": True,
        "resultados_admitidos": ["APROBADO", "COMPLETADO", "RECHAZADO", "EXCLUIDO"], "minimo_aprobados": 0,
    },
    fases=("Publicaciones", "Cierre"),
    bloques=(
        # UX-4: no hay tarea de planeación ni una tarea «Idea» por ítem. El Planificador arma la LLUVIA DE IDEAS
        # (borradores que no avanzan) y la envía COMPLETA a aprobación: cada idea entra a su recorrido por
        # «Aprobación de la idea» y el alcance se cierra (no se agregan más). «Ajustar la idea» solo existe si la
        # aprobación devuelve esa idea.
        BloqueSpec(
            RECORRIDO, "recorrido", "Publicaciones",
            lambda ctx: {
                "coleccion": ctx.coleccion, "ingreso": "AL_ENVIAR", "envio": "CONJUNTO",
                "etapa_borradores": "Lluvia de ideas",
                "columnas_requeridas_ingreso": [COPY, TIPO_PIEZA, CONTENIDO, FECHA_PUBLICACION],
                "incorporar_nuevos": False, "espera_alcance_cerrado": True,
            },
        ),
        BloqueSpec(
            "Ajustar la idea", "tarea", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: tarea_de(
                ctx.p.planificador,
                columnas_editables=[COPY, TIPO_PIEZA, CONTENIDO, FECHA_PUBLICACION],
                columnas_requeridas=[COPY, TIPO_PIEZA, CONTENIDO, FECHA_PUBLICACION],
            ),
            descripcion="Corrige la idea según la observación y reenvíala a aprobación.",
        ),
        BloqueSpec(
            "Aprobación de la idea", "aprobacion", "Publicaciones", contenedor=RECORRIDO, entra_al_enviar=True,
            configuracion=lambda ctx: {**aprobador(ctx.p.aprobador), "permite_exclusion": True},
        ),
        BloqueSpec(
            "Producción", "tarea", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: tarea_de(
                ctx.p.productor, columnas_editables=[ARCHIVO_PIEZA], columnas_requeridas=[ARCHIVO_PIEZA],
            ),
            descripcion="Produce la pieza y adjunta el archivo en «Archivo de la pieza».",
        ),
        BloqueSpec(
            "Aprobación de la pieza", "aprobacion", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: {**aprobador(ctx.p.aprobador), "permite_exclusion": True},
        ),
        BloqueSpec(
            "Revisión colaborativa de la pieza", "gaceta_item", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: {"gaceta": ctx.gaceta},
        ),
        BloqueSpec(
            "Publicación", "tarea", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: tarea_de(
                ctx.p.responsable_publicacion,
                fecha_prevista_campo=f"item.{FECHA_PUBLICACION}", fecha_limite_campo=f"item.{FECHA_PUBLICACION}",
                verificacion=[{"texto": "Publicada en el canal", "obligatorio": True}],
            ),
            descripcion="Publica la pieza en la fecha de publicación del ítem.",
        ),
        BloqueSpec(
            "Evidencia de publicación", "informacion", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: tarea_de(
                ctx.p.responsable_publicacion, requiere_evidencia=True,
                plazo_limite={"cantidad": 1, "unidad": "DIAS"}, plazo_habiles=True,
            ),
            descripcion="Adjunta la captura o el enlace de la publicación.",
        ),
        BloqueSpec(
            "Cerrar la parrilla", "tarea", "Cierre", lambda ctx: tarea_del_responsable(),
            descripcion="Revisa el resultado de cada publicación y resuelve la gaceta.",
        ),
    ),
    rutas=(
        RutaSpec("Aprobación de la idea", "Ajustar la idea", resultado="DEVUELTA"),
        RutaSpec("Aprobación de la pieza", "Producción", resultado="DEVUELTA"),
    ),
    programado=True,
)


# --- Caso 2 — Webinar (Proceso manual) -----------------------------------------

REQUIERE_AUDIOVISUAL = "requiere_audiovisual"

WEBINAR_CASO = Caso(
    codigo=WEBINAR,
    nombre="Webinar",
    tipo="PROCESO",
    descripcion="Organización de un webinar: propuesta, aprobación, preparación, realización y cierre.",
    instrucciones="Describe el objetivo, el público y la fecha tentativa del webinar.",
    responsable="coordinador",
    tiempo_objetivo=(15, "DIAS", True),
    formulario="Solicitud de webinar",
    campos=(
        CampoSpec("Objetivo", "objetivo", "TEXTO_LARGO", obligatorio=True),
        CampoSpec(
            "Tipo de evento", "tipo_de_evento", "LISTA", obligatorio=True,
            opciones=(("conferencia", "Conferencia"), ("taller", "Taller"), ("panel", "Panel"),
                      ("capacitacion", "Capacitación")),
        ),
        CampoSpec("Área", "area", "AREA", obligatorio=True, configuracion={"solo_activos": True}),
        CampoSpec("Tema", "tema", "TEXTO", obligatorio=True),
        CampoSpec("Fecha tentativa", "fecha_tentativa", "FECHA", obligatorio=True, es_fecha_requerida=True),
        CampoSpec(
            "Público objetivo", "publico_objetivo", "MULTILISTA", obligatorio=True,
            opciones=(("colaboradores", "Colaboradores"), ("clientes", "Clientes"), ("aliados", "Aliados"),
                      ("publico_general", "Público general")),
        ),
        CampoSpec("Expositor", "expositor", "TEXTO"),
        CampoSpec(
            "Modalidad", "modalidad", "LISTA", obligatorio=True,
            opciones=(("virtual", "Virtual"), ("presencial", "Presencial"), ("hibrido", "Híbrido")),
        ),
        CampoSpec("Contenidos", "contenidos", "TEXTO_LARGO"),
        CampoSpec(
            "Difusión", "difusion", "MULTILISTA",
            opciones=(("correo", "Correo"), ("intranet", "Intranet"), ("redes", "Redes sociales")),
        ),
        CampoSpec("¿Requiere audiovisual?", REQUIERE_AUDIOVISUAL, "BOOLEANO"),
        CampoSpec("Requerimientos audiovisuales", "requerimientos_audiovisuales", "TEXTO_LARGO"),
    ),
    reglas=(
        ReglaSpec(REQUIERE_AUDIOVISUAL, "IGUAL_A", "true", "requerimientos_audiovisuales", "MOSTRAR"),
        ReglaSpec(REQUIERE_AUDIOVISUAL, "IGUAL_A", "true", "requerimientos_audiovisuales", "REQUERIR"),
    ),
    entregables=(
        EntregableSpec("Propuesta del webinar", "ARCHIVO", obligatorio=True),
        EntregableSpec("Grabación o enlace", "ENLACE"),
        EntregableSpec("Informe de cierre", "TEXTO"),
    ),
    fases=("Planeación", "Aprobación", "Coordinación y preparación", "Ejecución", "Cierre"),
    bloques=(
        BloqueSpec("Propuesta", "resultado", "Planeación", entregable="Propuesta del webinar",
                   descripcion="Quien atiende el ticket sube la propuesta del webinar."),
        BloqueSpec("Aprobación de la propuesta", "aprobacion", "Aprobación",
                   lambda ctx: aprobador(ctx.p.aprobador_webinar), revisa="Propuesta"),
        BloqueSpec(
            "Coordinar y preparar", "tarea", "Coordinación y preparación",
            lambda ctx: tarea_del_responsable(
                permite_subtareas=True,
                verificacion=[
                    {"texto": "Expositor confirmado", "obligatorio": True},
                    {"texto": "Piezas de difusión listas", "obligatorio": True},
                    {"texto": "Enlace de la transmisión", "obligatorio": True},
                    {"texto": "Audiovisual coordinado", "obligatorio": False},
                ],
            ),
            descripcion="Coordina expositor, difusión y logística; los apoyos puntuales van como subtareas.",
        ),
        BloqueSpec("¿Requiere audiovisual?", "decision", "Coordinación y preparación", requiere="equipo_audiovisual"),
        BloqueSpec(
            "Apoyo audiovisual", "tarea", "Coordinación y preparación",
            lambda ctx: {"tipo_actor": "EQUIPO", "equipo_id": ctx.p.equipo_audiovisual.pk},
            requiere="equipo_audiovisual",
        ),
        BloqueSpec("Realizar el webinar", "informacion", "Ejecución",
                   lambda ctx: tarea_del_responsable(requiere_evidencia=True),
                   descripcion="Adjunta la evidencia del webinar realizado."),
        BloqueSpec("Cierre e informe", "tarea", "Cierre", lambda ctx: tarea_del_responsable(),
                   descripcion="Registra el informe de cierre y la grabación en los resultados del ticket."),
    ),
    rutas=(
        RutaSpec("Aprobación de la propuesta", "Coordinar y preparar", resultado="APROBADA"),
        RutaSpec("Aprobación de la propuesta", "Propuesta", resultado="DEVUELTA"),
        RutaSpec("Aprobación de la propuesta", resultado="RECHAZADA", finaliza=True),
        RutaSpec("¿Requiere audiovisual?", "Apoyo audiovisual", variable=f"formulario.{REQUIERE_AUDIOVISUAL}",
                 operador="IGUAL_A", valor="true", requiere="equipo_audiovisual"),
        RutaSpec("¿Requiere audiovisual?", "Realizar el webinar", es_fallback=True, requiere="equipo_audiovisual"),
    ),
)


# --- Caso 3 — Pieza gráfica (Servicio) ------------------------------------------

PIEZA_CASO = Caso(
    codigo=PIEZA_GRAFICA,
    nombre="Pieza gráfica",
    tipo="SERVICIO",
    descripcion="Diseño de una pieza gráfica para un canal interno o externo.",
    instrucciones="Indica el formato, el texto y la fecha en que necesitas la pieza.",
    responsable="equipo_diseno",
    tiempo_objetivo=(3, "DIAS", True),
    entrega=("PERIODO_OBSERVACIONES", 3),
    formulario="Solicitud de pieza gráfica",
    campos=(
        CampoSpec(
            "Tipo de pieza", "tipo_de_pieza", "LISTA", obligatorio=True,
            opciones=(("post", "Post"), ("banner", "Banner"), ("infografia", "Infografía"), ("flyer", "Flyer")),
        ),
        CampoSpec("Formato y medidas", "formato_y_medidas", "TEXTO", obligatorio=True),
        CampoSpec("Texto / copy", "texto_copy", "TEXTO_LARGO", obligatorio=True),
        CampoSpec(
            "Canal", "canal", "MULTILISTA", obligatorio=True,
            opciones=(("redes", "Redes sociales"), ("web", "Sitio web"), ("correo", "Correo"), ("impreso", "Impreso")),
        ),
        CampoSpec("Referencias", "referencias", "ARCHIVO", configuracion=ARCHIVOS_REFERENCIA),
        CampoSpec("Fecha en que la necesito", "fecha_necesaria", "FECHA", obligatorio=True, es_fecha_requerida=True),
    ),
    entregables=(EntregableSpec("Pieza final", "ARCHIVO", obligatorio=True),),
)


# --- Caso 4 — Creación de presentaciones comerciales (Servicio) -----------------

PRESENTACIONES_CASO = Caso(
    codigo=PRESENTACIONES,
    nombre="Creación de presentaciones comerciales",
    tipo="SERVICIO",
    descripcion="Elaboración de una presentación comercial para una reunión con un cliente u oportunidad.",
    instrucciones="Cuéntanos el cliente, el objetivo y la fecha de la reunión.",
    responsable="responsable_presentaciones",
    tiempo_objetivo=(5, "DIAS", True),
    entrega=("CIERRE_DIRECTO", None),
    formulario="Solicitud de presentación comercial",
    campos=(
        CampoSpec("Cliente / oportunidad", "cliente_oportunidad", "TEXTO", obligatorio=True),
        CampoSpec("Objetivo de la presentación", "objetivo", "TEXTO_LARGO", obligatorio=True),
        CampoSpec("Audiencia", "audiencia", "TEXTO", obligatorio=True),
        CampoSpec("Contenidos requeridos", "contenidos_requeridos", "TEXTO_LARGO", obligatorio=True),
        CampoSpec("Referencias", "referencias", "ARCHIVO", configuracion=ARCHIVOS_REFERENCIA),
        CampoSpec("Enlaces de referencia", "enlaces_de_referencia", "URL"),
        CampoSpec("Fecha de la reunión", "fecha_de_la_reunion", "FECHA_HORA", obligatorio=True, es_fecha_requerida=True),
    ),
    entregables=(
        EntregableSpec("Presentación", "ARCHIVO", obligatorio=True),
        EntregableSpec("Enlace", "ENLACE"),
    ),
)


# --- Caso 5 — Parrilla mensual - Imperandina (Proceso programado, demo empresarial) ------------------------------
# Misma estructura que la parrilla (lluvia de ideas enviada completa, recorrido por publicación, gaceta por ítem),
# con el medio de publicación, personas y equipos reales de Mercadeo y una gaceta con periodo COMPARTIDO de 7 días
# y silencio positivo (la ventana no cierra mientras falten piezas por llegar: `catalogo.gacetas.errores_de_uso_por_item`).
# No entra en los casos por defecto del comando: se prepara con `--casos imperandina`.

IMPERANDINA = "IMPERANDINA"
MEDIO_PUBLICACION = "medio_de_publicacion"
COLUMNAS_IDEA_IMPERANDINA = [COPY, TIPO_PIEZA, MEDIO_PUBLICACION, CONTENIDO, FECHA_PUBLICACION]

IMPERANDINA_CASO = Caso(
    codigo=IMPERANDINA,
    nombre="Parrilla mensual - Imperandina",
    tipo="PROCESO",
    descripcion="Proceso mensual para planificar, aprobar, producir, revisar y publicar los contenidos de Imperandina.",
    responsable="planificador",
    visibilidad=False,
    tiempo_objetivo=(30, "DIAS", False),
    coleccion="Publicación",
    columnas=(
        CampoSpec("Copy", COPY, "TEXTO_LARGO", obligatorio=True),
        CampoSpec(
            "Tipo de pieza", TIPO_PIEZA, "LISTA", obligatorio=True,
            opciones=(("post", "Post"), ("reel", "Reel"), ("historia", "Historia"), ("carrusel", "Carrusel")),
        ),
        CampoSpec(
            "Medio de publicación", MEDIO_PUBLICACION, "LISTA", obligatorio=True,
            opciones=(("instagram", "Instagram"), ("facebook", "Facebook"), ("linkedin", "LinkedIn"), ("tiktok", "TikTok")),
        ),
        CampoSpec("Contenido de la pieza", CONTENIDO, "TEXTO_LARGO", obligatorio=True,
                  ayuda="Descripción de lo que hay que producir."),
        CampoSpec("Fecha de publicación", FECHA_PUBLICACION, "FECHA", obligatorio=True,
                  configuracion={"dentro_del_periodo": True},
                  ayuda="Debe estar dentro del mes de la parrilla."),
        # Obligatoria para el ítem completo (Producción y la gaceta la exigen), pero NO para enviar la idea: la
        # lluvia de ideas solo exige `COLUMNAS_IDEA_IMPERANDINA`.
        CampoSpec("Archivo de la pieza", ARCHIVO_PIEZA, "ARCHIVO", obligatorio=True, configuracion=ARCHIVOS_PIEZA,
                  ayuda="Lo adjunta Producción; se puede reemplazar si la pieza vuelve."),
        CampoSpec("Responsable de publicación", RESPONSABLE_PUBLICACION, "USUARIO", configuracion={"solo_activos": True},
                  ayuda="Opcional e informativo: la tarea de Publicación es de la persona configurada."),
    ),
    gaceta=GacetaSpec(
        "Revisión de contenidos - Imperandina",
        "Mercadeo Imperandina revisa cada pieza aprobada en una ventana compartida de 7 días; lo que no tenga "
        "observaciones al vencer queda aprobado (silencio positivo).",
        lambda ctx: {
            "colecciones": [ctx.coleccion],
            "condicion_ingreso": None,
            "ingreso": "MANUAL",  # la presenta el paso del recorrido
            "apertura": "POR_FLUJO",  # la primera pieza aprobada la abre e inicia el plazo
            "exige_item_completo": True,  # exige el archivo producido
            "revisores": [actor_de(("EQUIPO", ctx.p.equipo_revisor))],
            "aprobadores": [actor_de(("USUARIO", ctx.p.aprobador))],  # decide y resuelve lo pendiente al vencer
            "politica": "TODOS",
            "plazo_revision": {"cantidad": 7, "unidad": "DIAS", "habiles": False},
            "plazo_correccion": {"cantidad": 2, "unidad": "DIAS", "habiles": True},
            "aceptacion_automatica": True,  # silencio positivo: nunca para lo que tiene observaciones pendientes
            "permite_exclusion": True,
            "justificacion_al_aprobar": False,
            "al_resolver_pendientes": "IMPEDIR",
            "resolucion_automatica": False,  # la resuelve la coordinadora antes del cierre
            "responsable_correccion_columna": "",
            "responsable_correccion_origen": "ULTIMA_ACTIVIDAD",  # quien produjo la pieza (Producción)
            "coordinador": {"tipo": "USUARIO", "usuario_id": ctx.p.planificador.pk},
            "obligatoria": True,
            "columnas_editables": [COPY, CONTENIDO, ARCHIVO_PIEZA],
            "columnas_visibles": [COPY, TIPO_PIEZA, MEDIO_PUBLICACION, CONTENIDO, FECHA_PUBLICACION, ARCHIVO_PIEZA],
        },
    ),
    finalizacion={
        "actividades_cerradas": True, "recorridos_terminados": True, "aprobaciones_resueltas": False,
        "gacetas_resueltas": True, "sin_correcciones": True, "alcance_cerrado": True, "items_con_resultado": True,
        "resultados_admitidos": ["APROBADO", "COMPLETADO", "RECHAZADO", "EXCLUIDO"], "minimo_aprobados": 0,
    },
    fases=("Publicaciones", "Cierre"),
    bloques=(
        BloqueSpec(
            RECORRIDO, "recorrido", "Publicaciones",
            lambda ctx: {
                "coleccion": ctx.coleccion, "ingreso": "AL_ENVIAR", "envio": "CONJUNTO",
                "etapa_borradores": "Lluvia de ideas",
                "columnas_requeridas_ingreso": COLUMNAS_IDEA_IMPERANDINA,
                "incorporar_nuevos": False, "espera_alcance_cerrado": True,
            },
        ),
        BloqueSpec(
            "Ajustar la idea", "tarea", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: tarea_de(
                ctx.p.planificador,
                columnas_editables=COLUMNAS_IDEA_IMPERANDINA, columnas_requeridas=COLUMNAS_IDEA_IMPERANDINA,
            ),
            descripcion="Corrige la idea según la observación de Andrés y reenvíala a aprobación.",
        ),
        BloqueSpec(
            "Aprobación de la idea", "aprobacion", "Publicaciones", contenedor=RECORRIDO, entra_al_enviar=True,
            configuracion=lambda ctx: {**aprobador(ctx.p.aprobador), "permite_exclusion": True},
        ),
        BloqueSpec(
            "Producción", "tarea", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: tarea_de(
                ctx.p.productor, columnas_editables=[ARCHIVO_PIEZA], columnas_requeridas=[ARCHIVO_PIEZA],
            ),
            descripcion="Produce la pieza gráfica o audiovisual y adjúntala en «Archivo de la pieza».",
        ),
        BloqueSpec(
            "Aprobación de la pieza", "aprobacion", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: {**aprobador(ctx.p.aprobador), "permite_exclusion": True},
        ),
        BloqueSpec(
            "Revisión de contenidos", "gaceta_item", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: {"gaceta": ctx.gaceta},
        ),
        BloqueSpec(
            "Publicación", "tarea", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: tarea_de(
                ctx.p.responsable_publicacion,
                fecha_prevista_campo=f"item.{FECHA_PUBLICACION}", fecha_limite_campo=f"item.{FECHA_PUBLICACION}",
                verificacion=[{"texto": "Publicada en el medio indicado", "obligatorio": True}],
            ),
            descripcion="Publica la pieza en su medio, en la fecha de publicación.",
        ),
        BloqueSpec(
            "Evidencia de publicación", "informacion", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: {
                "tipo_actor": "EQUIPO", "equipo_id": ctx.p.equipo_operativo.pk, "requiere_evidencia": True,
                "plazo_limite": {"cantidad": 1, "unidad": "DIAS"}, "plazo_habiles": True,
            },
            descripcion="Cualquier integrante de Mercadeo BPO adjunta la captura o el enlace de la publicación.",
        ),
        BloqueSpec(
            "Cerrar la parrilla", "tarea", "Cierre",
            lambda ctx: tarea_de(ctx.p.planificador, verificacion=[
                {"texto": "Revisé el resultado de todas las publicaciones", "obligatorio": True},
                {"texto": "Todos los recorridos finalizaron", "obligatorio": True},
                {"texto": "La gaceta está resuelta y no quedan correcciones abiertas", "obligatorio": True},
            ]),
            descripcion="Revisa el resultado de cada publicación, resuelve la gaceta y resuelve el ticket.",
        ),
    ),
    rutas=(
        RutaSpec("Aprobación de la idea", "Ajustar la idea", resultado="DEVUELTA"),
        RutaSpec("Aprobación de la pieza", "Producción", resultado="DEVUELTA"),
    ),
    programado=True,
    categoria="Mercadeo",
    nombre_exacto=True,
    personas=(("planificador", "lau"), ("aprobador", "amasso"), ("productor", "emm"), ("responsable_publicacion", "emm")),
    equipos=(
        EquipoSpec("Mercadeo BPO", "Equipo operativo de contenidos de Imperandina.", ("lau", "amasso", "emm"),
                   "equipo_operativo"),
        EquipoSpec("Mercadeo Imperandina", "Equipo interesado que revisa los contenidos.", ("rbuitrago", "scortes"),
                   "equipo_revisor"),
    ),
    calendario=(28, "MES_SIGUIENTE"),
    ideas=(
        IdeaSpec(5, "carrusel", "instagram",
                 "Tu techo también merece protección: conoce los beneficios de impermeabilizar a tiempo con Imperandina.",
                 "Carrusel de 5 láminas: qué es la impermeabilización, cómo evita filtraciones y humedad, ahorro en "
                 "reparaciones, durabilidad de la cubierta y llamado a cotizar con Imperandina."),
        IdeaSpec(10, "reel", "facebook",
                 "Antes de que llegue la lluvia, revisa tu cubierta. Te damos 3 consejos para protegerla.",
                 "Reel de 30 segundos: limpieza de canales y bajantes, revisión de fisuras y aplicación de un "
                 "impermeabilizante Imperandina, con cierre de marca."),
        IdeaSpec(15, "post", "linkedin",
                 "Soluciones de impermeabilización para proyectos industriales que no se pueden detener.",
                 "Post con foto de una bodega o planta industrial: sistemas de impermeabilización de alto "
                 "desempeño, asesoría técnica en obra y casos de éxito de Imperandina."),
        IdeaSpec(20, "historia", "instagram",
                 "¿Cada cuánto debes revisar tu impermeabilización? Te lo contamos.",
                 "Historia en 3 pantallas con recomendaciones de mantenimiento: inspección semestral, limpieza de "
                 "la superficie y retoques a tiempo; sticker de preguntas para la comunidad."),
        IdeaSpec(24, "post", "facebook",
                 "Gracias por confiar en Imperandina este año. ¡Felices fiestas y un próspero año nuevo!",
                 "Post de fin de año con la imagen de marca, mensaje de agradecimiento a clientes y aliados, y "
                 "deseos para el nuevo año."),
    ),
)


CONTRATOS = {
    c.codigo: c for c in (PARRILLA_CASO, WEBINAR_CASO, PIEZA_CASO, PRESENTACIONES_CASO, IMPERANDINA_CASO)
}


@dataclass
class Contexto:
    """Lo que la configuración de un caso necesita resolver: personas/equipos elegidos y claves creadas."""

    p: object  # Participantes
    coleccion: str = ""
    gaceta: str = ""
