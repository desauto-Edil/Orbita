"""Experiencia del Proceso en el Studio — Sprint 4.G2E (solo lectura).

El Diseñador es UNO solo (cinco secciones: Básico · Formulario · Salida · Flujo · Publicar) y sirve igual a un
Servicio que a un Proceso. Lo que cambia es la PRESENTACIÓN: un Proceso explica en cada sección cómo se
relacionan sus piezas —datos iniciales, elementos, revisión por conjunto, tareas por elemento y flujo—
reutilizando lo que el dominio ya sabe. Este módulo no escribe nada, no valida nada nuevo ni decide qué es
publicable: compone datos de lectura para las pantallas y clasifica los errores que ya produce
`operaciones.validar_publicacion` según la sección donde se corrigen.

Qué lee (todo existente): `DefinicionParrilla` y sus columnas, `ConfiguracionTareasElemento`,
`DefinicionEntregable` de conjunto (`ColumnaRevisadaDefinicion`, `RevisorDefinicion`), la
`ConfiguracionEjecucionVersion` con sus `BloqueOperativo` y `ProgramacionProceso`.
"""

from collections import defaultdict

from apps.catalogo.models import BloqueOperativo, DefinicionEntregable, Servicio
from apps.catalogo.parrilla import definicion_de, errores_de_parrilla
from apps.catalogo.programacion import errores_de_compatibilidad, programacion_activa_de
from apps.catalogo.revision_elementos import configuracion_de as configuracion_de_conjunto
from apps.catalogo.revision_elementos import errores_de_conjuntos, version_de_columnas
from apps.catalogo.tareas_elemento import (
    DESTINOS,
    configuracion_activa_de,
    configuracion_de as configuracion_de_tareas,
    errores_de_tareas_elemento,
)

ELEMENTOS = DefinicionEntregable.Tipo.ELEMENTOS

# Sección donde se corrige cada clase de error: (clave, etiqueta, pestaña, uso del formulario).
SECCION_GENERAL = ("general", "Básico", "general", "")
SECCION_DATOS = ("entrada", "Formulario · datos de inicio", "entrada", "")
SECCION_ELEMENTOS = ("elementos", "Formulario · elementos del proceso", "entrada", "parrilla")
SECCION_SALIDA = ("salida", "Salida", "salida", "")
SECCION_FLUJO = ("ejecucion", "Flujo", "ejecucion", "")

_TIPOS_BLOQUE = {
    "ACTIVIDAD": "Actividad", "ENTREGABLE": "Entregable", "APROBACION": "Aprobación", "ESPERA": "Espera",
    "DECISION": "Decisión", "TAREAS_ELEMENTOS": "Tareas por elemento",
    "GRUPO_ACTIVIDADES": "Actividades en paralelo", "RECORRIDO_ITEMS": "Recorrido por ítem", "GACETA": "Gaceta",
}


def aplica(servicio):
    """La experiencia de Proceso solo aplica a un Proceso real (no al Servicio interno del Ticket General)."""
    return servicio.tipo == Servicio.Tipo.PROCESO and not servicio.es_ticket_general


def url_seccion(servicio_pk, pestana, uso=""):
    from django.urls import reverse

    base = f"{reverse('catalogo:studio', args=[servicio_pk])}?tab={pestana}"
    return f"{base}&uso={uso}" if uso else base


# --- Piezas ---------------------------------------------------------------------------------------


def _persona(usuario):
    return (usuario.get_full_name() or "").strip() or usuario.get_username()


def _conjuntos(servicio):
    return list(
        servicio.definiciones_entregables.filter(activo=True, tipo=ELEMENTOS).order_by("orden", "pk")
    )


def _etiquetas_de_claves(version):
    if version is None:
        return {}
    return {c.clave: c.etiqueta for c in version.campos.all()}


def usos_de_columnas(servicio):
    """`{clave de columna: [textos]}` — para qué se usa cada columna de los elementos: dato de la tarea que
    originan o columna que se revisa en un entregable de conjunto. Ayuda a no romper una pieza al cambiar
    otra."""
    usos = defaultdict(list)
    configuracion = configuracion_activa_de(servicio)
    if configuracion is not None:
        for atributo, nombre, *_ in DESTINOS:
            clave = getattr(configuracion, atributo)
            if clave:
                usos[clave].append(f"{nombre[0].upper()}{nombre[1:]} de la tarea")
    for definicion in _conjuntos(servicio):
        claves, _, _ = configuracion_de_conjunto(definicion)
        for clave in claves:
            usos[clave].append(f"Se revisa en «{definicion.nombre}»")
    return dict(usos)


def _version_inicial(servicio):
    formulario = servicio.formulario
    return formulario.version_activa if formulario is not None else None


def _contar(version):
    if version is None:
        return 0, 0
    campos = list(version.campos.all())
    return len(campos), sum(1 for c in campos if c.obligatorio)


def tarjetas_formulario(servicio, ev=None):
    """Dos tarjetas, una por cada formulario del Proceso (nunca se mezclan en un mismo editor):

    - DATOS DE INICIO: lo que se responde al crear el Ticket.
    - ELEMENTOS DEL PROCESO: la estructura repetible que evoluciona durante la ejecución, con su
      activación, columnas, obligatorias y la configuración de tareas por elemento."""
    inicial = _version_inicial(servicio)
    campos, obligatorios = _contar(inicial)
    formulario = servicio.formulario
    borrador_inicial = bool(formulario is not None and formulario.versiones.filter(estado="BORRADOR").exists())
    datos = {
        "version": inicial, "campos": campos, "obligatorios": obligatorios, "borrador": borrador_inicial,
        "programado": programacion_activa_de(servicio) is not None,
    }

    # Bloque 2: TODAS las colecciones activas (no solo la por defecto), con la misma lectura que la evaluación central.
    from apps.catalogo.evaluacion import piezas_formulario

    if ev is not None:  # la misma lectura de la evaluación de esta petición
        pieza_datos, colecciones = ev.datos_inicio, ev.colecciones
    else:
        pieza_datos, colecciones = piezas_formulario(servicio)
    datos["configurada"] = pieza_datos.configurada

    definicion = definicion_de(servicio)
    version = version_de_columnas(servicio) if definicion is not None else None
    activa = definicion.formulario.version_activa if definicion is not None else None
    columnas, obligatorias = _contar(version)
    elementos = {
        "definicion": definicion, "usa": definicion is not None and definicion.activa,
        "nombre": definicion.nombre_elemento if definicion is not None else "",
        "version": activa if activa is not None and activa.estado == "ACTIVA" else None,
        "columnas": columnas, "obligatorias": obligatorias,
        "borrador": bool(
            definicion is not None and definicion.formulario.versiones.filter(estado="BORRADOR").exists()
        ),
    }

    configuracion = configuracion_de_tareas(servicio) if definicion is not None else None
    etiquetas = _etiquetas_de_claves(version)
    tareas = {
        "configuracion": configuracion,
        "activa": configuracion is not None and configuracion.activa,
        "destinos": [
            {"nombre": nombre, "columna": etiquetas.get(getattr(configuracion, atributo), getattr(configuracion, atributo))}
            for atributo, nombre, *_ in DESTINOS
            if configuracion is not None and getattr(configuracion, atributo)
        ],
        "evidencia": bool(configuracion is not None and configuracion.requiere_evidencia),
    }
    revisiones = []
    for conjunto in _conjuntos(servicio):
        claves, usuarios, equipos = configuracion_de_conjunto(conjunto)
        revisiones.append({
            "definicion": conjunto,
            "columnas": [etiquetas.get(c, c) for c in claves],
            "revisores": [_persona(u) for u in usuarios] + [e.nombre for e in equipos],
        })
    return {
        "datos": datos, "elementos": elementos, "colecciones": colecciones, "tareas": tareas, "revisiones": revisiones,
    }


def organizar_salida(servicio, definiciones):
    """Separa las definiciones de salida de un Proceso en «entregables del proceso» y «entregables por
    conjunto de elementos» y explica de qué depende cada conjunto (columnas revisadas, quién revisa, cuándo
    se aprueba y su relación con las tareas por elemento)."""
    simples = [d for d in definiciones if d.tipo != ELEMENTOS]
    conjuntos = [d for d in definiciones if d.tipo == ELEMENTOS]
    definicion = definicion_de(servicio)
    version = version_de_columnas(servicio) if definicion is not None else None
    etiquetas = _etiquetas_de_claves(version)
    tareas_activas = configuracion_activa_de(servicio) is not None
    dependencias = []
    for conjunto in conjuntos:
        claves, usuarios, equipos = configuracion_de_conjunto(conjunto)
        columnas = ", ".join(f"«{etiquetas.get(c, c)}»" for c in claves) or "ninguna columna"
        revisores = ", ".join([_persona(u) for u in usuarios] + [e.nombre for e in equipos]) or "nadie configurado"
        dependencias.append({"titulo": conjunto.nombre, "lineas": [
            f"Revisa {len(claves)} columna{'s' if len(claves) != 1 else ''} de los elementos: {columnas}.",
            f"Quién revisa: {revisores}.",
            "Se aprueba cuando todos los elementos vigentes están aprobados y completos; si cambia una columna "
            "revisada, ese elemento vuelve a revisarse.",
            *(
                ["Es obligatorio: las tareas por elemento solo se generan cuando el conjunto está aprobado."]
                if conjunto.obligatorio and tareas_activas else []
            ),
        ]})
    if tareas_activas and not conjuntos:
        dependencias.append({"titulo": "Tareas por elemento", "lineas": [
            "Se generan sin revisión previa: ningún entregable de conjunto condiciona su generación."
        ]})
    return {
        "definiciones_proceso": simples,
        "definiciones_conjunto": conjuntos,
        "dependencias": dependencias,
        "hay_estructura": definicion is not None and definicion.activa,
    }


# --- Flujo: recorrido legible ---------------------------------------------------------------------------


def _espera_de(bloque, tareas_activas):
    if bloque.tipo == BloqueOperativo.Tipo.ACTIVIDAD:
        return "Espera a que se complete la actividad."
    if bloque.tipo == BloqueOperativo.Tipo.APROBACION:
        return "Espera la decisión de quien aprueba."
    if bloque.tipo == BloqueOperativo.Tipo.ENTREGABLE:
        definicion = bloque.definicion_entregable
        if definicion is not None and definicion.tipo == ELEMENTOS:
            return "Espera a que todos los elementos estén revisados y aprobados."
        return "Espera a que el entregable esté completo."
    if bloque.tipo == BloqueOperativo.Tipo.TAREAS_ELEMENTOS:
        return "Genera una tarea por elemento y espera a que todas se completen."
    if bloque.tipo == BloqueOperativo.Tipo.ESPERA:
        return "Espera programada."
    if bloque.tipo == BloqueOperativo.Tipo.GRUPO_ACTIVIDADES:
        return "Lanza sus actividades en paralelo y espera a que terminen todas."
    if bloque.tipo == BloqueOperativo.Tipo.RECORRIDO_ITEMS:
        return "Cada ítem hace su propio recorrido; espera a que no quede ninguno en curso."
    if bloque.tipo == BloqueOperativo.Tipo.GACETA:
        if (bloque.configuracion or {}).get("esperar", "RESOLUCION") == "RESOLUCION":
            return "Abre la gaceta y espera a que se resuelva."
        return "Abre la gaceta y continúa sin esperar."
    return "Se evalúa sola, sin esperar a nadie."


def _ruta(transicion):
    if transicion.resultado_aprobacion:
        cuando = f"Si es {transicion.get_resultado_aprobacion_display().lower()}"
    elif transicion.es_fallback:
        cuando = "En otro caso"
    elif transicion.variable:
        cuando = f"Si {transicion.variable} {transicion.get_operador_display().lower()} {transicion.valor}".strip()
    else:
        cuando = "Después"
    if transicion.finaliza:
        destino = "Finaliza el flujo"
    elif transicion.bloque_destino_id:
        destino = transicion.bloque_destino.nombre
    else:
        destino = "Sin conectar"
    return {"cuando": cuando, "destino": destino, "sin_conectar": destino == "Sin conectar"}


def recorrido_flujo(servicio, configuracion, resumen_de_bloque):
    """Lista ordenada de los bloques de la configuración con lo que un Proceso necesita ver de un vistazo:
    orden, tipo, responsable, entregable, qué espera y adónde va después. `resumen_de_bloque` es la función
    que ya usa el Studio para describir quién atiende cada bloque. Solo lectura."""
    if configuracion is None:
        return []
    tareas = configuracion_activa_de(servicio)
    filas = []
    bloques = (
        configuracion.bloques.select_related("fase", "definicion_entregable", "entregable_revisado")
        .prefetch_related("transiciones_salientes__bloque_destino")
        .order_by("fase__orden", "orden", "pk")
    )
    # Fase 2: cada bloque contenido aparece justo después de su contenedor.
    principales = [b for b in bloques if b.contenedor_id is None]
    por_contenedor = {}
    for b in bloques:
        if b.contenedor_id:
            por_contenedor.setdefault(b.contenedor_id, []).append(b)
    bloques = [x for b in principales for x in [b, *por_contenedor.get(b.pk, [])]]
    for numero, bloque in enumerate(bloques, start=1):
        rutas = [_ruta(t) for t in sorted(bloque.transiciones_salientes.all(), key=lambda t: (t.prioridad, t.pk))]
        nota = ""
        if bloque.tipo == BloqueOperativo.Tipo.TAREAS_ELEMENTOS:
            nota = (
                "Título, responsable y fecha límite salen de las columnas configuradas en «Elementos del proceso»."
                if tareas is not None else "Falta configurar las tareas por elemento."
            )
        elif bloque.tipo == BloqueOperativo.Tipo.APROBACION and bloque.entregable_revisado_id:
            nota = f"Revisa el entregable «{bloque.entregable_revisado.nombre}»."
        tipo = _TIPOS_BLOQUE.get(bloque.tipo, bloque.tipo)
        if bloque.contenedor_id:
            # Fase 2: un paso dentro de un recorrido o grupo se lee como parte de él.
            tipo = f"↳ {tipo} ({'para cada ítem' if bloque.rama == 'ITEM' else 'en paralelo'})"
        filas.append({
            "numero": numero, "fase": bloque.fase.nombre, "bloque": bloque,
            "tipo": tipo,
            "responsable": resumen_de_bloque(bloque),
            "espera": _espera_de(bloque, tareas is not None),
            "nota": nota, "rutas": rutas,
        })
    return filas


# --- Publicar: resumen y errores por sección ------------------------------------------------------------


def resumen_publicacion(servicio, configuracion_activa, configuracion_borrador, ev=None):
    """Tarjetas del resumen del Proceso en Publicar. Cada una dice si está lista, qué contiene y desde
    dónde se corrige. Informativas: lo que BLOQUEA la publicación sigue siendo `validar_publicacion`."""
    tarjetas_f = tarjetas_formulario(servicio, ev)
    programacion = programacion_activa_de(servicio)
    tarjetas = []

    if programacion is not None:
        responsable = programacion.responsable_inicial
        quien = ""
        if responsable is not None:
            quien = _persona(responsable.usuario) if responsable.usuario_id else getattr(responsable.equipo, "nombre", "")
        tarjetas.append({
            "titulo": "Modalidad de inicio", "estado": "ok", "seccion": SECCION_GENERAL,
            "lineas": [
                "Programado: se crea una ejecución automática "
                f"el día {programacion.dia_creacion} de cada mes ({programacion.get_periodo_display().lower()}).",
                *([f"Responsable inicial: {quien}."] if quien else []),
            ],
        })
    else:
        tarjetas.append({
            "titulo": "Modalidad de inicio", "estado": "ok", "seccion": SECCION_GENERAL,
            "lineas": ["Manual: una persona lo inicia desde el catálogo."],
        })

    responsables = list(servicio.responsables.filter(activo=True).select_related("usuario", "equipo"))
    lineas = [
        "Responsables: "
        + (
            ", ".join(
                _persona(r.usuario) if r.usuario_id else getattr(r.equipo, "nombre", "") for r in responsables
            ) + "."
            if responsables else "ninguno configurado (opcional)."
        )
    ]
    if programacion is None:
        accesos = servicio.visibilidad.filter(activo=True).count()
        restringido = servicio.alcance_visibilidad == Servicio.AlcanceVisibilidad.RESTRINGIDO
        lineas.append(
            f"Visibilidad: restringido ({accesos} acceso{'s' if accesos != 1 else ''})." if restringido
            else "Visibilidad: público interno."
        )
    tarjetas.append({
        "titulo": "Responsables y visibilidad", "seccion": SECCION_GENERAL,
        "estado": "ok" if responsables else "neutro", "lineas": lineas,
    })

    # Bloque 2: datos de inicio y colecciones salen de la MISMA evaluación que la navegación
    # (`apps.catalogo.evaluacion`). Una tarjeta solo «pide atención» por un bloqueo real de su sección (lo agrega
    # `studio._verificacion`); un borrador que se activará al publicar se informa, no se marca como error.
    if ev is None:
        from apps.catalogo import evaluacion

        ev = evaluacion.evaluar(servicio)
    datos = ev.datos_inicio
    if datos.configurada and datos.activa_valida:
        linea_datos = (
            f"Versión {datos.version.numero} activa · {datos.campos} campo{'s' if datos.campos != 1 else ''}"
            f" ({datos.obligatorios} obligatorio{'s' if datos.obligatorios != 1 else ''})."
        )
        estado_datos = "ok"
    elif datos.borrador:
        estado_datos, linea_datos = "ok", "Borrador de datos de inicio: entrará en vigor al publicar."
    else:
        estado_datos, linea_datos = "neutro", "Sin datos de inicio: el proceso no pide información al crearse."
    if datos.borrador and datos.activa_valida:
        linea_datos += " Hay cambios sin activar: entrarán en vigor al publicar."
    tarjetas.append({
        "titulo": "Datos de inicio", "seccion": SECCION_DATOS, "estado": estado_datos, "lineas": [linea_datos],
    })

    if ev.colecciones:
        lineas = []
        for coleccion in ev.colecciones:
            if coleccion.activa_valida:
                linea = (
                    f"«{coleccion.nombre}»: versión {coleccion.version.numero} activa · {coleccion.campos} "
                    f"columna{'s' if coleccion.campos != 1 else ''} ({coleccion.obligatorios} obligatoria"
                    f"{'s' if coleccion.obligatorios != 1 else ''})."
                )
            else:
                linea = f"«{coleccion.nombre}»: sin una versión activa de columnas."
            if coleccion.borrador:
                linea += " Borrador de columnas: entrará en vigor al publicar."
            lineas.append(linea)
        tarjetas.append({
            "titulo": "Elementos del proceso", "seccion": SECCION_ELEMENTOS, "estado": "ok", "lineas": lineas,
        })
    else:
        tarjetas.append({
            "titulo": "Elementos del proceso", "seccion": SECCION_ELEMENTOS, "estado": "neutro",
            "lineas": ["No usa colecciones de ítems (opcional)."],
        })

    simples = [d for d in servicio.definiciones_entregables.filter(activo=True).exclude(tipo=ELEMENTOS)]
    lineas = [f"{len(simples)} entregable{'s' if len(simples) != 1 else ''} del proceso."]
    for revision in tarjetas_f["revisiones"]:
        lineas.append(
            f"Revisión «{revision['definicion'].nombre}»: {len(revision['columnas'])} columna"
            f"{'s' if len(revision['columnas']) != 1 else ''}, revisan {', '.join(revision['revisores']) or 'nadie'}."
        )
    tarjetas.append({
        "titulo": "Entregables y revisiones", "seccion": SECCION_SALIDA,
        "estado": "ok" if simples or tarjetas_f["revisiones"] else "neutro",
        "lineas": lineas,
    })

    tareas = tarjetas_f["tareas"]
    if tareas["activa"]:
        tarjetas.append({
            "titulo": "Tareas por elemento", "seccion": SECCION_ELEMENTOS, "estado": "ok",
            "lineas": [
                "Cada elemento origina una tarea: " + ", ".join(f"{d['nombre']} ← «{d['columna']}»" for d in tareas["destinos"]) + ".",
                "Pide evidencia para completarse." if tareas["evidencia"] else "No pide evidencia.",
            ],
        })
    else:
        tarjetas.append({
            "titulo": "Tareas por elemento", "seccion": SECCION_ELEMENTOS, "estado": "neutro",
            "lineas": ["No genera tareas por elemento (opcional)."],
        })

    workflow = servicio.workflow
    config = configuracion_activa or configuracion_borrador
    lineas = []
    estado = "pendiente"
    if workflow is None:
        lineas.append("Sin flujo asociado: elige o crea uno en la sección Flujo.")
    else:
        lineas.append(f"Flujo «{workflow.nombre}».")
        if config is not None:
            por_tipo = defaultdict(int)
            for bloque in config.bloques.all():
                por_tipo[_TIPOS_BLOQUE.get(bloque.tipo, bloque.tipo)] += 1
            lineas.append(
                ("Configuración activa" if configuracion_activa is not None else "Borrador: entrará en vigor al publicar")
                + f" v{config.numero}: "
                + (", ".join(f"{n} {t.lower()}" for t, n in por_tipo.items()) or "sin bloques")
                + "."
            )
            # Bloque 2: un borrador no es un error; si no se pudiera activar, `_verificacion` añade su bloqueo.
            estado = "ok"
        else:
            lineas.append("Todavía sin configuración de ejecución.")
    tarjetas.append({"titulo": "Flujo asociado", "seccion": SECCION_FLUJO, "estado": estado, "lineas": lineas})

    # Avisos de coherencia que no bloquean (Fase 4.A: los da el detector de dependencias; las obligatorias se
    # muestran como pendientes de publicación, no aquí).
    from apps.catalogo import dependencias

    avisos = [d.mensaje for d in dependencias.detectar(servicio) if not d.bloquea]
    return {"tarjetas": tarjetas, "avisos": avisos}


def errores_por_seccion(servicio, mensajes, traducir=lambda m: m):
    """Agrupa los mensajes de `validar_publicacion` por la sección donde se corrigen. `mensajes` son los
    ORIGINALES del dominio (se reconocen por coincidencia exacta con los validadores de cada pieza);
    `traducir` es el traductor de lenguaje del Studio. Lo no reconocido va al Flujo, que es donde el
    dominio acumula las reglas de ejecución. Devuelve `[{"seccion", "etiqueta", "url", "errores"}]`."""
    clasificados = {}
    for mensaje in errores_de_parrilla(servicio):
        clasificados[mensaje] = SECCION_ELEMENTOS
    for mensaje in errores_de_tareas_elemento(servicio):
        clasificados[mensaje] = SECCION_ELEMENTOS
    for mensaje in errores_de_conjuntos(servicio):
        clasificados[mensaje] = SECCION_SALIDA
    # Fase 4.A: las gacetas se configuran en Salida (antes caían por defecto en Flujo).
    from apps.catalogo.gacetas import errores_de_gacetas

    for mensaje in errores_de_gacetas(servicio):
        clasificados[mensaje] = SECCION_SALIDA
    if programacion_activa_de(servicio) is not None:
        for mensaje in errores_de_compatibilidad(servicio):
            if "campos obligatorios" in mensaje:
                clasificados[mensaje] = SECCION_DATOS
            elif "entrega formal" in mensaje:
                clasificados[mensaje] = SECCION_SALIDA
            elif "bloque «" in mensaje:
                clasificados[mensaje] = SECCION_FLUJO
            else:
                clasificados[mensaje] = SECCION_GENERAL
    grupos = {}
    for mensaje in mensajes:
        seccion = clasificados.get(mensaje)
        if seccion is None:
            # Fase 4.A: los mensajes pueden venir de la publicación SIMULADA (tras activar borradores), así que
            # además de la coincidencia exacta se reconoce el prefijo de cada validador.
            if mensaje.startswith(("La estructura de elementos", "La colección «", "Tareas por elemento:", "Colección «")):
                seccion = SECCION_ELEMENTOS
            elif mensaje.startswith(("Gaceta «", "Entregable «")):
                seccion = SECCION_SALIDA
            elif mensaje.startswith(("Datos de inicio", "Formulario de inicio", "Los datos de inicio")):
                seccion = SECCION_DATOS
            elif mensaje.startswith(("Nadie puede", "Falta el responsable inicial", "El responsable inicial",
                                     "El usuario responsable inicial", "El equipo responsable inicial")):
                # Bloque 2: visibilidad y responsables se corrigen en Básico.
                seccion = SECCION_GENERAL
        if seccion is None:
            if "formulario con una versión ACTIVA" in mensaje or mensaje.startswith("Los datos de inicio"):
                seccion = SECCION_DATOS
            elif "prórroga" in mensaje.lower() or "Tipo de catálogo" in mensaje:
                seccion = SECCION_GENERAL
            else:
                seccion = SECCION_FLUJO
        texto = traducir(mensaje)
        if not texto:
            continue
        grupos.setdefault(seccion, []).append(texto)
    return [
        {"seccion": clave, "etiqueta": etiqueta, "url": url_seccion(servicio.pk, pestana, uso), "errores": errores}
        for (clave, etiqueta, pestana, uso), errores in grupos.items()
    ]
