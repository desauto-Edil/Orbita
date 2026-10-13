"""Entregables de revisión de elementos — Sprint 4.G2C1 (configuración).

Un `DefinicionEntregable` de tipo ELEMENTOS controla el CONJUNTO de elementos de la estructura del
Proceso (`DefinicionParrilla`): qué columnas se revisan y quién revisa. Aquí vive solo lo que es
configuración MUTABLE del catálogo:

- las columnas se guardan por la `clave` ESTABLE del `Campo` (`ColumnaRevisadaDefinicion`), nunca por
  pk ni etiqueta, porque cada versión de la estructura clona los campos con otro pk;
- los revisores son personas o equipos reales (`RevisorDefinicion`).

Cada Ticket resuelve y COPIA esto al nacer (`apps.tickets.revision_elementos`): ninguna revisión
histórica depende de lo que aquí se cambie después.
"""

from django.core.exceptions import ValidationError

from apps.catalogo.models import (
    ColumnaRevisadaDefinicion,
    DefinicionEntregable,
    RevisorDefinicion,
    Servicio,
)
from apps.catalogo.parrilla import definicion_activa_de, definicion_de


def version_de_columnas(servicio):
    """La versión de columnas que sirve para ELEGIR claves: la ACTIVA (la que congelarán los Tickets
    nuevos); si todavía no hay, el borrador. `None` si el Proceso no usa elementos."""
    definicion = definicion_de(servicio)
    if definicion is None:
        return None
    formulario = definicion.formulario
    if formulario.version_activa is not None and formulario.version_activa.estado == "ACTIVA":
        return formulario.version_activa
    return formulario.versiones.filter(estado="BORRADOR").order_by("-numero").first()


def columnas_disponibles(servicio):
    """`[Campo]` que se pueden revisar: las columnas de la versión vigente, salvo ARCHIVO."""
    version = version_de_columnas(servicio)
    if version is None:
        return []
    return list(version.campos.exclude(tipo="ARCHIVO").order_by("orden", "id"))


def configuracion_de(definicion):
    """`(claves, usuarios, equipos)` configurados de un entregable ELEMENTOS, en orden estable."""
    claves = list(definicion.columnas_revisadas.order_by("clave").values_list("clave", flat=True))
    revisores = list(definicion.revisores.select_related("usuario", "equipo"))
    usuarios = sorted((r.usuario for r in revisores if r.usuario_id), key=lambda u: u.pk)
    equipos = sorted((r.equipo for r in revisores if r.equipo_id), key=lambda e: e.pk)
    return claves, usuarios, equipos


def datos_de_configuracion(definicion):
    """Forma serializable de la configuración, para auditoría y para detectar «sin cambios»."""
    claves, usuarios, equipos = configuracion_de(definicion)
    return {
        "columnas_revisadas": claves,
        "revisores_usuarios": [u.pk for u in usuarios],
        "revisores_equipos": [e.pk for e in equipos],
    }


def validar_configuracion(servicio, claves, usuarios, equipos):
    """Reglas de un entregable ELEMENTOS. Lanza `ValidationError`; devuelve las claves limpias."""
    if servicio.tipo != Servicio.Tipo.PROCESO or servicio.es_ticket_general:
        raise ValidationError("Solo un Proceso puede tener un entregable de revisión de elementos.")
    if definicion_activa_de(servicio) is None:
        raise ValidationError(
            "Este proceso no usa elementos estructurados: actívalos en la pestaña Formulario "
            "(«Elementos del proceso») antes de crear un entregable que los revise."
        )
    claves = list(dict.fromkeys(c for c in (claves or []) if c))
    if not claves:
        raise ValidationError("Elige al menos una columna que se revisará.")
    existentes = {c.clave for c in columnas_disponibles(servicio)}
    desconocidas = [c for c in claves if c not in existentes]
    if desconocidas:
        raise ValidationError(
            "Estas columnas no existen en la estructura de elementos (o no son revisables): "
            + ", ".join(desconocidas) + "."
        )
    usuarios, equipos = list(usuarios or []), list(equipos or [])
    if not usuarios and not equipos:
        raise ValidationError("Indica quién revisa: al menos una persona o un equipo.")
    if any(not u.is_active for u in usuarios) or any(not e.activo for e in equipos):
        raise ValidationError("Los revisores deben ser personas y equipos activos.")
    return claves


def guardar_configuracion(definicion, claves, usuarios, equipos):
    """Reemplaza la configuración de revisión de `definicion` (ya validada). Para un tipo distinto de
    ELEMENTOS la limpia. Es configuración: se reescribe; lo histórico vive en los Tickets."""
    es_conjunto = definicion.tipo == DefinicionEntregable.Tipo.ELEMENTOS
    claves = list(claves or []) if es_conjunto else []
    usuarios = list(usuarios or []) if es_conjunto else []
    equipos = list(equipos or []) if es_conjunto else []
    definicion.columnas_revisadas.exclude(clave__in=claves).delete()
    actuales = set(definicion.columnas_revisadas.values_list("clave", flat=True))
    for clave in claves:
        if clave not in actuales:
            ColumnaRevisadaDefinicion.objects.create(definicion=definicion, clave=clave)
    definicion.revisores.filter(usuario__isnull=False).exclude(usuario_id__in=[u.pk for u in usuarios]).delete()
    definicion.revisores.filter(equipo__isnull=False).exclude(equipo_id__in=[e.pk for e in equipos]).delete()
    con_usuario = set(definicion.revisores.filter(usuario__isnull=False).values_list("usuario_id", flat=True))
    con_equipo = set(definicion.revisores.filter(equipo__isnull=False).values_list("equipo_id", flat=True))
    for usuario in usuarios:
        if usuario.pk not in con_usuario:
            RevisorDefinicion.objects.create(definicion=definicion, usuario=usuario)
    for equipo in equipos:
        if equipo.pk not in con_equipo:
            RevisorDefinicion.objects.create(definicion=definicion, equipo=equipo)


def errores_de_conjuntos(servicio):
    """Por qué los entregables de revisión de elementos del Proceso no se podrían usar HOY (lista
    vacía = bien o no hay). Solo lectura; la comprueba `validar_publicacion` para que un Ticket
    nunca falle al nacer ni nazca con una revisión imposible."""
    errores = []
    definiciones = servicio.definiciones_entregables.filter(activo=True, tipo=DefinicionEntregable.Tipo.ELEMENTOS)
    for definicion in definiciones:
        try:
            claves, usuarios, equipos = configuracion_de(definicion)
            validar_configuracion(servicio, claves, usuarios, equipos)
        except ValidationError as exc:
            errores.append(f"Entregable «{definicion.nombre}»: {' '.join(exc.messages)}")
            continue
        version = servicio_version_activa(servicio)
        if version is not None:
            faltan = [c for c in claves if not version.campos.filter(clave=c).exists()]
            if faltan:
                errores.append(
                    f"Entregable «{definicion.nombre}»: la versión activa de la estructura ya no tiene las "
                    "columnas " + ", ".join(faltan) + "."
                )
    return errores


def servicio_version_activa(servicio):
    definicion = definicion_activa_de(servicio)
    if definicion is None:
        return None
    version = definicion.formulario.version_activa
    return version if version is not None and version.estado == "ACTIVA" else None


def errores_de_version_de_columnas(servicio, version):
    """Antes de ACTIVAR una versión de la estructura: ¿seguiría siendo válida cada columna revisada de
    los entregables vigentes? (Quitar una columna revisada dejaría Tickets nuevos sin poder nacer.)"""
    errores = []
    claves_nuevas = {c.clave: c.tipo for c in version.campos.all()}
    for definicion in servicio.definiciones_entregables.filter(activo=True, tipo=DefinicionEntregable.Tipo.ELEMENTOS):
        for clave in definicion.columnas_revisadas.values_list("clave", flat=True):
            if clave not in claves_nuevas:
                errores.append(f"El entregable «{definicion.nombre}» revisa la columna «{clave}», que esta versión quita.")
            elif claves_nuevas[clave] == "ARCHIVO":
                errores.append(f"El entregable «{definicion.nombre}» revisa la columna «{clave}», que no puede ser de tipo archivo.")
    return errores
