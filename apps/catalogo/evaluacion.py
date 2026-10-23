"""Evaluación centralizada del Studio (Bloque 2) — UNA consulta que leen la navegación de las cinco secciones, el
panel de Formulario y «Revisión y publicación», para que no se contradigan.

Distingue tres cosas sobre el mismo Servicio/Proceso:

A. VIGENTE: lo que hoy está activo (`vigentes`: lo que impediría publicar lo persistido tal cual).
B. PREPARADO: lo que resultaría de activar los borradores pendientes (`activaria`).
C. BLOQUEOS REALES: lo que impediría publicar DESPUÉS de activar esos borradores (`bloqueos`).

Los bloqueos salen de SIMULAR la publicación real (`publicacion.simular`: activa las piezas pendientes dentro de
una transacción que siempre se deshace y aplica `operaciones.errores_para_publicar`, la misma regla que
`activar_servicio`). Así un borrador válido nunca aparece como error y nada se activa al consultar. La simulación
evalúa la CONFIGURACIÓN, no los permisos de quien mira: el permiso por pieza se informa aparte en Publicar
(`publicacion.permisos_que_faltan`) y publicar de verdad lo sigue exigiendo.

Bloque 2.1 — costo de la consulta: solo se SIMULA si hay borradores pendientes. Sin ellos, lo preparado ES lo
vigente y la simulación se reduciría a `errores_para_publicar` (lo que aplica `_solo_validar`): se llama
directamente, sin transacción ni escrituras. La simulación, cuando hace falta, no toma el lock de la fila del
Servicio (`bloquear=False`; publicar de verdad lo toma y revalida). Una sola evaluación por petición: la navegación,
Básico, Formulario y Publicar reutilizan la misma (`apartados_basico`, `grupos_traducidos`).

Sin actor (o con un flujo LEGACY, que publica por su propia vía) no se simula: los bloqueos son los vigentes.
Solo lectura. No contiene reglas de negocio: compone validadores existentes.
"""

from dataclasses import dataclass, field
from functools import cached_property

from django.core.exceptions import ValidationError

from apps.catalogo.models import FormularioVersion, Servicio

# Claves de sección de `experiencia_proceso.errores_por_seccion`.
GENERAL, DATOS, ELEMENTOS, SALIDA, FLUJO = "general", "entrada", "elementos", "salida", "ejecucion"


@dataclass
class PiezaFormulario:
    """Una pieza del Formulario: los datos de inicio o una colección de ítems."""

    clave: str
    nombre: str
    configurada: bool  # existe y no es el formulario técnico vacío
    version: object = None  # versión ACTIVA (o None)
    campos: int = 0  # campos/columnas de la versión activa
    obligatorios: int = 0
    borrador: bool = False  # hay un borrador pendiente de activar
    definicion: object = None  # DefinicionParrilla (solo colecciones)

    @property
    def activa_valida(self):
        return self.version is not None and self.campos > 0


@dataclass
class Evaluacion:
    servicio: object
    piezas: list
    bloqueos: list
    activaria: list
    simulada: bool  # se evaluó el estado PREPARADO (con o sin simulación real)
    por_seccion: dict = field(default_factory=dict)
    datos_inicio: object = None
    colecciones: list = field(default_factory=list)
    grupos: list = field(default_factory=list)  # `errores_por_seccion` de los bloqueos, sin traducir
    vigentes_calculados: list = None  # se calcula solo si se pide (ver `vigentes`)

    @cached_property
    def vigentes(self):
        """A. Lo que impediría publicar lo persistido tal cual (sin activar borradores). Bajo demanda."""
        if self.vigentes_calculados is not None:
            return self.vigentes_calculados
        from apps.catalogo.operaciones import errores_para_publicar

        return errores_para_publicar(self.servicio)

    @cached_property
    def apartados_basico(self):
        """Los apartados de Básico (`estado_basico.apartados`), una sola vez por petición."""
        from apps.catalogo import estado_basico

        return estado_basico.apartados(self.servicio)

    def grupos_traducidos(self, traducir):
        """Los bloqueos agrupados por sección con el lenguaje del Studio (`traducir`); un mensaje que se traduce a
        vacío se omite, igual que en `experiencia_proceso.errores_por_seccion`."""
        resultado = []
        for grupo in self.grupos:
            errores = [t for t in (traducir(m) for m in grupo["errores"]) if t]
            if errores:
                resultado.append({**grupo, "errores": errores})
        return resultado

    @property
    def pendientes(self):
        return [p for p in self.piezas if p.pendiente]

    @property
    def publicable(self):
        return not self.bloqueos

    def bloqueos_de(self, *secciones):
        return [m for s in secciones for m in self.por_seccion.get(s, [])]

    def pendiente_en(self, *prefijos):
        """¿Hay un borrador pendiente en alguna pieza cuya clave empieza por `prefijos`?"""
        return any(p.clave.startswith(prefijos) for p in self.pendientes)


def _contar(version):
    if version is None:
        return 0, 0
    campos = list(version.campos.all())
    return len(campos), sum(1 for c in campos if c.obligatorio)


def piezas_formulario(servicio, pendientes=None):
    """`(datos_de_inicio, [colecciones])` como `PiezaFormulario`. `pendientes`: claves de piezas con borrador (por
    defecto, las de `publicacion.piezas`)."""
    from apps.catalogo import publicacion
    from apps.catalogo.operaciones import datos_inicio_configurados
    from apps.catalogo.parrilla import definiciones_activas_de

    if pendientes is None:
        pendientes = {p.clave for p in publicacion.piezas(servicio) if p.pendiente}
    formulario = servicio.formulario
    activa = formulario.version_activa if formulario is not None else None
    if activa is not None and activa.estado != FormularioVersion.Estado.ACTIVA:
        activa = None
    campos, obligatorios = _contar(activa)
    datos = PiezaFormulario(
        "formulario", "Datos de inicio" if servicio.tipo == Servicio.Tipo.PROCESO else "Formulario de inicio",
        configurada=datos_inicio_configurados(servicio), version=activa, campos=campos, obligatorios=obligatorios,
        borrador="formulario" in pendientes,
    )
    colecciones = []
    if servicio.tipo == Servicio.Tipo.PROCESO and not servicio.es_ticket_general:
        for definicion in definiciones_activas_de(servicio):
            vigente = definicion.formulario.version_activa
            if vigente is not None and vigente.estado != FormularioVersion.Estado.ACTIVA:
                vigente = None
            columnas, obligatorias = _contar(vigente)
            colecciones.append(PiezaFormulario(
                f"coleccion:{definicion.clave}", definicion.nombre_elemento, configurada=True, version=vigente,
                campos=columnas, obligatorios=obligatorias, borrador=f"coleccion:{definicion.clave}" in pendientes,
                definicion=definicion,
            ))
    return datos, colecciones


def _solo_validar(servicio, actor):
    """La parte de `activar_servicio` que decide si se puede publicar, sin publicar (para la simulación)."""
    from apps.catalogo.operaciones import errores_para_publicar

    errores = errores_para_publicar(servicio)
    if errores:
        raise ValidationError(errores)
    return servicio


def evaluar(servicio, actor=None):
    """La evaluación del Servicio/Proceso (ver el docstring del módulo). Solo lectura."""
    from apps.catalogo import publicacion
    from apps.catalogo.experiencia_proceso import errores_por_seccion
    from apps.catalogo.operaciones import errores_para_publicar
    from apps.workflows.models import Workflow

    piezas = publicacion.piezas(servicio)
    pendientes = [p for p in piezas if p.pendiente]
    legacy = servicio.workflow_id is not None and servicio.workflow.modo != Workflow.Modo.PLANTILLA_FASES
    preparado = actor is not None and not legacy
    vigentes = None
    if preparado and pendientes:
        # Hay borradores: lo preparado solo se conoce activándolos (simulación que siempre se deshace, sin lock).
        _publicable, bloqueos, _activaria = publicacion.simular(
            servicio, actor, verificar_permisos=False, activar=_solo_validar, bloquear=False
        )
    else:
        # Sin borradores (o sin simulación posible): lo preparado es lo vigente. Misma regla, sin escrituras.
        bloqueos = vigentes = errores_para_publicar(servicio)
    # Lo que entraría en vigor al publicar: el mismo texto que `publicar_cambios`, aunque algo lo impida.
    activaria = [f"{p.etiqueta} v{p.borrador.numero}" for p in pendientes] if preparado else []
    grupos = errores_por_seccion(servicio, bloqueos)
    por_seccion = {g["seccion"]: list(g["errores"]) for g in grupos}
    datos, colecciones = piezas_formulario(servicio, {p.clave for p in pendientes})
    return Evaluacion(
        servicio=servicio, piezas=piezas, bloqueos=list(bloqueos), activaria=activaria, simulada=preparado,
        por_seccion=por_seccion, datos_inicio=datos, colecciones=colecciones, grupos=grupos,
        vigentes_calculados=vigentes,
    )
