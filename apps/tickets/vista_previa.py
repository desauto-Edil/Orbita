"""Vista previa de archivos (UX-4b): qué archivos se pueden MOSTRAR en el navegador en lugar de solo descargarse.

Lista cerrada por extensión (nunca el tipo que declaró el navegador al subirlo): imágenes, video y PDF. Nada que el
navegador pueda ejecutar (HTML, SVG, scripts) se muestra en línea; todo lo demás se descarga como siempre. No
decide quién puede ver el archivo: eso sigue en `descargar_adjunto_view`.
"""

_TIPOS = {
    "png": ("imagen", "image/png"),
    "jpg": ("imagen", "image/jpeg"),
    "jpeg": ("imagen", "image/jpeg"),
    "gif": ("imagen", "image/gif"),
    "webp": ("imagen", "image/webp"),
    "mp4": ("video", "video/mp4"),
    "m4v": ("video", "video/mp4"),
    "webm": ("video", "video/webm"),
    "mov": ("video", "video/quicktime"),
    "pdf": ("pdf", "application/pdf"),
}


def _tipo(nombre):
    if not nombre or "." not in nombre:
        return None
    return _TIPOS.get(nombre.rsplit(".", 1)[-1].lower())


def clase_de(nombre):
    """«imagen», «video», «pdf» o «» (solo se descarga)."""
    tipo = _tipo(nombre)
    return tipo[0] if tipo else ""


def tipo_mime_en_linea(nombre):
    """El tipo con el que se sirve en línea, o `None` si este archivo no se muestra en el navegador."""
    tipo = _tipo(nombre)
    return tipo[1] if tipo else None
