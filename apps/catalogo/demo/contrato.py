"""Contrato de los cuatro casos de la demostración (`docs/demo/ORBITA_DEMO.md`, Parte C), como DATOS.

La preparación los construye con las operaciones de dominio y la verificación compara lo que existe contra
ellos. Las claves de campos y columnas son EXPLÍCITAS (no se dejan generar desde la etiqueta) porque las
actividades, la gaceta y las decisiones las referencian.

La configuración de cada actividad, aprobación o gaceta es una función de un `Contexto` (personas y equipos
elegidos por parámetro, claves de la colección y de la gaceta creadas), nunca ids fijos.

`VERSION_CONTRATO` se guarda en `RecursoDemo.version_contrato`: si cambia el contrato, la verificación lo avisa
(un caso ya preparado no se reconstruye solo).
"""

from dataclasses import dataclass, field, replace

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
    "imperandina": "IMPERANDINA", "desarrollo": "DESARROLLO_SOFTWARE",
    "solar": "SOLAR_ANDINA", "diseno": "DISENO_SOLUCIONES",
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
    variable: object = ""  # decisiones: texto, o función Contexto -> texto (p. ej. con la clave de un bloque creado)
    operador: str = ""
    valor: str = ""
    es_fallback: bool = False
    requiere: str = ""

    def reglas(self, ctx=None):
        reglas = {"finaliza": self.finaliza}
        if self.resultado:
            reglas["resultado_aprobacion"] = self.resultado
        if self.es_fallback:
            reglas["es_fallback"] = True
        if self.variable:
            variable = self.variable(ctx) if callable(self.variable) else self.variable
            reglas.update(variable=variable, operador=self.operador, valor=self.valor)
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
    gacetas_extra: tuple = ()  # GacetaSpec adicionales (sus claves quedan en `Contexto.gacetas` por nombre)
    finalizacion: dict = field(default_factory=dict)
    fases: tuple = ()
    bloques: tuple = ()
    rutas: tuple = ()
    programado: bool = False
    # Demo Imperandina: un caso puede fijar su categoría, su nombre exacto (sin el sufijo de la demo), sus
    # personas (`(atributo, correo o su parte local)`), sus equipos (`EquipoSpec`), su calendario y las ideas de su ticket de prueba.
    categoria: str = ""
    nombre_exacto: bool = False
    personas: tuple = ()
    equipos: tuple = ()
    calendario: tuple = ()  # (día de creación, periodo)
    ideas: tuple = ()  # IdeaSpec
    solicitante: str = ""  # correo (o su parte local) de quien radica el ticket de ejemplo si no se indica --solicitante


@dataclass(frozen=True)
class EquipoSpec:
    """Equipo EXISTENTE o que la demo crea si no existe (por nombre), con estos integrantes (correos o sus partes locales, que ya
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
            # Regla de oro: en una gaceta nadie aprueba; sin --revisores, comenta el aprobador de la parrilla.
            "revisores": [actor_de(r) for r in ctx.p.revisores] or [actor_de(("USUARIO", ctx.p.aprobador))],
            # Al cerrar el periodo queda aceptado lo que no tenga comentarios (5.3 lo extiende hasta la última pieza).
            "plazo_revision": {"cantidad": 5, "unidad": "DIAS", "habiles": True},
            "plazo_correccion": {"cantidad": 2, "unidad": "DIAS", "habiles": True},
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
EVIDENCIA_PUBLICACION = "evidencia_de_publicacion"
PUNTO_PUBLICADA = "publicada"
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
        # Lista de evidencias: la prueba de que se publicó (captura o enlace). Es la evidencia de «Publicación».
        CampoSpec(
            "Evidencia de publicación", EVIDENCIA_PUBLICACION, "CHECKLIST",
            configuracion={"extensiones_permitidas": ["png", "jpg", "jpeg", "pdf", "mp4"], "tamano_maximo_mb": 50},
            opciones=((PUNTO_PUBLICADA, "Publicada en el medio indicado"),),
            ayuda="Sube la captura o el enlace de la publicación.",
        ),
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
            "revisores": [actor_de(("EQUIPO", ctx.p.equipo_revisor))],  # comentan: nadie aprueba en una gaceta
            # Silencio positivo: al cerrar el periodo queda aceptado lo que no tenga comentarios pendientes.
            "plazo_revision": {"cantidad": 7, "unidad": "DIAS", "habiles": False},
            "plazo_correccion": {"cantidad": 2, "unidad": "DIAS", "habiles": True},
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
        # Después de la gaceta: cualquiera de Mercadeo BPO publica la pieza y sube la evidencia (sin «tomar» la
        # actividad); responde emm si no se hace a tiempo. La evidencia ES la lista «Evidencia de publicación».
        BloqueSpec(
            "Publicación", "tarea", "Publicaciones", contenedor=RECORRIDO,
            configuracion=lambda ctx: {
                "tipo_actor": "EQUIPO", "equipo_id": ctx.p.equipo_operativo.pk,
                "responde_usuario_id": ctx.p.responsable_publicacion.pk, "requiere_evidencia": True,
                "fecha_prevista_campo": f"item.{FECHA_PUBLICACION}", "fecha_limite_campo": f"item.{FECHA_PUBLICACION}",
            },
            descripcion="Publica la pieza en su medio, en la fecha de publicación, y sube la evidencia.",
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


# --- Caso 6 — Plan de trabajo — Desarrollo de software (Proceso MANUAL) -------------------------------------------
# Un usuario solicita un desarrollo con su información inicial; el responsable técnico revisa la solicitud y arma el
# PLAN (colección fija de actividades) y lo envía COMPLETO al aprobador, que aprueba cada actividad (devolver la
# regresa a «Ajustar la actividad»). Solo cuando TODO el plan está aprobado (la principal espera a que terminen todos
# los recorridos) el bloque GACETA «Revisión del plan» lo presenta al solicitante: comenta o, al vencer el periodo,
# silencio positivo; la gaceta se resuelve sola cuando nada queda pendiente. La ejecución SOLO empieza entonces: el
# segundo recorrido (ejecución por actividad) no existe hasta que el flujo llega a él. Cada actividad se ejecuta con
# el responsable y las fechas de SU ítem y su resultado se aprueba por separado (devolver la regresa a ejecución).
# Personas y equipo FIJOS en el contrato (como Imperandina): ylizz es el desarrollador (responsable técnico; Tecnología
# tiene uno solo), amasso aprueba cada actividad del plan ANTES de la gaceta, cada resultado y la validación general
# (no participa en la gaceta), scortes solicita y comenta el plan en la gaceta. El proceso lo atiende el equipo EXISTENTE «Desarrollo» (ylizz y amasso): el
# ticket llega a su Cola y ylizz lo toma. No entra en los casos por defecto: `--casos desarrollo`.

DESARROLLO = "DESARROLLO_SOFTWARE"
(ACT_NOMBRE, ACT_DESCRIPCION, ACT_TIPO, ACT_RESPONSABLE, ACT_FECHA_PREVISTA, ACT_FECHA_LIMITE, ACT_ENTREGABLE,
 ACT_EVIDENCIA) = (
    "nombre_de_la_actividad", "descripcion_de_la_actividad", "tipo_de_actividad", "responsable_de_la_actividad",
    "fecha_prevista", "fecha_limite", "entregable_esperado", "evidencia",
)
COLUMNAS_PLAN = [ACT_NOMBRE, ACT_DESCRIPCION, ACT_TIPO, ACT_RESPONSABLE, ACT_FECHA_PREVISTA, ACT_FECHA_LIMITE, ACT_ENTREGABLE]
RECORRIDO_PLAN = "Construcción y aprobación del plan"
REVISION_PLAN = "Revisión del plan"
RECORRIDO_EJECUCION = "Ejecución del plan"
ARCHIVOS_EVIDENCIA = {
    "extensiones_permitidas": ["pdf", "png", "jpg", "jpeg", "docx", "xlsx", "pptx", "zip", "txt", "md"],
    "tamano_maximo_mb": 50,
}

DESARROLLO_CASO = Caso(
    codigo=DESARROLLO,
    nombre="Plan de trabajo — Desarrollo de software",
    tipo="PROCESO",
    descripcion=(
        "Solicitud, evaluación, planeación, revisión del plan, ejecución por actividades y entrega de un desarrollo "
        "de software."
    ),
    instrucciones="Describe la necesidad, el problema que resuelve y las funcionalidades que esperas.",
    responsable="equipo_desarrollo",
    tiempo_objetivo=(90, "DIAS", True),
    entrega=("PERIODO_OBSERVACIONES", 5),
    formulario="Solicitud de desarrollo de software",
    campos=(
        CampoSpec("Nombre del desarrollo", "nombre_del_desarrollo", "TEXTO", obligatorio=True),
        CampoSpec("Área solicitante", "area_solicitante", "AREA", obligatorio=True, configuracion={"solo_activos": True}),
        CampoSpec("Necesidad", "necesidad", "TEXTO_LARGO", obligatorio=True),
        CampoSpec("Objetivo", "objetivo", "TEXTO_LARGO", obligatorio=True),
        CampoSpec("Problema que resuelve", "problema", "TEXTO_LARGO", obligatorio=True),
        CampoSpec("Funcionalidades solicitadas", "funcionalidades_solicitadas", "TEXTO_LARGO", obligatorio=True),
        CampoSpec("Usuarios destinatarios", "usuarios_destinatarios", "TEXTO_LARGO", obligatorio=True),
        CampoSpec(
            "Prioridad", "prioridad", "LISTA", obligatorio=True,
            opciones=(("alta", "Alta"), ("media", "Media"), ("baja", "Baja")),
        ),
        CampoSpec("Fecha requerida", "fecha_requerida", "FECHA", obligatorio=True, es_fecha_requerida=True),
        CampoSpec("Documentación de soporte", "documentacion", "ARCHIVO", configuracion=ARCHIVOS_REFERENCIA,
                  ayuda="Opcional: documentos, maquetas o referencias."),
    ),
    entregables=(EntregableSpec("Acta de entrega del desarrollo", "ARCHIVO", obligatorio=True),),
    coleccion="Actividades del plan de desarrollo",
    columnas=(
        CampoSpec("Nombre de la actividad", ACT_NOMBRE, "TEXTO", obligatorio=True),
        CampoSpec("Descripción", ACT_DESCRIPCION, "TEXTO_LARGO", obligatorio=True),
        CampoSpec(
            "Tipo de actividad", ACT_TIPO, "LISTA", obligatorio=True,
            opciones=(("analisis", "Análisis"), ("diseno", "Diseño"), ("desarrollo", "Desarrollo"),
                      ("pruebas", "Pruebas"), ("despliegue", "Despliegue"), ("documentacion", "Documentación")),
        ),
        CampoSpec("Responsable", ACT_RESPONSABLE, "USUARIO", obligatorio=True, configuracion={"solo_activos": True},
                  ayuda="Quien ejecuta la actividad cuando el plan se aprueba."),
        CampoSpec("Fecha prevista", ACT_FECHA_PREVISTA, "FECHA", obligatorio=True),
        CampoSpec("Fecha límite", ACT_FECHA_LIMITE, "FECHA", obligatorio=True),
        CampoSpec("Entregable esperado", ACT_ENTREGABLE, "TEXTO_LARGO", obligatorio=True),
        CampoSpec("Evidencia", ACT_EVIDENCIA, "ARCHIVO", configuracion=ARCHIVOS_EVIDENCIA,
                  ayuda="Opcional: la adjunta quien ejecuta la actividad."),
    ),
    gaceta=GacetaSpec(
        "Revisión del plan de trabajo",
        "Con el plan completo aprobado por el aprobador, el solicitante lo revisa en una ventana de 3 días hábiles: "
        "comenta lo que deba corregirse y lo que no tenga comentarios pendientes al vencer queda aceptado (silencio "
        "positivo; en la gaceta nadie aprueba). La ejecución empieza solo cuando la gaceta queda resuelta.",
        lambda ctx: {
            "colecciones": [ctx.coleccion],
            "condicion_ingreso": None,
            "ingreso": "MANUAL",  # el bloque «Revisión del plan» presenta TODO el plan aprobado de una vez
            "apertura": "POR_FLUJO",
            "exige_item_completo": True,
            "revisores": [actor_de(r) for r in ctx.p.revisores],  # el solicitante comenta: nadie aprueba
            # Silencio positivo al cerrar el periodo; una corrección que llega después queda aceptada al reenviarse.
            "plazo_revision": {"cantidad": 3, "unidad": "DIAS", "habiles": True},
            "plazo_correccion": {"cantidad": 3, "unidad": "DIAS", "habiles": True},
            "al_resolver_pendientes": "IMPEDIR",
            "resolucion_automatica": True,  # resuelta sola cuando nada queda pendiente: entonces empieza la ejecución
            "responsable_correccion_columna": "",  # la corrección vuelve al responsable del ticket
            "responsable_correccion_origen": "",
            "coordinador": persona(ctx.p.desarrollador),
            "obligatoria": True,
            "columnas_editables": COLUMNAS_PLAN,
            "columnas_visibles": COLUMNAS_PLAN,
        },
    ),
    finalizacion={
        "actividades_cerradas": True, "recorridos_terminados": True, "aprobaciones_resueltas": False,
        "gacetas_resueltas": True, "sin_correcciones": True, "alcance_cerrado": True, "items_con_resultado": True,
        "resultados_admitidos": ["APROBADO", "COMPLETADO", "RECHAZADO", "EXCLUIDO"], "minimo_aprobados": 0,
    },
    fases=("Evaluación", "Plan de trabajo", "Ejecución", "Validación y entrega"),
    bloques=(
        BloqueSpec(
            "Evaluación técnica", "tarea", "Evaluación",
            lambda ctx: tarea_del_responsable(verificacion=[
                {"texto": "Viabilidad técnica revisada", "obligatorio": True,
                 "pregunta": "¿Es viable? ¿Con qué tecnología o enfoque se haría?"},
                {"texto": "Alcance y esfuerzo estimados", "obligatorio": True,
                 "pregunta": "¿Qué incluye y qué no? ¿Cuánto tiempo y esfuerzo estimas?"},
                {"texto": "Riesgos y dependencias identificados", "obligatorio": True,
                 "pregunta": "¿Qué podría retrasarlo y de quién depende?"},
            ]),
            descripcion=(
                "Revisa la solicitud y concluye cada punto de la evaluación: viabilidad, alcance y esfuerzo, "
                "riesgos. Tus conclusiones son la base del plan de trabajo."
            ),
        ),
        BloqueSpec(
            RECORRIDO_PLAN, "recorrido", "Plan de trabajo",
            lambda ctx: {
                "coleccion": ctx.coleccion, "ingreso": "AL_ENVIAR", "envio": "CONJUNTO",
                "etapa_borradores": "Construcción del plan", "columnas_requeridas_ingreso": COLUMNAS_PLAN,
                "incorporar_nuevos": False, "espera_alcance_cerrado": True,
            },
            descripcion="El responsable técnico registra las actividades del plan y las envía todas juntas a aprobación.",
        ),
        BloqueSpec(
            "Ajustar la actividad", "tarea", "Plan de trabajo", contenedor=RECORRIDO_PLAN,
            configuracion=lambda ctx: tarea_de(
                ctx.p.desarrollador, columnas_editables=COLUMNAS_PLAN, columnas_requeridas=COLUMNAS_PLAN,
            ),
            descripcion="Corrige la actividad según la observación del aprobador y reenvíala a aprobación.",
        ),
        BloqueSpec(
            "Aprobación de la actividad", "aprobacion", "Plan de trabajo", contenedor=RECORRIDO_PLAN,
            entra_al_enviar=True, configuracion=lambda ctx: aprobador(ctx.p.aprobador),
        ),
        BloqueSpec(
            REVISION_PLAN, "gaceta", "Plan de trabajo",
            lambda ctx: {"gaceta": ctx.gaceta, "presentar_items": True, "esperar": "RESOLUCION"},
            descripcion=(
                "Con TODO el plan aprobado, el solicitante lo revisa en la gaceta; la ejecución empieza solo cuando "
                "la gaceta está resuelta."
            ),
        ),
        BloqueSpec(
            RECORRIDO_EJECUCION, "recorrido", "Ejecución",
            lambda ctx: {"coleccion": ctx.coleccion, "incorporar_nuevos": False, "espera_alcance_cerrado": False},
            descripcion="Cada actividad aprobada en el plan se ejecuta y se aprueba por separado.",
        ),
        BloqueSpec(
            "Ejecución de la actividad", "tarea", "Ejecución", contenedor=RECORRIDO_EJECUCION,
            configuracion=lambda ctx: {
                "tipo_actor": "COLUMNA", "columna": ACT_RESPONSABLE,
                "fecha_prevista_campo": f"item.{ACT_FECHA_PREVISTA}", "fecha_limite_campo": f"item.{ACT_FECHA_LIMITE}",
                "columnas_editables": [ACT_EVIDENCIA],
            },
            descripcion="Ejecuta la actividad del plan y, si aplica, adjunta su evidencia.",
        ),
        BloqueSpec(
            "Aprobación del resultado", "aprobacion", "Ejecución", contenedor=RECORRIDO_EJECUCION,
            configuracion=lambda ctx: aprobador(ctx.p.aprobador),
        ),
        BloqueSpec("Entrega del desarrollo", "resultado", "Validación y entrega", entregable="Acta de entrega del desarrollo",
                   descripcion="El responsable técnico adjunta el acta de entrega."),
        BloqueSpec("Validación general", "aprobacion", "Validación y entrega", lambda ctx: aprobador(ctx.p.aprobador),
                   revisa="Entrega del desarrollo", descripcion="Validación general del desarrollo antes de entregarlo."),
    ),
    rutas=(
        RutaSpec("Aprobación de la actividad", "Ajustar la actividad", resultado="DEVUELTA"),
        RutaSpec("Aprobación del resultado", "Ejecución de la actividad", resultado="DEVUELTA"),
        RutaSpec("Validación general", resultado="APROBADA", finaliza=True),
        RutaSpec("Validación general", "Entrega del desarrollo", resultado="DEVUELTA"),
        RutaSpec("Validación general", resultado="RECHAZADA", finaliza=True),
    ),
    personas=(
        ("desarrollador", "ylizz"), ("aprobador", "amasso"), ("revisores", "scortes"),
    ),
    equipos=(
        EquipoSpec("Desarrollo", "Equipo de desarrollo de software de Tecnología.", ("ylizz", "amasso"),
                   "equipo_desarrollo"),
    ),
    solicitante="scortes",
)


@dataclass(frozen=True)
class ActividadPlanSpec:
    """Una actividad del plan de ejemplo (`ejemplos --casos desarrollo`). Las fechas son días hábiles desde el
    día en que se arma el plan; el responsable es siempre el responsable técnico (Tecnología tiene uno)."""

    nombre: str
    tipo: str
    descripcion: str
    entregable: str
    inicio: int  # días hábiles hasta la fecha prevista
    duracion: int  # días hábiles de la fecha prevista a la límite


PLAN_ORBITA = (
    ActividadPlanSpec(
        "Levantamiento de requisitos", "analisis",
        "Entrevistas con las áreas y catálogo oficial de Casos de Uso (CU) de la plataforma.",
        "Catálogo de Casos de Uso aprobado y matriz de trazabilidad.", 3, 5),
    ActividadPlanSpec(
        "Arquitectura y modelo organizacional", "diseno",
        "Arquitectura Django + PostgreSQL, roles con alcance, auditoría append-only y organización sin jerarquía.",
        "Documento de arquitectura y modelo de datos base (Sprint 0).", 8, 5),
    ActividadPlanSpec(
        "Catálogo de servicios y formularios", "desarrollo",
        "Catálogo con visibilidad por alcance y constructor de formularios versionados.",
        "Catálogo y constructor de formularios publicados (Sprint 1).", 13, 10),
    ActividadPlanSpec(
        "Studio del diseñador", "desarrollo",
        "Studio guiado: Básico, Formulario, Salida, Flujo, Notificaciones y Publicar.",
        "Studio operativo con revisión y publicación.", 23, 10),
    ActividadPlanSpec(
        "Motor de workflows", "desarrollo",
        "Plantillas de fases, bloques, rutas, recorridos por ítem y actividades en paralelo.",
        "Motor de flujo con pruebas de recorridos y esperas.", 33, 10),
    ActividadPlanSpec(
        "Tickets y espacio de trabajo", "desarrollo",
        "Radicación, atención, asignación, conversación, historial y espacio unificado del ticket.",
        "Ciclo completo del ticket con espacio de trabajo.", 43, 10),
    ActividadPlanSpec(
        "Tareas y aprobaciones", "desarrollo",
        "Tareas con plazos, evidencia y verificación; aprobaciones con modos y devolución.",
        "Tareas y aprobaciones integradas al flujo.", 53, 5),
    ActividadPlanSpec(
        "Gacetas de revisión colaborativa", "desarrollo",
        "Revisión por ítem, observaciones, rondas, correcciones y resolución.",
        "Gacetas operando con versiones de los ítems.", 58, 7),
    ActividadPlanSpec(
        "Pruebas integrales", "pruebas",
        "Pruebas automatizadas y validación funcional con usuarios de las áreas.",
        "Informe de pruebas y lista de defectos resueltos.", 65, 7),
    ActividadPlanSpec(
        "Despliegue en producción", "despliegue",
        "Docker Compose, servidor ASGI, Caddy, Celery y respaldo de PostgreSQL.",
        "Plataforma desplegada con manual de operación.", 72, 5),
)


# --- Caso 7 — Parrilla mensual - Solar Andina - Demo (Proceso programado; como Imperandina) ----------------------
# Misma estructura, personas y equipos que Imperandina; cambian las columnas de la colección (nombre, copy,
# contenido, tipo y formato de la pieza, fecha y MEDIOS de publicación —varios, `MULTILISTA`— y responsable de
# publicación) y el calendario: la ejecución nace el día 2 del mes (MES_ACTUAL). Producción (emm) tiene como fecha
# límite el día 20 del mes (`fecha_limite_dia_del_periodo`). Los recordatorios del
# aprobador (amasso) salen tras UN día sin decidir (`notificaciones.0004`, regla global para todo aprobador).
# `--casos solar`.

SOLAR_ANDINA = "SOLAR_ANDINA"
NOMBRE_PIEZA, FORMATO_PIEZA, MEDIOS_PUBLICACION = "nombre_de_la_pieza", "formato_de_la_pieza", "medios_de_publicacion"
COLUMNAS_IDEA_SOLAR = [NOMBRE_PIEZA, COPY, CONTENIDO, TIPO_PIEZA, FORMATO_PIEZA, FECHA_PUBLICACION, MEDIOS_PUBLICACION]


def _bloques_solar():
    bloques = []
    for spec in IMPERANDINA_CASO.bloques:
        if spec.nombre == "Ajustar la idea":
            spec = replace(spec, configuracion=lambda ctx: tarea_de(
                ctx.p.planificador, columnas_editables=COLUMNAS_IDEA_SOLAR, columnas_requeridas=COLUMNAS_IDEA_SOLAR,
            ))
        elif spec.nombre == "Producción":
            spec = replace(spec, configuracion=lambda ctx: tarea_de(
                ctx.p.productor, columnas_editables=[ARCHIVO_PIEZA], columnas_requeridas=[ARCHIVO_PIEZA],
                fecha_limite_dia_del_periodo=20,
            ), descripcion="Produce la pieza y adjúntala en «Archivo de la pieza» (hasta el día 20 del mes).")
        elif spec.opcion == "recorrido":
            spec = replace(spec, configuracion=lambda ctx: {
                "coleccion": ctx.coleccion, "ingreso": "AL_ENVIAR", "envio": "CONJUNTO",
                "etapa_borradores": "Lluvia de ideas", "columnas_requeridas_ingreso": COLUMNAS_IDEA_SOLAR,
                "incorporar_nuevos": False, "espera_alcance_cerrado": True,
            })
        bloques.append(spec)
    return tuple(bloques)


def _gaceta_solar(ctx):
    cfg = IMPERANDINA_CASO.gaceta.configuracion(ctx)
    return {
        **cfg,
        "columnas_editables": [COPY, CONTENIDO, ARCHIVO_PIEZA],
        "columnas_visibles": [NOMBRE_PIEZA, COPY, TIPO_PIEZA, FORMATO_PIEZA, MEDIOS_PUBLICACION, CONTENIDO,
                              FECHA_PUBLICACION, ARCHIVO_PIEZA],
    }


SOLAR_ANDINA_CASO = replace(
    IMPERANDINA_CASO,
    codigo=SOLAR_ANDINA,
    nombre="Parrilla mensual - Solar Andina - Demo",
    descripcion="Proceso mensual para planificar, aprobar, producir, revisar y publicar los contenidos de Solar Andina.",
    columnas=(
        CampoSpec("Nombre", NOMBRE_PIEZA, "TEXTO", obligatorio=True, ayuda="Nombre corto de la pieza."),
        CampoSpec("Copy", COPY, "TEXTO_LARGO", obligatorio=True),
        CampoSpec("Contenido", CONTENIDO, "TEXTO_LARGO", obligatorio=True, ayuda="Descripción de lo que hay que producir."),
        CampoSpec(
            "Tipo de pieza", TIPO_PIEZA, "LISTA", obligatorio=True,
            opciones=(("blog", "Blog"), ("reel", "Reel"), ("post", "Post"), ("carrusel", "Carrusel")),
        ),
        CampoSpec(
            "Formato de la pieza", FORMATO_PIEZA, "LISTA", obligatorio=True,
            opciones=(("cuadrado", "Cuadrado 1:1"), ("vertical", "Vertical 4:5"), ("historia", "Vertical 9:16"),
                      ("horizontal", "Horizontal 16:9"), ("articulo", "Artículo web")),
        ),
        CampoSpec("Fecha de publicación", FECHA_PUBLICACION, "FECHA", obligatorio=True,
                  configuracion={"dentro_del_periodo": True}, ayuda="Debe estar dentro del mes de la parrilla."),
        CampoSpec(
            "Medios de publicación", MEDIOS_PUBLICACION, "MULTILISTA", obligatorio=True,
            opciones=(("instagram", "Instagram"), ("facebook", "Facebook"), ("linkedin", "LinkedIn"),
                      ("tiktok", "TikTok"), ("web", "Sitio web")),
            ayuda="Marca todos los medios en los que se publica.",
        ),
        CampoSpec("Responsable de publicación", RESPONSABLE_PUBLICACION, "USUARIO", configuracion={"solo_activos": True}),
        CampoSpec("Archivo de la pieza", ARCHIVO_PIEZA, "ARCHIVO", obligatorio=True, configuracion=ARCHIVOS_PIEZA,
                  ayuda="Lo adjunta Producción; se puede reemplazar si la pieza vuelve."),
        CampoSpec(
            "Evidencia de publicación", EVIDENCIA_PUBLICACION, "CHECKLIST",
            configuracion={"extensiones_permitidas": ["png", "jpg", "jpeg", "pdf", "mp4"], "tamano_maximo_mb": 50},
            opciones=((PUNTO_PUBLICADA, "Publicada en los medios indicados"),),
            ayuda="Sube la captura o el enlace de la publicación.",
        ),
    ),
    gaceta=GacetaSpec(
        "Revisión de contenidos - Solar Andina",
        "Mercadeo revisa cada pieza aprobada en una ventana compartida de 7 días; lo que no tenga observaciones al "
        "vencer queda aceptado (silencio positivo).",
        _gaceta_solar,
    ),
    bloques=_bloques_solar(),
    calendario=(2, "MES_ACTUAL"),
    ideas=(),  # sus ideas de prueba son `IDEAS_SOLAR` (otras columnas que Imperandina)
)


@dataclass(frozen=True)
class IdeaSolarSpec:
    dia: int
    nombre: str
    tipo: str
    formato: str
    medios: tuple
    copy: str
    contenido: str


IDEAS_SOLAR = (
    IdeaSolarSpec(5, "Paneles en casa", "carrusel", "vertical", ("instagram", "facebook"),
                  "Tu techo puede producir energía: así funciona un sistema solar residencial.",
                  "Carrusel de 5 láminas: cómo funciona un panel, ahorro en la factura, instalación, mantenimiento y cotización."),
    IdeaSolarSpec(9, "Ahorro real", "reel", "historia", ("instagram", "tiktok"),
                  "¿Cuánto ahorras con energía solar? Te lo mostramos en 30 segundos.",
                  "Reel con un cliente real mostrando su factura antes y después de instalar paneles Solar Andina."),
    IdeaSolarSpec(14, "Guía de energía solar para empresas", "blog", "articulo", ("web", "linkedin"),
                  "Guía práctica: cómo una empresa reduce costos con energía solar.",
                  "Artículo de blog de 1.200 palabras: beneficios tributarios, retorno de la inversión y casos de éxito."),
    IdeaSolarSpec(19, "Mantenimiento sencillo", "post", "cuadrado", ("facebook", "instagram"),
                  "Tres cuidados para que tus paneles rindan al máximo.",
                  "Post con ilustración: limpieza, revisión del inversor y monitoreo desde la app."),
    IdeaSolarSpec(26, "Cierre de mes", "post", "horizontal", ("linkedin",),
                  "Este mes instalamos energía limpia en nuevos hogares y empresas. ¡Gracias por confiar!",
                  "Post institucional con cifras del mes e imagen de un proyecto instalado."),
)


# --- Caso 8 — Diseño de nuevas soluciones (Proceso MANUAL; como Desarrollo de software) --------------------------
# El mismo plan de trabajo con DOS gacetas: la de siempre («Revisión del plan de trabajo», el solicitante comenta el
# plan aprobado antes de ejecutarlo) y una segunda, «Publicación a interesados», DURANTE la ejecución. Cuando el
# aprobador aprueba el RESULTADO de una actividad decide en esa misma decisión si se publica a los interesados y
# durante cuántos días (1 o 2; `catalogo.gacetas.publicacion_de`). Solo lo publicado entra a la segunda gaceta, con
# su propia ventana (`ParticipacionItemGaceta.vence_revision_en`; al vencer, silencio positivo de ese ítem). Lo no
# publicado termina su recorrido. `--casos diseno`.

DISENO = "DISENO_SOLUCIONES"
APROBACION_RESULTADO = "Aprobación del resultado"
DECISION_PUBLICAR = "¿Se publica a los interesados?"
PUBLICACION_INTERESADOS = "Publicación a interesados"


def _bloques_diseno():
    bloques = []
    for spec in DESARROLLO_CASO.bloques:
        if spec.nombre == APROBACION_RESULTADO:
            spec = replace(spec, configuracion=lambda ctx: {
                **aprobador(ctx.p.aprobador),
                "publicacion": {"gaceta": ctx.gacetas[PUBLICACION_INTERESADOS], "dias_maximos": 2},
            }, descripcion="Aprueba el resultado y decide si se publica a los interesados (1 o 2 días).")
            bloques.append(spec)
            bloques.append(BloqueSpec(
                DECISION_PUBLICAR, "decision", "Ejecución", contenedor=RECORRIDO_EJECUCION,
                descripcion="Si el aprobador lo publicó, va a los interesados; si no, la actividad termina.",
            ))
            bloques.append(BloqueSpec(
                PUBLICACION_INTERESADOS, "gaceta_item", "Ejecución", contenedor=RECORRIDO_EJECUCION,
                configuracion=lambda ctx: {
                    "gaceta": ctx.gacetas[PUBLICACION_INTERESADOS],
                    "dias_desde_aprobacion": ctx.bloques[APROBACION_RESULTADO],
                },
                descripcion="Los interesados comentan el resultado durante los días que eligió el aprobador.",
            ))
            continue
        bloques.append(spec)
    return tuple(bloques)


DISENO_CASO = replace(
    DESARROLLO_CASO,
    codigo=DISENO,
    nombre="Diseño de nuevas soluciones",
    nombre_exacto=True,
    descripcion=(
        "Solicitud, evaluación, plan de trabajo con revisión del solicitante, ejecución por actividades con "
        "publicación opcional a los interesados y entrega de una nueva solución."
    ),
    formulario="Solicitud de diseño de una nueva solución",
    gacetas_extra=(
        GacetaSpec(
            PUBLICACION_INTERESADOS,
            "Durante la ejecución, el aprobador decide qué resultados se publican a los interesados y por cuántos "
            "días (máximo 2). Los interesados comentan; lo que no tenga comentarios al vencer queda aceptado.",
            lambda ctx: {
                "colecciones": [ctx.coleccion],
                "condicion_ingreso": None,
                "ingreso": "MANUAL",  # lo presenta el paso del recorrido, solo si el aprobador lo publicó
                "apertura": "POR_FLUJO",
                "exige_item_completo": True,
                "revisores": [actor_de(r) for r in ctx.p.revisores],
                "plazo_revision": None,  # cada ítem tiene su ventana: la que eligió su aprobador
                "plazo_correccion": {"cantidad": 2, "unidad": "DIAS", "habiles": True},
                "al_resolver_pendientes": "IMPEDIR",
                "resolucion_automatica": False,
                "responsable_correccion_columna": ACT_RESPONSABLE,
                "responsable_correccion_origen": "",
                "coordinador": persona(ctx.p.desarrollador),
                "obligatoria": False,
                "columnas_editables": [ACT_ENTREGABLE, ACT_EVIDENCIA],
                "columnas_visibles": [ACT_NOMBRE, ACT_DESCRIPCION, ACT_ENTREGABLE, ACT_EVIDENCIA],
            },
        ),
    ),
    bloques=_bloques_diseno(),
    rutas=DESARROLLO_CASO.rutas + (
        RutaSpec(
            DECISION_PUBLICAR, PUBLICACION_INTERESADOS,
            variable=lambda ctx: f"aprobaciones.{ctx.bloques[APROBACION_RESULTADO]}.publicar",
            operador="IGUAL_A", valor="true",
        ),
        RutaSpec(DECISION_PUBLICAR, finaliza=True, es_fallback=True),
    ),
)


CONTRATOS = {
    c.codigo: c
    for c in (PARRILLA_CASO, WEBINAR_CASO, PIEZA_CASO, PRESENTACIONES_CASO, IMPERANDINA_CASO, DESARROLLO_CASO,
              SOLAR_ANDINA_CASO, DISENO_CASO)
}


@dataclass
class Contexto:
    """Lo que la configuración de un caso necesita resolver: personas/equipos elegidos y claves creadas."""

    p: object  # Participantes
    coleccion: str = ""
    gaceta: str = ""
    gacetas: dict = field(default_factory=dict)  # nombre de la gaceta -> clave
    bloques: dict = field(default_factory=dict)  # nombre del bloque ya creado -> clave estable
