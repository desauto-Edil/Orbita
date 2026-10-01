"""Validación de `TransicionEtapa`/`WorkflowVersion` (CU-020, RQF-069).

`validar_integridad_transicion` protege cada fila de `TransicionEtapa` de
forma aislada, en `clean()`/`save()` (mismo criterio que
`apps/catalogo/reglas.py::validar_integridad_regla` para `ReglaCondicional`:
protege las vías ordinarias de dominio/Admin, no una escritura masiva vía
`QuerySet.update()`/`bulk_create()` ni acceso directo a la base de datos).

Deliberadamente compara `etapa.tipo` contra los valores literales de
`Etapa.Tipo` (cadenas estables, ej. `"FIN"`) en vez de importar la clase
`Etapa` — `apps/workflows/models.py` importa este módulo para usar
`validar_integridad_transicion` en `TransicionEtapa.save()`, así que
importar `Etapa` aquí crearía un ciclo. Mismo motivo por el que
`apps/catalogo/reglas.py` nunca importa `Campo`.

`validar_estructura` es la pasada holística sobre toda una `WorkflowVersion`,
invocada por `apps.workflows.versionamiento.activar_version` antes de
permitir BORRADOR→ACTIVA (RQF-069). Devuelve una lista de errores en vez de
lanzar una excepción — quien la invoque decide qué hacer con ellos (bloquear
la activación con un mensaje claro, listarlos en el Admin, etc.).

BFS simple sobre un diccionario de adyacencia para la alcanzabilidad — no se
usa ninguna librería de grafos (decisión aprobada, punto K/10): con decenas
de etapas por versión, una lista de adyacencia y una búsqueda en anchura
bastan, y una dependencia nueva no se justifica para esto.

3.2 (CU-020 "Ejecutar workflow") agrega a `validar_estructura` dos reglas
más, ambas necesarias para que el motor de ejecución tenga una semántica
inequívoca: disponibilidad unificada de tipos (W.5 — usa
`estrategia.ejecutable`, el mismo atributo que consulta el motor, nunca un
segundo criterio) y cardinalidad de salidas para etapas ordinarias (W.4).
"""

from django.core.exceptions import ValidationError

from apps.workflows.estrategias import ESTRATEGIAS_POR_TIPO
from apps.workflows.actores import validar_actor


def validar_integridad_transicion(transicion):
    """Valida una `TransicionEtapa` de forma aislada, antes de guardarla.

    Cubre: misma versión en origen/destino; FIN sin salidas; INICIO sin
    entradas; y la coherencia condicional/fallback según X.4 — una
    transición que sale de una CONDICION es *o* el fallback (sin
    variable/operador/valor) *o* una transición condicional completa (los
    tres presentes); para cualquier otra etapa de origen, esos cuatro
    campos deben permanecer vacíos/inactivos.
    """
    if transicion.etapa_origen_id is None or transicion.etapa_destino_id is None:
        return

    origen = transicion.etapa_origen
    destino = transicion.etapa_destino

    if destino.version_id != origen.version_id:
        raise ValidationError(
            "etapa_origen y etapa_destino deben pertenecer a la misma versión del workflow."
        )

    if origen.tipo == "FIN":
        raise ValidationError("Una etapa FIN no puede tener transiciones salientes.")

    if destino.tipo == "INICIO":
        raise ValidationError("Una etapa INICIO no puede tener transiciones entrantes.")

    if origen.tipo == "CONDICION":
        if transicion.es_fallback:
            if transicion.variable or transicion.operador or transicion.valor:
                raise ValidationError(
                    "La transición de fallback de una CONDICION no debe llevar "
                    "variable, operador ni valor."
                )
        else:
            if not (transicion.variable and transicion.operador and transicion.valor):
                raise ValidationError(
                    "Una transición condicional (no fallback) que sale de una CONDICION "
                    "requiere variable, operador y valor."
                )
        if transicion.resultado_aprobacion:
            raise ValidationError("resultado_aprobacion no aplica a una transición que sale de CONDICION.")
    elif origen.tipo == "APROBACION":
        # 3.4 (diseño aprobado) — semántica propia, independiente de
        # CONDICION: nunca variable/operador/valor/es_fallback, siempre
        # `resultado_aprobacion` con uno de los 3 valores cerrados.
        if transicion.es_fallback or transicion.variable or transicion.operador or transicion.valor:
            raise ValidationError(
                "Una transición que sale de una etapa APROBACION no debe llevar variable, "
                "operador, valor ni es_fallback — use resultado_aprobacion."
            )
        if not transicion.resultado_aprobacion:
            raise ValidationError(
                "Una transición que sale de una etapa APROBACION requiere resultado_aprobacion "
                "(APROBADA, RECHAZADA o DEVUELTA)."
            )
    else:
        if (
            transicion.es_fallback
            or transicion.variable
            or transicion.operador
            or transicion.valor
            or transicion.resultado_aprobacion
        ):
            raise ValidationError(
                "variable, operador, valor, es_fallback y resultado_aprobacion solo aplican a "
                "transiciones que salen de una etapa CONDICION o APROBACION, respectivamente."
            )


def validar_estructura(version):
    """Validaciones estructurales mínimas antes de activar `version`
    (RQF-069). No lanza excepción: devuelve la lista de errores encontrados
    (vacía si la versión es válida para activarse).
    """
    errores = []
    etapas = list(version.etapas.all())

    if not etapas:
        return ["La versión no tiene etapas."]

    inicios = [e for e in etapas if e.tipo == "INICIO"]
    fines = [e for e in etapas if e.tipo == "FIN"]

    if len(inicios) != 1:
        errores.append(f"Debe existir exactamente una etapa INICIO (hay {len(inicios)}).")
    if not fines:
        errores.append("Debe existir al menos una etapa FIN.")

    # INICIO sin entradas / FIN sin salidas — defensa adicional a clean()
    # (cubre datos escritos sin pasar por save(), ej. fixtures/migraciones).
    for inicio in inicios:
        if inicio.transiciones_entrantes.exists():
            errores.append(f"La etapa INICIO «{inicio.nombre}» no debe tener transiciones entrantes.")
    for fin in fines:
        if fin.transiciones_salientes.exists():
            errores.append(f"La etapa FIN «{fin.nombre}» no debe tener transiciones salientes.")

    # Transiciones dentro de la misma versión — defensa adicional a clean().
    for etapa in etapas:
        for transicion in etapa.transiciones_salientes.all():
            if transicion.etapa_destino.version_id != version.pk:
                errores.append(f"La transición «{transicion}» conecta etapas de versiones distintas.")

    # Strategy/configuración válida + tipos de etapa disponibles para activar.
    # `estrategia.ejecutable` es el ÚNICO criterio de disponibilidad (W.5,
    # 3.2, corrección aprobada): sin él, no se activa, sin importar si su
    # configuración es válida — evita que "validable" y "ejecutable" cuenten
    # dos historias distintas (APROBACION/GACETA no tienen ni siquiera
    # entrada en `ESTRATEGIAS_POR_TIPO`; TAREA/TICKET sí tienen entrada,
    # para validar su configuración vacía, pero `ejecutable=False` — ambos
    # casos producen el mismo mensaje).
    for etapa in etapas:
        estrategia = ESTRATEGIAS_POR_TIPO.get(etapa.tipo)
        if estrategia is not None:
            try:
                estrategia.validar_configuracion(etapa.configuracion or {})
            except ValidationError as exc:
                errores.append(f"La etapa «{etapa.nombre}»: {'; '.join(exc.messages)}")
        if estrategia is None or not estrategia.ejecutable:
            errores.append(
                f"La etapa «{etapa.nombre}» usa el tipo {etapa.tipo}, que todavía no está "
                "disponible para activar (reservado para un incremento futuro)."
            )

    # Cardinalidad de salidas (W.4, 3.2, corrección aprobada): toda etapa
    # ordinaria tiene exactamente 1 transición saliente; FIN ya se valida
    # sin salidas arriba; CONDICION se valida aparte (fallback único +
    # condicionales, más abajo) — exenta de esta regla porque su
    # naturaleza es tener varias. APROBACION/GACETA quedan sin regla de
    # cardinalidad hasta definir su semántica (no están en este conjunto).
    TIPOS_SALIDA_UNICA = {"INICIO", "TAREA", "ESPERA", "TICKET", "HITO"}
    for etapa in etapas:
        if etapa.tipo not in TIPOS_SALIDA_UNICA:
            continue
        cantidad = etapa.transiciones_salientes.count()
        if cantidad != 1:
            errores.append(
                f"La etapa «{etapa.nombre}» ({etapa.get_tipo_display()}) debe tener exactamente "
                f"una transición saliente (tiene {cantidad})."
            )

    # CONDICION: exactamente un fallback sin expresión, y al menos una
    # transición condicional real además de él (X.4).
    for etapa in etapas:
        if etapa.tipo != "CONDICION":
            continue
        salientes = list(etapa.transiciones_salientes.all())
        fallbacks = [t for t in salientes if t.es_fallback]
        condicionales = [t for t in salientes if not t.es_fallback]
        if len(fallbacks) != 1:
            errores.append(
                f"La etapa CONDICION «{etapa.nombre}» debe tener exactamente una transición de "
                f"fallback (tiene {len(fallbacks)})."
            )
        elif fallbacks[0].variable or fallbacks[0].operador or fallbacks[0].valor:
            errores.append(
                f"La transición de fallback de la etapa CONDICION «{etapa.nombre}» no debe "
                "llevar expresión condicional."
            )
        if not condicionales:
            errores.append(
                f"La etapa CONDICION «{etapa.nombre}» debe tener al menos una transición "
                "condicional además del fallback."
            )

    # APROBACION (3.4, diseño aprobado): exactamente una transición saliente
    # por cada resultado_aprobacion (APROBADA/RECHAZADA/DEVUELTA) — ni
    # faltante ni duplicada. No entra en TIPOS_SALIDA_UNICA (necesita 3
    # salidas, no 1) ni en la validación de CONDICION (semántica propia,
    # sin fallback).
    RESULTADOS_APROBACION = {"APROBADA", "RECHAZADA", "DEVUELTA"}
    for etapa in etapas:
        if etapa.tipo != "APROBACION":
            continue
        salientes = list(etapa.transiciones_salientes.all())
        por_resultado = {}
        for transicion in salientes:
            por_resultado.setdefault(transicion.resultado_aprobacion, []).append(transicion)
        for resultado in RESULTADOS_APROBACION:
            cantidad = len(por_resultado.get(resultado, []))
            if cantidad != 1:
                errores.append(
                    f"La etapa APROBACION «{etapa.nombre}» debe tener exactamente una transición "
                    f"con resultado_aprobacion={resultado} (tiene {cantidad})."
                )
        inesperados = set(por_resultado) - RESULTADOS_APROBACION
        if inesperados:
            errores.append(
                f"La etapa APROBACION «{etapa.nombre}» tiene transiciones con resultado_aprobacion "
                f"no reconocido: {sorted(inesperados)}."
            )

    # Los actores dinámicos se validan como definición aquí; la existencia
    # de su Ticket/responsable se resuelve recién al ejecutar cada etapa.
    for etapa in etapas:
        if etapa.tipo != "TAREA":
            continue
        configuracion = getattr(etapa, "configuracion_tarea", None)
        if configuracion is not None:
            try:
                validar_actor(
                    configuracion.tipo_responsable, usuario=configuracion.usuario_responsable,
                    equipo=configuracion.equipo_responsable, permite_vacio=True,
                )
            except ValidationError as exc:
                errores.append(f"La etapa «{etapa.nombre}»: {'; '.join(exc.messages)}")

    # Configuración relacional de APROBACION (a diferencia de TAREA, que
    # puede activarse sin `ConfiguracionEtapaTarea` — "sin responsable,
    # disponible para tomar" — un EsquemaAprobacion siempre necesita al
    # menos 1 participante para tener sentido; se exige aquí, no en la
    # Strategy, que solo valida `Etapa.configuracion` — vacía para
    # APROBACION igual que para TAREA). `getattr` en vez de importar el
    # modelo: evita un ciclo (`models.py` ya importa este módulo).
    for etapa in etapas:
        if etapa.tipo != "APROBACION":
            continue
        configuracion = getattr(etapa, "configuracion_aprobacion", None)
        if configuracion is None:
            errores.append(f"La etapa APROBACION «{etapa.nombre}» no tiene ConfiguracionEtapaAprobacion.")
            continue
        participantes = list(configuracion.participantes.all())
        if not participantes:
            errores.append(f"La etapa APROBACION «{etapa.nombre}» no tiene ningún participante configurado.")
        ordenes = [p.orden for p in participantes]
        if len(ordenes) != len(set(ordenes)):
            errores.append(f"La etapa APROBACION «{etapa.nombre}» tiene participantes con orden duplicado.")
        for participante in participantes:
            try:
                validar_actor(participante.tipo_aprobador, usuario=participante.usuario, equipo=participante.equipo)
            except ValidationError as exc:
                errores.append(f"La etapa «{etapa.nombre}»: {'; '.join(exc.messages)}")

    # Alcanzabilidad desde INICIO (BFS simple) — huérfanas y FIN alcanzable.
    if len(inicios) == 1:
        adyacencia = {
            etapa.pk: [t.etapa_destino_id for t in etapa.transiciones_salientes.all()] for etapa in etapas
        }
        visitados = {inicios[0].pk}
        pendientes = [inicios[0].pk]
        while pendientes:
            actual = pendientes.pop()
            for siguiente in adyacencia.get(actual, []):
                if siguiente not in visitados:
                    visitados.add(siguiente)
                    pendientes.append(siguiente)

        huerfanas = [e for e in etapas if e.pk not in visitados]
        if huerfanas:
            nombres = ", ".join(f"«{e.nombre}»" for e in huerfanas)
            errores.append(f"Las siguientes etapas no son alcanzables desde INICIO: {nombres}.")

        if not any(fin.pk in visitados for fin in fines):
            errores.append("Ninguna etapa FIN es alcanzable desde INICIO.")

    return errores
