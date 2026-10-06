"""Buscador por reglas "¿Qué necesitas?" (Sprint 4.D).

Una persona describe lo que necesita con sus palabras y Órbita le propone los
Servicios y Procesos que realmente puede solicitar. Es determinista y explicable:
sin IA, embeddings, servicios externos ni historial de búsquedas. El texto
escrito no se guarda en ningún lado (ni base de datos ni auditoría).

SEGURIDAD. El universo de candidatos parte SIEMPRE de `servicios_visibles_para`
(visibilidad, activo/publicado y exclusión del Servicio interno del Ticket
General) y agrega solo lo que hace útil la sugerencia: categoría activa y un
formulario con versión activa (si no, la solicitud no podría abrirse). Lo que la
persona no puede ver nunca se carga en memoria, así que ni su nombre ni sus
términos pueden filtrarse. El Ticket General no participa nunca del puntaje: es
una acción alterna que decide la vista (`apps.core.views.necesidad_view`).

ALGORITMO. Cada Servicio suma puntos por campo; todo se compara normalizado
(`apps.catalogo.normalizacion`: sin acentos/puntuación, sin palabras vacías,
con una raíz aproximada para plurales y derivaciones). Prioridad conceptual:

    término configurado > nombre > categoría > descripción / instrucciones

Cobertura = proporción de las raíces de un lado que aparecen en el otro. Campos
cortos (nombre) cuentan la mejor de las dos coberturas ("casi todo el nombre está
en la consulta" o "casi toda la consulta está en el nombre"); textos largos
(descripción, instrucciones) cuentan solo cuánto de la consulta explican.

Un Servicio aparece una sola vez: sus coincidencias en todos los campos se SUMAN
en un único resultado. Se sugiere solo con `puntuación >= UMBRAL_RELEVANCIA` y se
muestran los `MAX_RESULTADOS` mejores, con desempate estable (puntuación desc,
nombre asc, id asc). Para que el umbral signifique algo:

- una categoría, aunque coincida por completo, NO alcanza sola (solo refuerza),
  salvo que la persona escriba exactamente el nombre de la categoría;
- una descripción o unas instrucciones alcanzan solo si explican TODA la consulta.

Todos los pesos y el umbral viven aquí, juntos: ajustarlos no toca vistas.

RENDIMIENTO. V1 puntúa en Python sobre los candidatos visibles: dos consultas en
total (Servicios con categoría + términos activos, sin N+1) y trabajo lineal en
el texto de cada candidato. Es adecuado para un catálogo de cientos de
Servicios (milisegundos); hacia varios miles convendría preseleccionar en base de
datos o usar búsqueda de texto de PostgreSQL, sin cambiar la interfaz de este
módulo.
"""

from dataclasses import dataclass, field

from django.db.models import Prefetch

from apps.catalogo import normalizacion
from apps.catalogo.models import TerminoServicio
from apps.catalogo.visibilidad import servicios_visibles_para

# --- Pesos (centralizados) -------------------------------------------------------

# Término configurado: el mejor término puntúa completo; cada otro término que
# también coincide suma un refuerzo pequeño (hasta `MAX_TERMINOS_ADICIONALES`).
PESO_TERMINO_EXACTO = 100  # el término es toda la consulta
PESO_TERMINO_CUBIERTO = 80  # todas las palabras del término están en la consulta
PESO_TERMINO_CONTIENE_CONSULTA = 70  # toda la consulta está dentro del término
PESO_TERMINO_PARCIAL = 30  # × fracción de palabras del término presentes
PESO_TERMINO_ADICIONAL = 10
MAX_TERMINOS_ADICIONALES = 3

PESO_NOMBRE_EXACTO = 90  # el nombre es toda la consulta
PESO_NOMBRE_COBERTURA = 60  # × mejor cobertura (nombre↔consulta)

PESO_CATEGORIA_EXACTA = 35  # la consulta es exactamente el nombre de la categoría
PESO_CATEGORIA_COBERTURA = 25  # × cobertura de la categoría (< UMBRAL: solo refuerza)

PESO_DESCRIPCION = 30  # × cobertura de la consulta
PESO_INSTRUCCIONES = 20  # × cobertura de la consulta

UMBRAL_RELEVANCIA = 30
MAX_RESULTADOS = 5
LARGO_MAXIMO_CONSULTA = 200

# Claves de campo (también identifican la razón principal de una coincidencia).
TERMINO, NOMBRE, CATEGORIA, DESCRIPCION, INSTRUCCIONES = (
    "termino", "nombre", "categoria", "descripcion", "instrucciones",
)
_PRIORIDAD = (TERMINO, NOMBRE, CATEGORIA, DESCRIPCION, INSTRUCCIONES)

MENSAJE_VACIA = "Escribe lo que necesitas."
MENSAJE_POCO_ESPECIFICA = "Cuéntanos un poco más: usa al menos una palabra concreta, por ejemplo «vacaciones» o «presentación»."


@dataclass(frozen=True)
class Resultado:
    """Un Servicio/Proceso sugerido, con su puntaje y por qué obtuvo ese puntaje."""

    servicio: object
    puntuacion: int
    campo: str  # campo que más aportó (una de las claves de arriba)
    razon: str  # texto legible de esa razón principal
    puntos: dict = field(default_factory=dict)  # puntos por campo (para entender/testear el puntaje)


@dataclass(frozen=True)
class Consulta:
    normalizada: str
    raices: dict  # {raíz: palabra}


def preparar_consulta(texto):
    texto = str(texto or "")[:LARGO_MAXIMO_CONSULTA]
    return Consulta(normalizacion.normalizar(texto), normalizacion.raices(texto))


def validar_consulta(texto):
    """Mensaje de por qué `texto` no puede buscarse, o `None` si es válido. Una
    consulta vacía o sin una sola palabra con significado nunca lista el catálogo."""
    consulta = preparar_consulta(texto)
    if not consulta.normalizada:
        return MENSAJE_VACIA
    if not consulta.raices:
        return MENSAJE_POCO_ESPECIFICA
    return None


def servicios_buscables_para(usuario):
    """Servicios/Procesos que el buscador puede considerar para `usuario`: los de
    `servicios_visibles_para` (sin Ticket General) con categoría activa y un
    formulario con versión activa. Trae categoría y términos activos de una vez."""
    return (
        servicios_visibles_para(usuario)
        .filter(categoria__activo=True, formulario__version_activa__isnull=False)
        .select_related("categoria")
        .prefetch_related(
            Prefetch(
                "terminos_busqueda", queryset=TerminoServicio.objects.filter(activo=True), to_attr="terminos_activos"
            )
        )
    )


# --- Puntaje por campo ---------------------------------------------------------------


def _fraccion(parte, total, peso):
    return peso * parte // total if total else 0


def _puntos_termino(termino, consulta):
    """Puntos de UN término frente a la consulta (0 si no coincide)."""
    normalizado = normalizacion.normalizar(termino)
    if not normalizado:
        return 0
    if normalizado == consulta.normalizada:
        return PESO_TERMINO_EXACTO
    del_termino = set(normalizacion.raices(termino))
    de_la_consulta = set(consulta.raices)
    if not del_termino:
        return 0
    if del_termino <= de_la_consulta:
        return PESO_TERMINO_CUBIERTO
    if de_la_consulta <= del_termino:
        return PESO_TERMINO_CONTIENE_CONSULTA
    return _fraccion(len(del_termino & de_la_consulta), len(del_termino), PESO_TERMINO_PARCIAL)


def _puntos_terminos(terminos, consulta):
    """(puntos, término que más aportó) sumando el refuerzo de los demás que coinciden."""
    puntuados = [(_puntos_termino(t.termino, consulta), t.termino) for t in terminos]
    # Orden estable: más puntos primero y, a igualdad, el término alfabéticamente primero.
    puntuados = sorted((p for p in puntuados if p[0] > 0), key=lambda p: (-p[0], normalizacion.normalizar(p[1])))
    if not puntuados:
        return 0, ""
    adicionales = min(len(puntuados) - 1, MAX_TERMINOS_ADICIONALES)
    return puntuados[0][0] + adicionales * PESO_TERMINO_ADICIONAL, puntuados[0][1]


def _puntos_nombre(nombre, consulta):
    if normalizacion.normalizar(nombre) == consulta.normalizada:
        return PESO_NOMBRE_EXACTO
    del_nombre = set(normalizacion.raices(nombre))
    comunes = len(del_nombre & set(consulta.raices))
    return max(
        _fraccion(comunes, len(del_nombre), PESO_NOMBRE_COBERTURA),
        _fraccion(comunes, len(consulta.raices), PESO_NOMBRE_COBERTURA),
    )


def _puntos_categoria(nombre, consulta):
    if normalizacion.normalizar(nombre) == consulta.normalizada:
        return PESO_CATEGORIA_EXACTA
    de_la_categoria = set(normalizacion.raices(nombre))
    return _fraccion(len(de_la_categoria & set(consulta.raices)), len(de_la_categoria), PESO_CATEGORIA_COBERTURA)


def _puntos_texto(texto, consulta, peso):
    """Cuánto de la consulta explica un texto largo (descripción, instrucciones)."""
    if not texto:
        return 0
    comunes = len(set(normalizacion.raices(texto)) & set(consulta.raices))
    return _fraccion(comunes, len(consulta.raices), peso)


_RAZONES = {
    NOMBRE: "Coincidencia por nombre",
    CATEGORIA: "Coincidencia por categoría",
    DESCRIPCION: "Coincidencia en la descripción",
    INSTRUCCIONES: "Coincidencia en las instrucciones",
}


def puntuar(servicio, terminos, consulta):
    """`Resultado` de `servicio` frente a `consulta` (`Consulta`) o `None` si no
    coincide en nada. NO aplica el umbral: eso lo decide quien lista resultados."""
    puntos_termino, mejor_termino = _puntos_terminos(terminos, consulta)
    puntos = {
        TERMINO: puntos_termino,
        NOMBRE: _puntos_nombre(servicio.nombre, consulta),
        CATEGORIA: _puntos_categoria(servicio.categoria.nombre, consulta),
        DESCRIPCION: _puntos_texto(servicio.descripcion, consulta, PESO_DESCRIPCION),
        INSTRUCCIONES: _puntos_texto(servicio.instrucciones, consulta, PESO_INSTRUCCIONES),
    }
    total = sum(puntos.values())
    if total == 0:
        return None
    # Razón principal: el campo que más aportó; a igualdad, el de mayor prioridad.
    campo = max(_PRIORIDAD, key=lambda c: (puntos[c], -_PRIORIDAD.index(c)))
    razon = f"Coincide con: {mejor_termino}" if campo == TERMINO else _RAZONES[campo]
    return Resultado(servicio=servicio, puntuacion=total, campo=campo, razon=razon, puntos=puntos)


def buscar_servicios_por_necesidad(usuario, texto, *, limite=MAX_RESULTADOS):
    """Mejores coincidencias (a lo sumo `limite`) para lo que `usuario` describe
    en `texto`, ordenadas por puntuación desc, nombre asc, id asc. Lista vacía si
    la consulta no es válida (ver `validar_consulta`) o nada alcanza el umbral. No
    crea ni modifica nada."""
    if validar_consulta(texto) is not None:
        return []
    consulta = preparar_consulta(texto)
    resultados = []
    for servicio in servicios_buscables_para(usuario):
        resultado = puntuar(servicio, servicio.terminos_activos, consulta)
        if resultado is not None and resultado.puntuacion >= UMBRAL_RELEVANCIA:
            resultados.append(resultado)
    resultados.sort(key=lambda r: (-r.puntuacion, normalizacion.normalizar(r.servicio.nombre), r.servicio.pk))
    return resultados[:limite]
