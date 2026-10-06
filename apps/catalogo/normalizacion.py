"""Normalización de texto para el buscador "¿Qué necesitas?" (Sprint 4.D).

Funciones puras, sin acceso a base de datos: comparan lo que escribe una
persona con lo que está configurado en el catálogo sin alterar jamás lo
almacenado. Todo lo que se compara pasa por las MISMAS funciones, de modo que
"Presentación Comercial", "presentacion comercial" y "PRESENTACIÓN, comercial!"
son equivalentes.

Tres niveles, de menor a mayor interpretación:

- `normalizar(texto)`   minúsculas, sin acentos ni puntuación, espacios únicos.
- `palabras(texto)`     palabras con significado: sin palabras vacías
                        (stopwords) y de al menos `LARGO_MINIMO_PALABRA`.
- `raiz(palabra)`       raíz aproximada para tolerar plurales y derivaciones
                        ("presentación" ≈ "presentar" ≈ "presentaciones").

La raíz NO es un lematizador: quita una `s` final y recorta a
`LARGO_RAIZ` caracteres. Es determinista, explicable y simétrica (se aplica
igual a la consulta y al catálogo). Límite conocido: puede unir palabras
distintas que comparten los primeros `LARGO_RAIZ` caracteres; por eso el
recorte es de 7 y no menor ("contrato" y "contraseña" siguen separadas).
"""

import re
import unicodedata

LARGO_MINIMO_PALABRA = 2
LARGO_RAIZ = 7

# Lista explícita y pequeña. Palabras funcionales del español más verbos de
# intención genéricos ("necesito", "quiero", "solicitar"...), que describen
# CÓMO pide la persona y no QUÉ pide. Está normalizada (sin acentos).
PALABRAS_VACIAS = frozenset(
    """
    a al algo algun alguna algunas alguno algunos ante aqui asi aun buenas buenos como con cual cuales cuando de del
    desde donde el ella ellas ellos en entre era es esa esas ese eso esos esta estan estas este esto estos favor fue
    gracias ha han hay la las le les lo los mas me mi mis muy ni no nos o otra otras otro otros para pero por porque
    que quien se ser si sin sobre son su sus te tu tus un una uno unas unos y ya yo
    busco crear deseo desearia hacer necesita necesitamos necesitar necesito pedir puede pueden puedo quiero quisiera
    realizar requiero solicitar solicito
    """.split()
)

_NO_ALFANUMERICO = re.compile(r"[\W_]+", re.UNICODE)


def normalizar(texto):
    """Minúsculas, sin diacríticos, sin puntuación y con espacios únicos. No
    elimina palabras: para eso están `palabras` y `raiz`."""
    descompuesto = unicodedata.normalize("NFKD", str(texto or ""))
    sin_acentos = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return _NO_ALFANUMERICO.sub(" ", sin_acentos.casefold()).strip()


def palabras(texto):
    """Palabras con significado de `texto`, en orden y sin repetidas."""
    vistas = []
    for palabra in normalizar(texto).split():
        if len(palabra) >= LARGO_MINIMO_PALABRA and palabra not in PALABRAS_VACIAS and palabra not in vistas:
            vistas.append(palabra)
    return vistas


def raiz(palabra):
    """Raíz aproximada de una palabra YA normalizada (ver docstring del módulo)."""
    if len(palabra) > 3 and palabra.endswith("s"):
        palabra = palabra[:-1]
    return palabra[:LARGO_RAIZ]


def raices(texto):
    """{raíz: palabra original} de las palabras con significado de `texto`. Si
    dos palabras comparten raíz conserva la primera (sirve para explicar una
    coincidencia con las palabras de la persona)."""
    resultado = {}
    for palabra in palabras(texto):
        resultado.setdefault(raiz(palabra), palabra)
    return resultado
