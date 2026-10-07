"""Strategy Pattern para el comportamiento de cada tipo de Etapa
(CU-020, RQF-063/064/065/066/067, RN-021).

3.1 definió únicamente `validar_configuracion()` (qué claves admite
`Etapa.configuracion`). 3.2 agrega el contrato de EJECUCIÓN: `ejecutar()`
más `ejecutable` como **único** indicador de disponibilidad (W.5,
corrección aprobada: "no quiero que validación diga 'disponible' mientras
ejecución diga 'no disponible'"). `ejecutable` es consultado tanto por
`apps.workflows.validacion.validar_estructura` (¿puede activarse una
versión que use este tipo?) como por el motor (¿puede ejecutarse en
runtime?) — un único atributo, nunca dos registros con criterios
distintos.

Igual que `apps/catalogo/campos.py::ESTRATEGIAS_POR_TIPO`, el registry es un
diccionario plano — un único punto de variación real no justifica una
Factory (RN-021: "El motor de workflow debe delegar el comportamiento de
cada tipo de etapa sin duplicar un motor por dominio").

`APROBACION` y `GACETA` siguen sin tener entrada aquí (X.1, 3.1): sus
Strategies son responsabilidad de Sprint 4 y Sprint 6. `TAREA` y `TICKET`
SÍ tienen entrada (validan su configuración vacía, igual que en 3.1) pero
`ejecutable=False`: 3.2 no implementa sus dominios operacionales (Tarea/
Ticket-de-workflow todavía no existen — W.5, corrección aprobada), así que
una versión que los use no puede activarse todavía, aunque sí puede
diseñarse. Cuando su Strategy de ejecución real se implemente, se habilita
cambiando únicamente este registro — sin rediseñar el modelo.

3.1 elimina de la configuración de TAREA/TICKET/HITO todo lo relativo a
responsable/plazo/formulario/servicio/subtareas/delegación (corrección
explícita del usuario, X.7/X.2): esos vínculos son relaciones de dominio
reales (Usuario/Equipo/Área/Unidad/Servicio/Formulario), y la regla
acordada es "configuración simple → JSON, relación real de dominio →
FK/modelo relacional" — no hay todavía dónde declarar esa FK (pertenece a
3.3). Por eso estos tipos quedan sin configuración propia, no con una
configuración incompleta.

3.3 (CU-021/022/023, RQF-071 a 077) activa TAREA: `EstrategiaTarea` crea
una `Tarea` real (`apps.tareas`, dominio independiente de Workflow — RQF-071
documenta que las tareas también pueden provenir de tickets, así que este
módulo consume la API pública de `apps.tareas`, nunca al revés) y devuelve
`ESPERAR` con `motivo_espera=MotivoEspera.TAREA` — el mismo mecanismo de
espera que ya usaba `EstrategiaEspera` (ahora con `motivo_espera=
MotivoEspera.TEMPORAL`), sin agregar un estado nuevo al motor (W.3/W.7,
correcciones aprobadas). Su configuración real (responsable candidato,
`permite_subtareas`) vive en `apps.workflows.models.ConfiguracionEtapaTarea`
(relación real, no JSON) — `Etapa.configuracion` sigue vacía para TAREA,
igual que HITO/FIN.

`TICKET` sigue con `EstrategiaSinConfiguracion()` (`ejecutable=False`, no se
habilita en 3.3 — instrucción explícita: "no habilites todavía TICKET").

3.4 (CU-024/025, RQF-078 a 084) activa APROBACION: `EstrategiaAprobacion`
crea un `EsquemaAprobacion` real (`apps.aprobaciones`, dominio independiente
de Workflow) y devuelve `ESPERAR` con `motivo_espera=MotivoEspera.APROBACION`
— mismo mecanismo genérico, sin estado nuevo en el motor. Su configuración
real vive en `apps.workflows.models.ConfiguracionEtapaAprobacion`/
`ParticipanteEtapaAprobacion` (relación real, no JSON) — `Etapa.configuracion`
sigue vacía para APROBACION, igual que TAREA. `GACETA` sigue sin entrada
(Sprint 6); `TICKET` sigue sin `ejecutable=True`.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.workflows.contexto import evaluar_operador
from apps.workflows.variables import ResolutorVariables
from apps.workflows.actores import resolver_actor


class ResultadoEjecucion:
    """Los 4 resultados que `EstrategiaEtapa.ejecutar()` puede devolver
    (sección D de la propuesta aprobada). Constantes simples, no un
    `TextChoices` de Django — no son choices de un campo de modelo, se
    intercambian en memoria entre la Strategy y el motor."""

    CONTINUAR = "CONTINUAR"
    ESPERAR = "ESPERAR"
    COMPLETAR = "COMPLETAR"
    ERROR = "ERROR"


class MotivoEspera:
    """Por qué una `InstanciaEtapa` queda `ESPERAR` — 3.2.x solo conocía
    esperas temporales; 3.3 (W.3, corrección aprobada) agrega TAREA. Igual
    que `ResultadoEjecucion`: constantes simples en memoria, no un
    `TextChoices` — `InstanciaEtapa.MotivoEspera` (`models.py`) declara su
    propio `TextChoices` con los mismos valores de texto para el campo de
    BD; no se comparte la clase entre ambos módulos para no invertir la
    dependencia ya existente `models.py → estrategias.py`.

    Solo TEMPORAL/TAREA por ahora — instrucción explícita: no agregar
    APROBACION/TICKET/GACETA todavía "solo para anticipar futuro"."""

    TEMPORAL = "TEMPORAL"
    TAREA = "TAREA"
    APROBACION = "APROBACION"
    ENTREGABLE = "ENTREGABLE"


@dataclass
class ResultadoEjecucionEtapa:
    """Contrato de retorno de `ejecutar()` (sección "Corrección" aprobada
    sobre CONDICION, y sección D). La Strategy nunca escribe directamente
    sobre `InstanciaWorkflow.contexto` — solo devuelve los cambios
    (`datos`, `variables_actualizadas`); el motor los aplica/persiste.

    `transicion_seleccionada` solo lo usa `EstrategiaCondicion` — el motor
    delega en la Strategy CUÁL transición tomar cuando puede haber varias
    (RN-021: el motor no debe saber cómo decide una CONDICION). Para
    cualquier otro tipo se deja en `None` y el motor usa la única
    transición saliente ya validada estructuralmente (W.4).

    `motivo_espera` (3.3, W.3) solo lo usan las Strategies que devuelven
    `estado=ESPERAR` (`EstrategiaEspera`/`EstrategiaTarea`) — el motor lo
    copia tal cual a `InstanciaEtapa.motivo_espera` sin interpretarlo
    (nunca decide él mismo por `etapa.tipo`, mismo criterio RN-021 que
    `transicion_seleccionada`)."""

    estado: str
    datos: dict = field(default_factory=dict)
    variables_actualizadas: dict = field(default_factory=dict)
    transicion_seleccionada: object = None
    motivo_espera: str | None = None
    mensaje_error: str | None = None
    # 4.B1: `(ámbito, datos)` que el MOTOR publica en el contexto por la clave estable del
    # bloque (`contexto.publicar_resultado_bloque`) cuando la etapa se completa. La
    # Strategy nunca escribe el contexto: solo declara qué resultado produjo.
    resultado_bloque: tuple | None = None


class EstrategiaEtapa(ABC):
    claves_configuracion: frozenset = frozenset()
    ejecutable: bool = False

    def validar_configuracion(self, configuracion):
        desconocidas = set(configuracion) - self.claves_configuracion
        if desconocidas:
            raise ValidationError(
                f"Claves de configuración no admitidas para este tipo de etapa: {sorted(desconocidas)}."
            )
        self._validar_configuracion(configuracion)

    def _validar_configuracion(self, configuracion):
        return None

    def ejecutar(self, instancia_etapa, contexto) -> ResultadoEjecucionEtapa:
        """Solo se invoca si `ejecutable=True` (garantizado por
        `validar_estructura` al momento de activar la versión; el motor lo
        revalida de todas formas como defensa contra una escritura masiva
        que bypasee esa validación — mismo criterio que
        `apps/workflows/validacion.py`)."""
        raise NotImplementedError(
            f"{type(self).__name__} no implementa ejecutar() (ejecutable=False)."
        )


class EstrategiaSinConfiguracion(EstrategiaEtapa):
    """Ninguna clave de configuración propia. `TAREA`/`TICKET` la usan tal
    cual (`ejecutable=False`, ver docstring del módulo). `INICIO`/`HITO`/
    `FIN`/`CONDICION` heredan de esta misma clase — comparten "sin
    configuración propia" — pero se declaran ejecutables y agregan su
    propio `ejecutar()` (subclases abajo)."""


def _definicion(instancia_etapa):
    return getattr(instancia_etapa, "definicion_ejecutable", instancia_etapa.etapa)


class EstrategiaInicio(EstrategiaSinConfiguracion):
    """RQF-065: arranca la cadena de ejecución. Sin lógica de negocio
    propia — el motor ya creó la `InstanciaEtapa` de INICIO; aquí solo se
    confirma que debe completarse y continuar.

    Única excepción mecánica (no de negocio): promueve `datos_iniciales`
    a `variables` en el mismo momento en que arranca la instancia — es el
    único punto donde tiene sentido hacerlo sin ambigüedad (una sola vez,
    determinista, sin cascada implícita durante la evaluación de ninguna
    CONDICION posterior — sección F de la propuesta aprobada). Sin este
    paso, ninguna CONDICION podría depender jamás de un dato de entrada:
    "variables" nunca se puebla solo, y ningún otro tipo ejecutable en 3.2
    tiene una razón de negocio para tocarlo."""

    ejecutable = True

    def ejecutar(self, instancia_etapa, contexto):
        return ResultadoEjecucionEtapa(
            estado=ResultadoEjecucion.CONTINUAR,
            variables_actualizadas=dict(contexto.get("datos_iniciales") or {}),
        )


class EstrategiaHito(EstrategiaSinConfiguracion):
    """Registra que la etapa fue alcanzada (la propia fila `InstanciaEtapa`,
    con sus tiempos, ya es esa evidencia) y continúa de inmediato — sin
    convertirse en Tarea."""

    ejecutable = True

    def ejecutar(self, instancia_etapa, contexto):
        return ResultadoEjecucionEtapa(estado=ResultadoEjecucion.CONTINUAR)


class EstrategiaFin(EstrategiaSinConfiguracion):
    """Termina la instancia — `COMPLETAR`, no `CONTINUAR`: le indica al
    motor que no debe buscar ninguna transición saliente (no las hay,
    `validar_estructura` ya lo garantiza)."""

    ejecutable = True

    def ejecutar(self, instancia_etapa, contexto):
        return ResultadoEjecucionEtapa(estado=ResultadoEjecucion.COMPLETAR)


class EstrategiaCondicion(EstrategiaSinConfiguracion):
    """RQF-066/RN-021: la CONDICION —no el motor— decide qué transición
    sigue. Ordena por `prioridad` (orden ya garantizado por
    `TransicionEtapa.Meta.ordering`), evalúa cada transición no-fallback
    contra su variable y devuelve la primera que coincide; si ninguna coincide,
    devuelve el fallback (estructuralmente garantizado único por
    `validar_estructura`). 4.B0: la variable puede ser plana (`variables` del
    contexto, como siempre) o una referencia punteada (`ticket.estado`,
    `formulario.<clave>`, `aprobaciones.<clave>.resultado`) que resuelve
    `apps.workflows.variables`; una variable inexistente nunca coincide. La
    precedencia (prioridad → fallback) no cambia."""

    ejecutable = True

    def ejecutar(self, instancia_etapa, contexto):
        salientes = list(_definicion(instancia_etapa).transiciones_salientes.all())
        fallback = next((t for t in salientes if t.es_fallback), None)
        resolutor = ResolutorVariables(getattr(instancia_etapa, "instancia_workflow", None), contexto)
        for transicion in salientes:
            if transicion.es_fallback:
                continue
            valor_actual = resolutor.resolver(transicion.variable)
            if evaluar_operador(transicion.operador, valor_actual, transicion.valor):
                return ResultadoEjecucionEtapa(
                    estado=ResultadoEjecucion.CONTINUAR, transicion_seleccionada=transicion
                )
        return ResultadoEjecucionEtapa(
            estado=ResultadoEjecucion.CONTINUAR, transicion_seleccionada=fallback
        )


class EstrategiaEspera(EstrategiaEtapa):
    """RQF-067: "esperar... conforme a condiciones o fechas configuradas".

    3.1 solo modeló lo que el texto documental sostiene sin ambigüedad:
    duración relativa o fecha objetivo absoluta. "Espera por evento" queda
    fuera — ninguna RN la exige y Domain Events es Sprint 7 (X.3, decisión
    aprobada). 3.2 agrega `ejecutar()`: calcula `reanudar_en` y devuelve
    `ESPERAR` — no reanuda nada por sí sola, eso es
    `apps.workflows.motor.reanudar_instancia` (W.6: sin Celery todavía).
    """

    ejecutable = True
    MODOS = frozenset({"DURACION", "FECHA"})
    UNIDADES = frozenset({"DIAS", "HORAS"})
    claves_configuracion = frozenset({"modo", "duracion_valor", "duracion_unidad", "fecha_objetivo"})

    def _validar_configuracion(self, configuracion):
        modo = configuracion.get("modo")
        if modo not in self.MODOS:
            raise ValidationError(f"modo debe ser uno de {sorted(self.MODOS)}.")

        if modo == "DURACION":
            if "fecha_objetivo" in configuracion:
                raise ValidationError("modo=DURACION no admite fecha_objetivo.")
            valor = configuracion.get("duracion_valor")
            unidad = configuracion.get("duracion_unidad")
            if not isinstance(valor, int) or isinstance(valor, bool) or valor <= 0:
                raise ValidationError("duracion_valor debe ser un entero positivo.")
            if unidad not in self.UNIDADES:
                raise ValidationError(f"duracion_unidad debe ser uno de {sorted(self.UNIDADES)}.")
        else:  # FECHA
            if "duracion_valor" in configuracion or "duracion_unidad" in configuracion:
                raise ValidationError("modo=FECHA no admite duracion_valor/duracion_unidad.")
            fecha = configuracion.get("fecha_objetivo")
            if not fecha:
                raise ValidationError("modo=FECHA requiere fecha_objetivo.")
            try:
                date.fromisoformat(str(fecha))
            except ValueError as exc:
                raise ValidationError("fecha_objetivo debe tener formato ISO (AAAA-MM-DD).") from exc

    def ejecutar(self, instancia_etapa, contexto):
        configuracion = _definicion(instancia_etapa).configuracion or {}
        if configuracion.get("modo") == "DURACION":
            unidad = configuracion["duracion_unidad"]
            delta = (
                timedelta(days=configuracion["duracion_valor"])
                if unidad == "DIAS"
                else timedelta(hours=configuracion["duracion_valor"])
            )
            reanudar_en = timezone.now() + delta
        else:  # FECHA
            fecha = date.fromisoformat(str(configuracion["fecha_objetivo"]))
            reanudar_en = timezone.make_aware(datetime.combine(fecha, time.min))
        return ResultadoEjecucionEtapa(
            estado=ResultadoEjecucion.ESPERAR,
            motivo_espera=MotivoEspera.TEMPORAL,
            datos={"reanudar_en": reanudar_en.isoformat()},
        )


class EstrategiaTarea(EstrategiaSinConfiguracion):
    """RQF-068/CU-021 — 3.3. Crea una `Tarea` real (`apps.tareas`, dominio
    independiente de Workflow) y dos ejecuciones humanas se activan sobre
    ella (`apps.tareas.operaciones`), no aquí. El motor nunca sabe qué es
    una `Tarea`: solo recibe `ESPERAR`/`motivo_espera=TAREA`, igual
    contrato que `EstrategiaEspera` (RN-021).

    Import local de `apps.workflows.models.TareaWorkflow` (no a nivel de
    módulo): `models.py` ya importa `ESTRATEGIAS_POR_TIPO` desde este
    archivo, así que un `import` de `models.py` aquí arriba crearía un
    ciclo de carga de módulos — puramente mecánico, no una frontera de
    arquitectura (`apps.workflows` importándose a sí mismo sigue siendo la
    misma app; la regla real, "`apps.tareas` nunca importa
    `apps.workflows`", no se toca)."""

    ejecutable = True

    def ejecutar(self, instancia_etapa, contexto):
        from apps.tareas.models import Tarea
        from apps.tareas.operaciones import crear_tarea
        from apps.workflows.models import TareaWorkflow

        etapa = _definicion(instancia_etapa)
        config = getattr(etapa, "configuracion_tarea", None)
        usuario_responsable = None
        equipo_responsable = None
        if config is not None:
            try:
                usuario_responsable, equipo_responsable = resolver_actor(
                    config.tipo_responsable, instancia=instancia_etapa.instancia_workflow,
                    usuario=config.usuario_responsable, equipo=config.equipo_responsable,
                    permite_vacio=True,
                )
            except ValidationError as exc:
                if (
                    getattr(etapa, "permite_responsable_pendiente", False)
                    and config.tipo_responsable == "RESPONSABLE_TICKET"
                    and any("todavía no tiene un responsable individual" in mensaje for mensaje in exc.messages)
                ):
                    usuario_responsable, equipo_responsable = None, None
                else:
                    raise

        tarea = crear_tarea(
            titulo=etapa.nombre,
            descripcion=etapa.descripcion,
            origen=Tarea.Origen.SISTEMA,
            usuario_responsable=usuario_responsable,
            equipo_responsable=equipo_responsable,
            permite_subtareas=config.permite_subtareas if config is not None else False,
        )
        TareaWorkflow.objects.create(tarea=tarea, instancia_etapa=instancia_etapa)

        return ResultadoEjecucionEtapa(
            estado=ResultadoEjecucion.ESPERAR,
            motivo_espera=MotivoEspera.TAREA,
            datos={"tarea_id": tarea.pk},
        )


class EstrategiaAprobacion(EstrategiaSinConfiguracion):
    """RQF-081/082, CU-025 — 3.4. Crea un `EsquemaAprobacion` real
    (`apps.aprobaciones`, dominio independiente de Workflow) a partir de la
    plantilla `ConfiguracionEtapaAprobacion`/`ParticipanteEtapaAprobacion`,
    y deja la ejecución `ESPERAR` con `motivo_espera=APROBACION` — mismo
    contrato exacto que `EstrategiaTarea` (RN-021: el motor nunca sabe qué
    es un `EsquemaAprobacion`). Ninguna lógica de política
    (TODOS/CUALQUIERA/secuencial) vive aquí ni en el motor — pertenece
    íntegramente a `apps.aprobaciones.operaciones` (instrucción explícita
    del diseño aprobado).

    Import local de `apps.aprobaciones`/`apps.workflows.models.
    EsquemaAprobacionWorkflow` (no a nivel de módulo): mismo motivo
    mecánico que `EstrategiaTarea` — evita el ciclo
    `models.py → estrategias.py`."""

    ejecutable = True

    def ejecutar(self, instancia_etapa, contexto):
        from apps.aprobaciones.models import Aprobacion
        from apps.aprobaciones.operaciones import crear_esquema_aprobacion
        from apps.workflows.models import EsquemaAprobacionWorkflow

        etapa = _definicion(instancia_etapa)
        configuracion = etapa.configuracion_aprobacion
        participantes = []
        for participante in configuracion.participantes.order_by("orden"):
            usuario, equipo = resolver_actor(
                participante.tipo_aprobador, instancia=instancia_etapa.instancia_workflow,
                usuario=participante.usuario, equipo=participante.equipo,
            )
            participantes.append((
                Aprobacion.TipoAprobador.USUARIO if usuario is not None else Aprobacion.TipoAprobador.EQUIPO,
                usuario if usuario is not None else equipo,
            ))

        esquema = crear_esquema_aprobacion(
            modo=configuracion.modo,
            politica=configuracion.politica or None,
            participantes=participantes,
        )
        EsquemaAprobacionWorkflow.objects.create(esquema=esquema, instancia_etapa=instancia_etapa)

        return ResultadoEjecucionEtapa(
            estado=ResultadoEjecucion.ESPERAR,
            motivo_espera=MotivoEspera.APROBACION,
            datos={"esquema_id": esquema.pk},
        )


class EstrategiaEntregable(EstrategiaSinConfiguracion):
    """4.B1 — el bloque ENTREGABLE exige que un entregable DEFINIDO del Servicio esté
    satisfecho antes de continuar. No almacena nada, no es una Tarea y no entrega
    formalmente el Ticket: solo consulta el `EntregableTicket` congelado del Ticket
    (`apps.tickets.entregables.entregable_esta_satisfecho`, única regla de "satisfecho").

    - Ya satisfecho → `CONTINUAR` y el motor publica `entregables.<clave>.satisfecho`.
      4.E2: «satisfecho» se evalúa sobre la versión VIGENTE: si una aprobación que revisa este
      entregable ya observó (devolvió/rechazó) exactamente esa versión, el bloque espera una
      nueva (`apps.tickets.entregables.entregable_vigente_para_flujo`).
    - Pendiente → `ESPERAR` con `motivo_espera=ENTREGABLE`: estado técnico EN_ESPERA, sin
      Tarea ni temporizador. Lo libera `apps.workflows.integracion.continuar_por_entregable`
      desde el dominio del entregable (mismo patrón de espera externa que APROBACION).
    - Sin Ticket vinculado, sin definición o sin el `EntregableTicket` congelado → `ERROR`
      funcional controlado: nunca se inventa un entregable en plena ejecución.

    Imports locales: `apps.tickets` importa el motor, no al revés a nivel de módulo."""

    ejecutable = True

    def ejecutar(self, instancia_etapa, contexto):
        from apps.tickets.entregables import entregable_vigente_para_flujo
        from apps.tickets.models import EntregableTicket

        etapa = _definicion(instancia_etapa)
        definicion_id = getattr(etapa, "definicion_entregable_id", None)
        if definicion_id is None:
            return ResultadoEjecucionEtapa(
                estado=ResultadoEjecucion.ERROR, mensaje_error="El bloque de entregable no tiene un entregable seleccionado."
            )
        ticket = ResolutorVariables(getattr(instancia_etapa, "instancia_workflow", None), contexto).ticket
        if ticket is None:
            return ResultadoEjecucionEtapa(
                estado=ResultadoEjecucion.ERROR, mensaje_error="Este bloque necesita una ejecución vinculada a un Ticket."
            )
        entregable = EntregableTicket.objects.filter(ticket=ticket, definicion_id=definicion_id).first()
        if entregable is None:
            return ResultadoEjecucionEtapa(
                estado=ResultadoEjecucion.ERROR,
                mensaje_error="El ticket no tiene el entregable de este bloque (no se congeló al crearlo).",
            )
        if entregable_vigente_para_flujo(entregable):
            return ResultadoEjecucionEtapa(
                estado=ResultadoEjecucion.CONTINUAR,
                datos={"entregable_id": entregable.pk},
                resultado_bloque=("entregables", {"satisfecho": True}),
            )
        return ResultadoEjecucionEtapa(
            estado=ResultadoEjecucion.ESPERAR,
            motivo_espera=MotivoEspera.ENTREGABLE,
            datos={"entregable_id": entregable.pk},
        )


ESTRATEGIAS_POR_TIPO = {
    "INICIO": EstrategiaInicio(),
    "TAREA": EstrategiaTarea(),
    "APROBACION": EstrategiaAprobacion(),
    "ENTREGABLE": EstrategiaEntregable(),
    "CONDICION": EstrategiaCondicion(),
    "ESPERA": EstrategiaEspera(),
    "TICKET": EstrategiaSinConfiguracion(),
    "HITO": EstrategiaHito(),
    "FIN": EstrategiaFin(),
    # GACETA: sin entrada a propósito, Sprint 6, ver docstring del módulo.
    # TICKET: sigue sin ejecutable=True — no se habilita en 3.4.
}
