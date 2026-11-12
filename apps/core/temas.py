"""Estilos de acento curados de la plataforma (Fase visual, Configuración › General).

Solo cambia el ACENTO (`--color-primary` y sus variantes): carbón, superficies, textos y
estados son la identidad fija de Órbita. Los colores viven únicamente en
`static/css/tokens.css` (bloques `[data-tema="…"]`); aquí está la lista cerrada de estilos
válidos, su nombre visible y su descripción. Agregar un estilo exige su escala en
`tokens.css` con contraste AA para texto oscuro encima: por eso son curados y no un color
libre.
"""

# Versión 1 (2026-11-12): Cielo es el estilo predeterminado; Lima sigue disponible (era el original).
TEMA_POR_DEFECTO = "cielo"

# clave → (nombre visible, descripción corta). El orden es el de la pantalla.
TEMAS = {
    "lima": ("Lima", "La identidad con la que nació Órbita: enérgica y tecnológica."),
    "menta": ("Menta", "Fresca y serena, con un verde limpio."),
    "cielo": ("Cielo", "El estilo predeterminado: claro y confiable, con un azul luminoso."),
    "ambar": ("Ámbar", "Cálida y optimista, con un dorado suave."),
    "coral": ("Coral", "Cercana y vibrante, con un tono humano."),
    "lavanda": ("Lavanda", "Creativa y tranquila, con un violeta claro."),
}

OPCIONES = [(clave, nombre) for clave, (nombre, _descripcion) in TEMAS.items()]


def descripcion_de(clave):
    return TEMAS.get(clave, TEMAS[TEMA_POR_DEFECTO])[1]
