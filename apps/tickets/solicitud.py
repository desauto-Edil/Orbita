"""Experiencia de solicitud (Fase visual V2) — capa de PRESENTACIÓN.

Descubrir → completar → previsualizar → revisar → enviar → confirmar.

Este módulo NO es un segundo motor de formularios y NO guarda nada propio:
solo traduce lo que ya existe (`FormularioVersion` congelada del ticket,
`Campo`/`OpcionCampo`/`ReglaCondicional`, `RespuestaCampo`) a lo que necesitan
las plantillas, y reutiliza tal cual:

- `EstrategiaCampo.validar_valor` (`apps/catalogo/campos.py`) para validar un
  valor enviado campo por campo;
- `calcular_estados_efectivos` (`apps/tickets/validaciones.py`) para saber qué
  campos están visibles/son requeridos — la misma función que usan
  `guardar_respuestas_borrador` y `radicar_ticket`. El navegador NUNCA evalúa
  reglas: consulta al servidor (`estados_en_vivo`) y aplica lo que éste diga;
- `operaciones.crear_borrador` / `guardar_respuestas_borrador` /
  `radicar_ticket` para toda escritura (la radicación real no se duplica).

La autoridad siempre es el servidor: lo que muestre el navegador mientras el
usuario escribe es una ayuda; al guardar/revisar/enviar se recalcula todo.
"""

from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.template.defaultfilters import filesizeformat
from django.urls import reverse
from django.utils import formats, timezone

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import Campo, ReglaCondicional
from apps.catalogo.visibilidad import servicios_visibles_para
from apps.tickets import operaciones
from apps.tickets.models import Ticket

# Se reutilizan (no se reescriben) los dos criterios que ya definen qué es un
# valor "vacío" y qué es un archivo técnicamente aceptable: una sola fuente.
from apps.tickets.operaciones import _es_valor_vacio, _validar_archivo_tecnico
from apps.tickets.validaciones import calcular_estados_efectivos

TipoCampo = Campo.TipoCampo

# Umbrales de representación visual (no son reglas de negocio): cuántas
# opciones caben cómodamente como tarjetas/chips antes de pasar a un selector.
MAX_OPCIONES_TARJETA = 4
MAX_OPCIONES_CHIPS = 8

CONTROL_POR_TIPO = {
    TipoCampo.TEXTO: "texto",
    TipoCampo.TEXTO_LARGO: "area",
    TipoCampo.CORREO: "email",
    TipoCampo.URL: "url",
    TipoCampo.NUMERO: "numero",
    TipoCampo.FECHA: "fecha",
    TipoCampo.FECHA_HORA: "fecha_hora",
    TipoCampo.BOOLEANO: "switch",
    TipoCampo.ARCHIVO: "archivo",
    TipoCampo.USUARIO: "select",
    TipoCampo.AREA: "select",
    TipoCampo.UNIDAD: "select",
}

INPUT_TYPE_POR_CONTROL = {
    "texto": "text",
    "email": "email",
    "url": "url",
    "numero": "number",
    "fecha": "date",
    "fecha_hora": "datetime-local",
}

# Controles cortos o compactos que pueden compartir fila de dos columnas. La
# decisión sale solo del tipo de campo (nunca del nombre/significado de la
# etiqueta).
CONTROLES_ANCHO_MEDIO = {"texto", "email", "url", "numero", "fecha", "fecha_hora", "select", "switch"}


# Columnas de las tarjetas de selección única: 4 opciones se leen mejor en 2×2.
COLUMNAS_TARJETAS = {1: 1, 2: 2, 3: 3, 4: 2}

# Controles cuyo valor el navegador sabe validar (rango, paso, formato) a partir
# de los propios atributos HTML: se les reserva un aviso en vivo, no bloqueante.
CONTROLES_CON_AVISO_VIVO = {"numero", "fecha", "fecha_hora", "email", "url"}


def control_para(campo, cantidad_opciones):
    if campo.tipo == TipoCampo.LISTA:
        return "tarjetas" if 1 <= cantidad_opciones <= MAX_OPCIONES_TARJETA else "select"
    if campo.tipo == TipoCampo.MULTILISTA:
        return "chips" if 1 <= cantidad_opciones <= MAX_OPCIONES_CHIPS else "multiselect"
    return CONTROL_POR_TIPO[campo.tipo]


# ---------------------------------------------------------------------------
# Entrada: servicio solicitable y borrador
# ---------------------------------------------------------------------------


def version_activa_de(servicio):
    formulario = servicio.formulario
    return formulario.version_activa if formulario is not None else None


def obtener_o_crear_borrador(usuario, servicio):
    """Abre la solicitud de un Servicio/Proceso sin dejar basura al navegar.

    Entrar a una solicitud es un GET idempotente: si el usuario ya tiene un
    borrador de este servicio, sobre la versión activa actual, que todavía no
    tiene ninguna respuesta guardada, se retoma ese mismo; si no, se crea uno
    con `crear_borrador` (que valida visibilidad y formulario activo). Un
    borrador con respuestas guardadas nunca se reutiliza: pertenece a otra
    solicitud en curso.
    """
    if not servicios_visibles_para(usuario).filter(pk=servicio.pk).exists():
        raise PermissionDenied("El servicio no está activo o no es visible para este usuario.")

    version = version_activa_de(servicio)
    if version is not None:
        vacio = (
            Ticket.objects.filter(
                solicitante=usuario,
                estado=Ticket.Estado.BORRADOR,
                detalle_servicio__servicio=servicio,
                detalle_servicio__formulario_version=version,
                respuesta_formulario__respuestas_campo__isnull=True,
            )
            .order_by("-creado_en")
            .first()
        )
        if vacio is not None:
            return vacio
    return operaciones.crear_borrador(usuario, servicio)


# ---------------------------------------------------------------------------
# Estado efectivo (visible/requerido) — delegado al servidor
# ---------------------------------------------------------------------------


def _existentes(respuesta_formulario):
    return {
        rc.campo_id: rc
        for rc in respuesta_formulario.respuestas_campo.select_related("campo", "archivo").all()
    }


def _valores_para_evaluar(existentes, respuestas_crudas):
    """Mismo criterio que `guardar_respuestas_borrador`: lo guardado + lo
    que llega en este envío."""
    valores = {campo_id: rc.valor for campo_id, rc in existentes.items()}
    valores.update(respuestas_crudas or {})
    return valores


def estados_en_vivo(ticket, respuestas_crudas):
    """`{campo_id: {"visible": bool, "requerido": bool}}` para el envío
    actual (sin guardar nada)."""
    respuesta_formulario = ticket.respuesta_formulario
    version = respuesta_formulario.formulario_version
    valores = _valores_para_evaluar(_existentes(respuesta_formulario), respuestas_crudas)
    estados = calcular_estados_efectivos(version, valores)
    return {
        campo_id: {"visible": estado.visible, "requerido": estado.requerido}
        for campo_id, estado in estados.items()
    }


# ---------------------------------------------------------------------------
# Validación por campo (para mostrar el error junto al campo)
# ---------------------------------------------------------------------------


def errores_de_formato(ticket, respuestas_crudas):
    """`{campo_id: [mensajes]}` con los valores ENVIADOS que no pasan la
    validación de su tipo. Solo valida campos visibles y no vacíos (un vacío
    borra la respuesta; la obligatoriedad se exige al revisar/radicar). No
    reimplementa nada: delega en `EstrategiaCampo.validar_valor` y en el
    control técnico de archivos que usa `guardar_respuestas_borrador`.
    """
    respuesta_formulario = ticket.respuesta_formulario
    version = respuesta_formulario.formulario_version
    campos = {c.id: c for c in version.campos.prefetch_related("opciones")}
    valores = _valores_para_evaluar(_existentes(respuesta_formulario), respuestas_crudas)
    estados = calcular_estados_efectivos(version, valores)

    errores = {}
    for campo_id, crudo in respuestas_crudas.items():
        campo = campos.get(campo_id)
        if campo is None or not estados[campo_id].visible or _es_valor_vacio(crudo):
            continue
        estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
        try:
            if campo.tipo == TipoCampo.ARCHIVO:
                _validar_archivo_tecnico(crudo)
                estrategia.validar_valor(
                    campo, {"nombre": crudo.name, "tamano_mb": crudo.size / (1024 * 1024)}
                )
            else:
                estrategia.validar_valor(campo, crudo)
        except ValidationError as exc:
            errores[campo_id] = exc.messages
    return errores


# ---------------------------------------------------------------------------
# Construcción de lo que ve el usuario
# ---------------------------------------------------------------------------


def _formatear_numero(valor):
    if valor is None:
        return ""
    return format(Decimal(valor).normalize(), "f")


def _valores_del_campo(campo, rc, hay_envio, crudo):
    """`(valor, seleccion, marcado)` — qué mostrar en el control: el valor
    enviado (re-render con errores) o el guardado."""
    tipo = campo.tipo
    if hay_envio:
        if tipo == TipoCampo.MULTILISTA:
            return "", [str(v) for v in (crudo or [])], False
        if tipo == TipoCampo.BOOLEANO:
            return "", [], bool(crudo)
        return ("" if crudo is None else str(crudo)), [], False
    if rc is None:
        return "", [], False
    if tipo == TipoCampo.MULTILISTA:
        return "", [str(v) for v in (rc.valor_json or [])], False
    if tipo == TipoCampo.BOOLEANO:
        return "", [], bool(rc.valor_booleano)
    if tipo == TipoCampo.NUMERO:
        return _formatear_numero(rc.valor_numero), [], False
    if tipo == TipoCampo.FECHA:
        return (rc.valor_fecha.isoformat() if rc.valor_fecha else ""), [], False
    if tipo == TipoCampo.FECHA_HORA:
        momento = rc.valor_fecha_hora
        return (timezone.localtime(momento).strftime("%Y-%m-%dT%H:%M") if momento else ""), [], False
    if tipo in (TipoCampo.USUARIO, TipoCampo.AREA, TipoCampo.UNIDAD):
        pk = rc.valor
        return (str(pk) if pk is not None else ""), [], False
    return (rc.valor_texto or ""), [], False


def _archivo_de(rc):
    if rc is None:
        return None
    archivo = getattr(rc, "archivo", None)
    if archivo is None:
        return None
    return {
        "nombre": archivo.nombre_original,
        "tamano": filesizeformat(archivo.tamano_bytes),
        "url": reverse("tickets:descargar_archivo", args=[archivo.pk]),
    }


def _numero(valor):
    """Texto sin localizar ("1.5", no "1,5"): sirve para atributos HTML y pistas."""
    return format(Decimal(str(valor)).normalize(), "f")


def _limite_temporal(valor, con_hora):
    """`(atributo_html, fecha)` de un límite configurado, o None. Para FECHA_HORA
    se replica la lectura del dominio (`EstrategiaTemporal._parsear`): una fecha
    sin hora equivale a las 00:00 de ese día."""
    if valor in (None, ""):
        return None
    try:
        momento = datetime.fromisoformat(str(valor))
    except ValueError:
        return None
    if con_hora:
        return momento.strftime("%Y-%m-%dT%H:%M"), momento.date()
    return momento.date().isoformat(), momento.date()


def _pista_de_rango(minimo, maximo, unidad_min, unidad_max, entre):
    if minimo is not None and maximo is not None:
        return entre
    if minimo is not None:
        return unidad_min
    if maximo is not None:
        return unidad_max
    return ""


def _atributos_y_pista(campo):
    """Atributos HTML de ayuda y una pista corta con las restricciones REALES
    del campo (`Campo.configuracion`). No validan: el servidor es la autoridad;
    el navegador solo los usa para orientar."""
    configuracion = campo.configuracion or {}
    tipo = campo.tipo
    atributos, pistas = {}, []

    if tipo in (TipoCampo.TEXTO, TipoCampo.CORREO, TipoCampo.URL, TipoCampo.TEXTO_LARGO):
        if configuracion.get("placeholder"):
            atributos["placeholder"] = configuracion["placeholder"]
        limite = configuracion.get("longitud_maxima")
        if isinstance(limite, int) and not isinstance(limite, bool):
            # En un textarea el navegador cuenta el salto de línea como 1 y el
            # servidor como 2 (CRLF): no se usa `maxlength`, se muestra un
            # contador con la cuenta del servidor.
            atributos["limite" if tipo == TipoCampo.TEXTO_LARGO else "maxlength"] = limite
    elif tipo == TipoCampo.NUMERO:
        decimales = bool(configuracion.get("permite_decimales"))
        atributos["step"] = "any" if decimales else "1"
        atributos["inputmode"] = "decimal" if decimales else "numeric"
        minimo, maximo = configuracion.get("minimo"), configuracion.get("maximo")
        minimo = minimo if isinstance(minimo, (int, float)) and not isinstance(minimo, bool) else None
        maximo = maximo if isinstance(maximo, (int, float)) and not isinstance(maximo, bool) else None
        if minimo is not None:
            atributos["min"] = _numero(minimo)
        if maximo is not None:
            atributos["max"] = _numero(maximo)
        rango = _pista_de_rango(
            minimo, maximo,
            f"Mínimo {_numero(minimo)}" if minimo is not None else "",
            f"Máximo {_numero(maximo)}" if maximo is not None else "",
            f"Entre {_numero(minimo)} y {_numero(maximo)}" if minimo is not None and maximo is not None else "",
        )
        if rango:
            pistas.append(rango)
        if not decimales:
            pistas.append("Sin decimales")
    elif tipo in (TipoCampo.FECHA, TipoCampo.FECHA_HORA):
        con_hora = tipo == TipoCampo.FECHA_HORA
        minima = _limite_temporal(configuracion.get("fecha_minima"), con_hora)
        maxima = _limite_temporal(configuracion.get("fecha_maxima"), con_hora)
        if minima:
            atributos["min"] = minima[0]
        if maxima:
            atributos["max"] = maxima[0]

        def legible(limite):
            return formats.date_format(limite[1], "DATE_FORMAT")

        rango = _pista_de_rango(
            minima, maxima,
            f"Desde el {legible(minima)}" if minima else "",
            f"Hasta el {legible(maxima)}" if maxima else "",
            f"Entre el {legible(minima)} y el {legible(maxima)}" if minima and maxima else "",
        )
        if rango:
            pistas.append(rango)
    elif tipo == TipoCampo.ARCHIVO:
        extensiones = configuracion.get("extensiones_permitidas") or []
        if extensiones:
            atributos["accept"] = ",".join("." + e.lstrip(".").lower() for e in extensiones)
            atributos["extensiones"] = ", ".join(e.lstrip(".").upper() for e in extensiones)
        if configuracion.get("tamano_maximo_mb"):
            atributos["tamano_maximo"] = f"{_numero(configuracion['tamano_maximo_mb'])} MB"
    elif tipo == TipoCampo.MULTILISTA:
        minimo = configuracion.get("minimo_selecciones")
        maximo = configuracion.get("maximo_selecciones")
        if minimo and maximo:
            pistas.append(f"Elige entre {minimo} y {maximo}")
        elif minimo:
            pistas.append(f"Elige al menos {minimo}")
        elif maximo:
            pistas.append(f"Elige hasta {maximo}")
    return atributos, " · ".join(pistas)


def _construir_items(version, existentes=None, *, errores=None, valores_envio=None):
    """Una entrada por campo de la versión congelada, en su orden, lista
    para renderizar (control, ancho, estado, valores, errores)."""
    errores = errores or {}
    valores_envio = valores_envio or {}
    existentes = existentes or {}
    campos = list(version.campos.prefetch_related("opciones").all())
    ids_archivo = {c.id for c in campos if c.tipo == TipoCampo.ARCHIVO}

    valores = _valores_para_evaluar(
        existentes, {k: v for k, v in valores_envio.items() if k not in ids_archivo}
    )
    estados = calcular_estados_efectivos(version, valores)
    origenes = set(
        ReglaCondicional.objects.filter(campo_origen__version=version).values_list("campo_origen_id", flat=True)
    )

    items = []
    for campo in campos:
        estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
        rc = existentes.get(campo.id)
        hay_envio = campo.id in valores_envio and campo.tipo != TipoCampo.ARCHIVO
        valor, seleccion, marcado = _valores_del_campo(campo, rc, hay_envio, valores_envio.get(campo.id))

        if campo.tipo in (TipoCampo.LISTA, TipoCampo.MULTILISTA):
            opciones = [{"valor": o.valor, "etiqueta": o.etiqueta} for o in campo.opciones.all()]
        elif hasattr(estrategia, "queryset"):
            # Mismo queryset que valida el servidor (activos, etc.); solo se
            # ordena para presentarlo.
            opciones = sorted(
                ({"valor": str(o.pk), "etiqueta": str(o)} for o in estrategia.queryset(campo)),
                key=lambda opcion: opcion["etiqueta"].casefold(),
            )
        else:
            opciones = []
        for opcion in opciones:
            opcion["seleccionada"] = (
                opcion["valor"] in seleccion
                if campo.tipo == TipoCampo.MULTILISTA
                else opcion["valor"] == valor
            )

        control = control_para(campo, len(opciones))
        estado = estados[campo.id]
        atributos, pista = _atributos_y_pista(campo)
        limite = atributos.get("limite")
        aviso_vivo = control in CONTROLES_CON_AVISO_VIVO
        describedby = " ".join(
            identificador
            for identificador, aplica in (
                (f"help_{campo.id}", bool(campo.ayuda)),
                (f"hint_{campo.id}", bool(pista)),
                (f"count_{campo.id}", bool(limite)),
                (f"live_{campo.id}", aviso_vivo),
                (f"err_{campo.id}", bool(errores.get(campo.id))),
            )
            if aplica
        )
        item = {
            "campo": campo,
            "id": campo.id,
            "control": control,
            "input_type": INPUT_TYPE_POR_CONTROL.get(control, ""),
            "ancho": "medio" if control in CONTROLES_ANCHO_MEDIO else "completo",
            "visible": estado.visible,
            "requerido": estado.requerido,
            "es_origen": campo.id in origenes,
            "opciones": opciones,
            "valor": valor,
            "seleccion": seleccion,
            "marcado": marcado,
            "archivo": _archivo_de(rc) if campo.tipo == TipoCampo.ARCHIVO else None,
            "atributos": atributos,
            "pista": pista,
            "limite": limite,
            "aviso_vivo": aviso_vivo,
            "columnas": COLUMNAS_TARJETAS.get(len(opciones), 2) if control == "tarjetas" else None,
            "describedby": describedby,
            "errores": errores.get(campo.id),
        }
        item["completado"] = (
            control != "switch" and item["visible"] and item["requerido"] and _completado(item)
        )
        items.append(item)
    return items


def construir_items(ticket, *, errores=None, valores_envio=None):
    respuesta_formulario = ticket.respuesta_formulario
    return _construir_items(
        respuesta_formulario.formulario_version,
        _existentes(respuesta_formulario),
        errores=errores,
        valores_envio=valores_envio,
    )


def construir_items_para_version(version):
    return _construir_items(version)


def _completado(item):
    control = item["control"]
    if control == "switch":
        return True  # False también es una respuesta válida y se guarda (ver `radicar`)
    if control == "archivo":
        return bool(item["archivo"])
    if control in ("chips", "multiselect"):
        return bool(item["seleccion"])
    return bool((item["valor"] or "").strip())


def calcular_progreso(items):
    """Solo campos REQUERIDOS y actualmente VISIBLES (según el servidor)."""
    requeridos = [i for i in items if i["visible"] and i["requerido"]]
    completados = sum(1 for i in requeridos if _completado(i))
    total = len(requeridos)
    return {
        "completados": completados,
        "total": total,
        "porcentaje": round(100 * completados / total) if total else 100,
    }


def _fila_resumen(item):
    campo, control = item["campo"], item["control"]
    etiqueta = campo.etiqueta
    if control in ("tarjetas", "select"):
        elegida = next((o["etiqueta"] for o in item["opciones"] if o["seleccionada"]), "")
        return {"etiqueta": etiqueta, "tipo": "texto", "texto": elegida} if elegida else None
    if control in ("chips", "multiselect"):
        elegidas = [o["etiqueta"] for o in item["opciones"] if o["seleccionada"]]
        return {"etiqueta": etiqueta, "tipo": "lista", "valores": elegidas} if elegidas else None
    if control == "switch":
        return {"etiqueta": etiqueta, "tipo": "texto", "texto": "Sí" if item["marcado"] else "No"}
    if control == "archivo":
        if not item["archivo"]:
            return None
        return {"etiqueta": etiqueta, "tipo": "lista", "valores": [item["archivo"]["nombre"]]}
    texto = (item["valor"] or "").strip()
    if not texto:
        return None
    try:
        if control == "fecha":
            texto = formats.date_format(date.fromisoformat(texto), "DATE_FORMAT")
        elif control == "fecha_hora":
            texto = formats.date_format(datetime.fromisoformat(texto), r"j \d\e F \d\e Y, H:i")
    except ValueError:
        pass  # valor enviado con formato inválido: se muestra tal cual junto a su error
    return {"etiqueta": etiqueta, "tipo": "largo" if control == "area" else "texto", "texto": texto}


def resumen_de(items):
    """Filas presentables (solo visibles y con respuesta) para la vista
    previa y la revisión. Nada de ids, códigos ni JSON."""
    filas = []
    for item in items:
        if not item["visible"]:
            continue
        fila = _fila_resumen(item)
        if fila is not None:
            filas.append(fila)
    return filas


def contexto_workspace(ticket, *, errores=None, valores_envio=None):
    respuesta_formulario = ticket.respuesta_formulario
    servicio = ticket.detalle_servicio.servicio
    items = construir_items(ticket, errores=errores, valores_envio=valores_envio)
    return {
        "ticket": ticket,
        "servicio": servicio,
        "items": items,
        "progreso": calcular_progreso(items),
        "resumen": resumen_de(items),
        "campos_con_error": [i for i in items if i["errores"] and i["visible"]],
        "tiene_respuestas_guardadas": respuesta_formulario.respuestas_campo.exists(),
        "servicio_activo": servicio.activo,
    }
