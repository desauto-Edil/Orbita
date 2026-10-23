"""Pruebas de la Fase 4.B — Studio como aplicación y constructor visual del flujo.

Cubren: el Application Shell (cinco secciones, un único estado por sección, estado de guardado y barra de acciones),
Básico por grupos (campos asociados al mismo formulario), el Formulario como constructor (propiedades y
reordenamiento seguro de campos), Salida por categorías (gaceta por pasos, selección múltiple, condiciones de cierre
agrupadas), el lienzo (columnas por fase, tarjetas, conexiones REALES de `flujo_api`, conexiones de fase que no se
ejecutan, contenedores), las posiciones visuales (persistencia, validación, permisos y que NO alteran la ejecución
ni se pierden al reconciliar), cambiar una actividad de fase (dominio, borrador, auditoría, contenedores), la
revisión y publicación unificada, LEGACY y el sistema visual (tokens neutros y ORBITA_DESIGN.md).

Se entregan SIN ejecutar (ver los comandos en el informe de entrega)."""

import json
from pathlib import Path

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.catalogo import flujo_api, lienzo
from apps.catalogo.configuracion_ejecucion import (
    activar_configuracion_ejecucion,
    agregar_bloque_operativo,
    cambiar_fase_bloque_operativo,
    conectar_bloques_operativos,
    preparar_configuracion_ejecucion,
)
from apps.catalogo.models import Campo, PosicionVisualBloque, ProgramacionProceso, Servicio
from apps.catalogo.tests import _permiso_get_or_create
from apps.catalogo.tests_fase4a import CLAVE, Usuario, _Base4A
from apps.catalogo.versionamiento import crear_nueva_version, mover_campo
from apps.core.models import RegistroAuditoria

RAIZ = Path(settings.BASE_DIR)


class _Base4B(_Base4A):
    def _eventos(self, objeto):
        return RegistroAuditoria.objects.filter(
            content_type=ContentType.objects.get_for_model(type(objeto)), object_id=objeto.pk
        )

    def _aprobacion(self, config, fase, nombre="Aprobar", **extra):
        return agregar_bloque_operativo(
            config, self.admin, fase=fase, tipo="APROBACION", nombre=nombre,
            configuracion={"modo": "SECUENCIAL", "participantes": [{"tipo": "USUARIO", "usuario_id": self.persona.pk}]},
            **extra,
        )

    def _posiciones(self, servicio, posiciones):
        return self.client.post(
            reverse("catalogo:studio_lienzo_posiciones", args=[servicio.pk]),
            data=json.dumps({"posiciones": posiciones}), content_type="application/json",
        )


# --- Application Shell -------------------------------------------------------------------------------------------


class ShellDelStudioTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()
        self.servicio = self._servicio()

    def test_las_cinco_secciones_viven_en_el_mismo_shell(self):
        for tab in ("general", "entrada", "salida", "ejecucion", "publicacion"):
            pagina = self.client.get(self._url(self.servicio, tab))
            self.assertEqual(pagina.status_code, 200, tab)
            for marca in ('data-studio', 'class="studio-app__cabecera"', 'class="studio-app__nav"', 'id="studio-panel"',
                          "studio-app__pie", "data-estado-guardado"):
                self.assertContains(pagina, marca, msg_prefix=tab)
            self.assertEqual([s["clave"] for s in pagina.context["secciones"]],
                             ["general", "entrada", "salida", "ejecucion", "publicacion"])

    def test_un_unico_estado_por_seccion_sin_texto_duplicado(self):
        pagina = self.client.get(self._url(self.servicio, "general"))
        contenido = pagina.content.decode()
        self.assertNotIn("service-step__state", contenido)  # el texto largo pasó al `title`
        nav = contenido.split('class="studio-app__nav"')[1].split("</nav>")[0]
        for seccion in pagina.context["secciones"]:
            self.assertEqual(nav.count(f'>{seccion["estado_etiqueta"]}</span>'),
                             sum(1 for s in pagina.context["secciones"] if s["estado_etiqueta"] == seccion["estado_etiqueta"]))

    def test_guardar_del_pie_existe_salvo_en_publicar_y_empieza_deshabilitado(self):
        self.assertContains(self.client.get(self._url(self.servicio, "general")), "data-guardar disabled")
        self.assertNotContains(self.client.get(self._url(self.servicio, "publicacion")), "data-guardar")

    def test_el_estudio_no_duplica_el_dock_ni_el_header(self):
        pagina = self.client.get(self._url(self.servicio, "general")).content.decode()
        self.assertEqual(pagina.count('class="dock"'), 1)
        self.assertEqual(pagina.count('<header class="app-header">'), 1)


# --- Básico por grupos -------------------------------------------------------------------------------------------


class BasicoPorGruposTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()
        self.servicio = self._servicio()

    def test_grupos_y_campos_asociados_al_mismo_formulario(self):
        pagina = self.client.get(self._url(self.servicio, "general"))
        for grupo in ("identidad", "responsables", "visibilidad", "tiempos"):
            self.assertContains(pagina, f'data-subnav-ir="{grupo}"')
            self.assertContains(pagina, f'data-subpanel="{grupo}"')
        for nombre in ("nombre", "categoria", "tipo", "descripcion", "instrucciones", "alcance_visibilidad"):
            self.assertIn('form="form-general-servicio"', str(pagina.context["form_general"][nombre]))

    def test_un_proceso_muestra_el_grupo_inicio(self):
        proceso = self._servicio("Proceso", tipo=Servicio.Tipo.PROCESO)
        pagina = self.client.get(self._url(proceso, "general"))
        self.assertContains(pagina, 'data-subnav-ir="inicio"')
        self.assertContains(pagina, "¿Cómo se inicia este proceso?")
        self.assertIn('form="form-general-servicio"', str(pagina.context["form_inicio"]["modo"]))

    def test_guardar_sigue_siendo_una_sola_operacion(self):
        respuesta = self.client.post(reverse("catalogo:studio_general_guardar", args=[self.servicio.pk]), {
            "nombre": "Nombre nuevo", "categoria": self.categoria.pk, "tipo": "SERVICIO", "descripcion": "",
            "instrucciones": "", "alcance_visibilidad": "PUBLICO_INTERNO",
        })
        self.assertEqual(respuesta.status_code, 302)
        self.servicio.refresh_from_db()
        self.assertEqual(self.servicio.nombre, "Nombre nuevo")


# --- Formulario como constructor ---------------------------------------------------------------------------------


class ConstructorDeFormularioTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()
        self.servicio = self._servicio()
        self.borrador = crear_nueva_version(self.servicio.formulario, self.admin)
        self.a = Campo.objects.create(version=self.borrador, tipo="TEXTO", etiqueta="A", orden=1)
        self.b = Campo.objects.create(version=self.borrador, tipo="TEXTO", etiqueta="B", orden=2)
        self.c = Campo.objects.create(version=self.borrador, tipo="TEXTO", etiqueta="C", orden=3)

    def _orden(self):
        return list(Campo.objects.filter(version=self.borrador).order_by("orden", "id").values_list("etiqueta", flat=True))

    def test_estructura_campos_y_propiedades(self):
        pagina = self.client.get(self._url(self.servicio, "entrada"))
        self.assertContains(pagina, "data-constructor")
        self.assertContains(pagina, "data-propiedades")
        for campo in (self.a, self.b, self.c):
            self.assertContains(pagina, f'data-propiedad="propiedad-campo-{campo.pk}"')
        self.assertContains(pagina, 'data-subnav-ir="reglas"')
        self.assertContains(pagina, "distintivo--campo")

    def test_mover_un_campo_es_seguro_y_auditado(self):
        respuesta = self.client.post(
            reverse("catalogo:studio_campo_mover", args=[self.servicio.pk, self.c.pk]), {"direccion": "subir"}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(self._orden(), ["A", "C", "B"])
        self.assertTrue(self._eventos(self.c).filter(accion=RegistroAuditoria.Accion.ACTUALIZAR).exists())
        mover_campo(self.borrador, Campo.objects.get(pk=self.a.pk), self.admin, direccion=-1)  # borde: nada
        self.assertEqual(self._orden(), ["A", "C", "B"])

    def test_no_se_mueve_un_campo_de_una_version_activa(self):
        from apps.catalogo.versionamiento import activar_version

        activar_version(self.servicio.formulario, self.borrador, self.admin)
        respuesta = self.client.post(
            reverse("catalogo:studio_campo_mover", args=[self.servicio.pk, self.c.pk]), {"direccion": "subir"}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.borrador.refresh_from_db()
        self.assertEqual(list(Campo.objects.filter(version=self.borrador).order_by("orden").values_list("etiqueta", flat=True)), ["A", "B", "C"])

    def test_mover_exige_el_permiso_de_formularios(self):
        limitado = Usuario.objects.create_user("f4b_limitado", password=CLAVE)
        _permiso_get_or_create(limitado, "catalogo.administrar", "Rol 4B limitado")
        self.client.login(username="f4b_limitado", password=CLAVE)
        respuesta = self.client.post(
            reverse("catalogo:studio_campo_mover", args=[self.servicio.pk, self.c.pk]), {"direccion": "subir"}
        )
        self.assertEqual(respuesta.status_code, 403)


# --- Salida por categorías ---------------------------------------------------------------------------------------


class SalidaPorCategoriasTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()

    def test_servicio_y_proceso_ven_sus_categorias(self):
        servicio = self._servicio()
        pagina = self.client.get(self._url(servicio, "salida"))
        self.assertContains(pagina, 'data-subnav-ir="resultados"')
        self.assertContains(pagina, 'data-subnav-ir="entrega"')
        self.assertNotContains(pagina, 'data-subnav-ir="gacetas"')
        proceso = self._servicio("Proceso", tipo=Servicio.Tipo.PROCESO)
        pagina = self.client.get(self._url(proceso, "salida"))
        for categoria in ("resultados", "revisiones", "gacetas", "finalizacion", "entrega"):
            self.assertContains(pagina, f'data-subnav-ir="{categoria}"')

    def test_gaceta_por_pasos_seleccion_multiple_y_cierre_agrupado(self):
        proceso = self._servicio("Proceso", tipo=Servicio.Tipo.PROCESO)
        pagina = self.client.get(self._url(proceso, "salida"))
        self.assertContains(pagina, "data-pasos")
        for paso in ("Identidad y colecciones", "Ingreso y apertura", "Participantes", "Revisión y aprobación",
                     "Coordinación y cierre", "Opciones avanzadas"):
            self.assertContains(pagina, f'data-paso="{paso}"')
        self.assertContains(pagina, "data-multiselect")
        for grupo in ("Actividades", "Recorridos", "Aprobaciones", "Gacetas", "Alcance de colecciones",
                      "Resultados finales de los ítems"):
            self.assertContains(pagina, f"<legend>{grupo}</legend>")


# --- Lienzo ------------------------------------------------------------------------------------------------------


class LienzoTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()
        self.workflow = self._flujo("Flujo lienzo", fases=("Recepción", "Revisión", "Cierre"))
        self.servicio = self._servicio("Con lienzo", workflow=self.workflow)
        self.config = preparar_configuracion_ejecucion(self.servicio, self.admin)
        self.recepcion = self._fase(self.workflow, "Recepción")
        self.revision = self._fase(self.workflow, "Revisión")
        self.hacer = self._actividad(self.config, self.recepcion, "Hacer")
        self.revisar = self._actividad(self.config, self.recepcion, "Revisar")
        self.aprobar = self._aprobacion(self.config, self.revision)
        conectar_bloques_operativos(self.aprobar, None, self.admin, resultado_aprobacion="APROBADA", finaliza=True)
        conectar_bloques_operativos(self.aprobar, None, self.admin, resultado_aprobacion="RECHAZADA", finaliza=True)
        conectar_bloques_operativos(self.aprobar, self.hacer, self.admin, resultado_aprobacion="DEVUELTA")

    def _datos(self):
        return lienzo.datos(self.servicio, self.config)

    def test_fases_como_columnas_y_actividades_como_tarjetas(self):
        datos = self._datos()
        self.assertEqual([c["fase"].nombre for c in datos["columnas"]], ["Recepción", "Revisión", "Cierre"])
        recepcion = datos["columnas"][0]
        self.assertEqual([(t.bloque.nombre, t.paso) for t in recepcion["tarjetas"]], [("Hacer", 1), ("Revisar", 2)])
        self.assertEqual([t.y for t in recepcion["tarjetas"]], [0, lienzo.ALTO_TARJETA])
        self.assertEqual(datos["columnas"][1]["tarjetas"][0].tipo, "Aprobación")

    def test_las_conexiones_son_las_que_ejecuta_el_motor(self):
        conexiones = self._datos()["conexiones"]
        par = {(c["de"], c["a"], c["estilo"]) for c in conexiones}
        self.assertIn((f"nodo-{self.hacer.pk}", f"nodo-{self.revisar.pk}", "secuencia"), par)
        self.assertIn((f"nodo-{self.revisar.pk}", f"nodo-{self.aprobar.pk}", "secuencia"), par)  # cambio de fase
        self.assertIn((f"nodo-{self.aprobar.pk}", "nodo-fin", "aprobada"), par)
        self.assertIn((f"nodo-{self.aprobar.pk}", "nodo-fin", "rechazada"), par)
        self.assertIn((f"nodo-{self.aprobar.pk}", f"nodo-{self.hacer.pk}", "devuelta"), par)
        # Lo que se dibuja coincide con flujo_api (no hay un grafo visual aparte).
        self.assertEqual(len(conexiones), len([c for c in flujo_api.conexiones(self.config) if c["origen"].contenedor_id is None]))

    def test_una_aprobacion_sin_rutas_no_aparenta_continuar(self):
        sin_rutas = self._aprobacion(self.config, self.revision, nombre="Sin rutas")
        conexiones = lienzo.conexiones(self.config)
        self.assertFalse([c for c in conexiones if c["de"] == f"nodo-{sin_rutas.pk}"])

    def test_decision_con_condicion_y_otro_caso(self):
        decision = agregar_bloque_operativo(self.config, self.admin, fase=self.revision, tipo="DECISION", nombre="¿Seguir?")
        conectar_bloques_operativos(decision, None, self.admin, variable="x", operador="IGUAL_A", valor="1", finaliza=True)
        conectar_bloques_operativos(decision, self.hacer, self.admin, es_fallback=True)
        estilos = {c["estilo"] for c in lienzo.conexiones(self.config) if c["de"] == f"nodo-{decision.pk}"}
        self.assertEqual(estilos, {"condicion", "otro_caso"})

    def test_las_conexiones_de_fase_que_no_se_ejecutan_se_marcan(self):
        from apps.workflows import fases as fases_ops
        from apps.workflows.versionamiento import activar_version as activar_wf

        workflow = fases_ops.crear_plantilla_fases(self.admin, nombre="Flujo con salida extra")
        version = workflow.versiones.get(numero=1)
        uno, dos, tres = (fases_ops.agregar_fase(version, self.admin, nombre=n) for n in ("Uno", "Dos", "Tres"))
        fases_ops.conectar_fases(uno, dos, self.admin, prioridad=0)
        fases_ops.conectar_fases(uno, tres, self.admin, prioridad=5)  # el motor solo sigue la primera
        fases_ops.conectar_fases(dos, tres, self.admin)
        activar_wf(workflow, version, self.admin)
        workflow.refresh_from_db()
        servicio = self._servicio("Con salida extra", workflow=workflow)
        datos = lienzo.datos(servicio, preparar_configuracion_ejecucion(servicio, self.admin))
        salidas = datos["columnas"][0]["salidas"]
        self.assertEqual([(s["destino"].nombre, s["ejecuta"]) for s in salidas], [("Dos", True), ("Tres", False)])
        self.assertEqual(datos["fases_extra"], [{"de": f"fase-{uno.pk}", "a": f"fase-{tres.pk}", "nombre": "Tres"}])

    def test_un_contenedor_muestra_sus_pasos_y_no_cruza_ambitos(self):
        grupo = agregar_bloque_operativo(self.config, self.admin, fase=self.revision, tipo="GRUPO_ACTIVIDADES", nombre="Juntas")
        interna = agregar_bloque_operativo(
            self.config, self.admin, fase=self.revision, tipo="ACTIVIDAD", nombre="Interna", contenedor=grupo,
            rama="GENERAL", configuracion={"tipo_actor": "USUARIO", "usuario_id": self.persona.pk},
        )
        tarjeta = next(t for t in self._datos()["columnas"][1]["tarjetas"] if t.bloque.pk == grupo.pk)
        self.assertEqual([[p.bloque.pk for p in pasos] for _e, pasos in tarjeta.ramas], [[interna.pk]])
        self.assertFalse([c for c in lienzo.conexiones(self.config) if c["de"] == f"nodo-{interna.pk}"])

    def test_la_pagina_dibuja_el_lienzo_y_el_inspector(self):
        pagina = self.client.get(self._url(self.servicio, "ejecucion"))
        for marca in ("data-lienzo", 'id="lienzo-datos"', f'id="nodo-{self.hacer.pk}"', f'id="fase-{self.recepcion.pk}"',
                      f'id="inspector-bloque-{self.hacer.pk}"', f'id="inspector-fase-{self.recepcion.pk}"',
                      'id="nodo-fin"', "lienzo.js", "Leyenda"):
            self.assertContains(pagina, marca)
        datos = json.loads(pagina.content.decode().split('id="lienzo-datos" type="application/json">')[1].split("</script>")[0])
        self.assertTrue(datos["conexiones"])
        self.assertEqual(set(datos["conexiones"][0]), {"id", "de", "a", "estilo", "etiqueta"})


# --- Posiciones visuales -----------------------------------------------------------------------------------------


class PosicionesVisualesTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()
        self.workflow = self._flujo()
        self.servicio = self._servicio(workflow=self.workflow)
        self.config = preparar_configuracion_ejecucion(self.servicio, self.admin)
        self.recepcion = self._fase(self.workflow, "Recepción")
        self.a = self._actividad(self.config, self.recepcion, "A")
        self.b = self._actividad(self.config, self.recepcion, "B")

    def test_se_guardan_y_se_leen_por_clave_estable(self):
        respuesta = self._posiciones(self.servicio, [{"clave": self.b.clave, "x": 0, "y": 10}])
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json()["guardadas"], 1)
        tarjetas = lienzo.datos(self.servicio, self.config)["columnas"][0]["tarjetas"]
        self.assertEqual({t.bloque.nombre: t.y for t in tarjetas}["B"], 10)
        self._posiciones(self.servicio, [{"clave": self.b.clave, "x": 0, "y": 40}])
        self.assertEqual(PosicionVisualBloque.objects.get(servicio=self.servicio, clave=self.b.clave).y, 40)

    def test_la_posicion_visual_no_cambia_la_ejecucion(self):
        antes = [(c["origen"].pk, c["destino"].pk if c["destino"] else None) for c in flujo_api.conexiones(self.config)]
        self._posiciones(self.servicio, [{"clave": self.b.clave, "y": 0}, {"clave": self.a.clave, "y": 500}])
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertLess(self.a.orden, self.b.orden)  # el orden de ejecución no cambia
        despues = [(c["origen"].pk, c["destino"].pk if c["destino"] else None) for c in flujo_api.conexiones(self.config)]
        self.assertEqual(antes, despues)
        tarjetas = lienzo.datos(self.servicio, self.config)["columnas"][0]["tarjetas"]
        self.assertEqual([t.paso for t in tarjetas], [1, 2])  # «Paso N» sigue el orden de ejecución

    def test_se_validan_las_posiciones(self):
        self.assertEqual(self._posiciones(self.servicio, [{"clave": "ajena", "y": 1}]).status_code, 400)
        self.assertEqual(self._posiciones(self.servicio, [{"clave": self.a.clave, "y": "no"}]).status_code, 400)
        self.assertEqual(self._posiciones(self.servicio, [{"clave": self.a.clave, "y": -5}]).status_code, 400)
        respuesta = self.client.post(
            reverse("catalogo:studio_lienzo_posiciones", args=[self.servicio.pk]), data="no-json",
            content_type="application/json",
        )
        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(self.client.get(reverse("catalogo:studio_lienzo_posiciones", args=[self.servicio.pk])).status_code, 405)
        otro = self._servicio("Otro", workflow=self.workflow)
        self.assertEqual(self._posiciones(otro, [{"clave": self.a.clave, "y": 1}]).status_code, 400)
        self.assertFalse(PosicionVisualBloque.objects.exists())

    def test_exige_el_permiso_de_flujos(self):
        limitado = Usuario.objects.create_user("f4b_sin_flujos", password=CLAVE)
        _permiso_get_or_create(limitado, "catalogo.administrar", "Rol 4B sin flujos")
        self.client.login(username="f4b_sin_flujos", password=CLAVE)
        self.assertEqual(self._posiciones(self.servicio, [{"clave": self.a.clave, "y": 1}]).status_code, 403)

    def test_las_posiciones_sobreviven_a_otra_version_de_la_configuracion(self):
        self._posiciones(self.servicio, [{"clave": self.b.clave, "y": 77}])
        activar_configuracion_ejecucion(self.servicio, self.config, self.admin)
        self.servicio.refresh_from_db()
        nueva = preparar_configuracion_ejecucion(self.servicio, self.admin)
        tarjetas = lienzo.datos(self.servicio, nueva)["columnas"][0]["tarjetas"]
        self.assertEqual({t.bloque.nombre: t.y for t in tarjetas}["B"], 77)


# --- Cambiar de fase ---------------------------------------------------------------------------------------------


class CambiarDeFaseTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()
        self.workflow = self._flujo()
        self.servicio = self._servicio(workflow=self.workflow)
        self.config = preparar_configuracion_ejecucion(self.servicio, self.admin)
        self.recepcion = self._fase(self.workflow, "Recepción")
        self.revision = self._fase(self.workflow, "Revisión")
        self.a = self._actividad(self.config, self.recepcion, "A")
        self.b = self._actividad(self.config, self.revision, "B")

    def test_queda_al_final_de_la_fase_destino_y_se_audita(self):
        respuesta = self.client.post(
            reverse("catalogo:studio_bloque_fase", args=[self.servicio.pk, self.a.pk]), {"fase_id": self.revision.pk}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.a.refresh_from_db()
        self.assertEqual(self.a.fase_id, self.revision.pk)
        self.assertGreater(self.a.orden, self.b.orden)
        evento = self._eventos(self.a).latest("pk")
        self.assertEqual(evento.datos_anteriores["fase_id"], self.recepcion.pk)

    def test_un_contenedor_se_mueve_con_sus_pasos(self):
        grupo = agregar_bloque_operativo(self.config, self.admin, fase=self.recepcion, tipo="GRUPO_ACTIVIDADES", nombre="G")
        interna = agregar_bloque_operativo(
            self.config, self.admin, fase=self.recepcion, tipo="ACTIVIDAD", nombre="I", contenedor=grupo, rama="GENERAL",
            configuracion={"tipo_actor": "USUARIO", "usuario_id": self.persona.pk},
        )
        cambiar_fase_bloque_operativo(self.config, grupo, self.admin, fase=self.revision)
        interna.refresh_from_db()
        self.assertEqual(interna.fase_id, self.revision.pk)
        with self.assertRaises(ValidationError):
            cambiar_fase_bloque_operativo(self.config, interna, self.admin, fase=self.recepcion)

    def test_solo_en_borrador(self):
        activar_configuracion_ejecucion(self.servicio, self.config, self.admin)
        with self.assertRaises(ValidationError):
            cambiar_fase_bloque_operativo(self.config, self.a, self.admin, fase=self.revision)
        self.a.refresh_from_db()
        self.assertEqual(self.a.fase_id, self.recepcion.pk)

    def test_misma_fase_no_hace_nada_y_otra_version_se_rechaza(self):
        orden = self.a.orden
        cambiar_fase_bloque_operativo(self.config, self.a, self.admin, fase=self.recepcion)
        self.a.refresh_from_db()
        self.assertEqual(self.a.orden, orden)
        ajeno = self._flujo("Otro flujo")
        with self.assertRaises(ValidationError):
            cambiar_fase_bloque_operativo(self.config, self.a, self.admin, fase=ajeno.version_activa.fases.first())

    def test_exige_el_permiso_de_flujos(self):
        limitado = Usuario.objects.create_user("f4b_sin_fase", password=CLAVE)
        _permiso_get_or_create(limitado, "catalogo.administrar", "Rol 4B sin fase")
        self.client.login(username="f4b_sin_fase", password=CLAVE)
        respuesta = self.client.post(
            reverse("catalogo:studio_bloque_fase", args=[self.servicio.pk, self.a.pk]), {"fase_id": self.revision.pk}
        )
        self.assertEqual(respuesta.status_code, 403)


# --- Revisión y publicación unificada -----------------------------------------------------------------------------


class RevisionUnificadaTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()

    def _filas(self, servicio):
        return {f["nombre"]: f for f in self.client.get(self._url(servicio, "publicacion")).context["verificacion"]}

    def test_una_lista_con_listo_atencion_y_opcional(self):
        servicio = self._servicio()
        filas = self._filas(servicio)
        self.assertEqual(filas["Formulario de entrada"]["estado"], "listo")
        self.assertEqual(filas["Flujo"]["estado"], "opcional")
        self.assertEqual(filas["Responsables"]["estado"], "opcional")
        self.assertEqual(filas["Entrega al solicitante"]["estado"], "opcional")
        sin_formulario = Servicio.objects.create(nombre="Sin formulario", categoria=self.categoria)
        filas = self._filas(sin_formulario)
        self.assertEqual(filas["Formulario de entrada"]["estado"], "atencion")
        self.assertIn("tab=entrada", filas["Formulario de entrada"]["url"])

    def test_una_sola_accion_principal_y_resumen_compacto(self):
        servicio = self._servicio()
        pagina = self.client.get(self._url(servicio, "publicacion"))
        self.assertContains(pagina, "Revisión y publicación")
        self.assertEqual(pagina.content.decode().count(reverse("catalogo:studio_publicar", args=[servicio.pk])), 1)
        cabecera = pagina.context["cabecera_revision"]
        self.assertEqual((cabecera["tipo"], cabecera["flujo"]), ("Servicio", "Sin flujo"))

    def test_un_proceso_resume_sus_piezas_en_la_misma_lista(self):
        proceso = self._servicio("Proceso", tipo=Servicio.Tipo.PROCESO)
        filas = self._filas(proceso)
        for nombre in ("Modalidad de inicio", "Datos de inicio", "Flujo asociado"):
            self.assertIn(nombre, filas)
        self.assertEqual(filas["Flujo asociado"]["estado"], "atencion")
        pagina = self.client.get(self._url(proceso, "publicacion"))
        self.assertEqual(pagina.context["cabecera_revision"]["modalidad"], "Manual")


# --- LEGACY y sistema visual ---------------------------------------------------------------------------------------


class CompatibilidadYSistemaVisualTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()

    def test_un_flujo_legacy_no_usa_el_lienzo(self):
        from apps.workflows.models import Workflow

        legado = Workflow.objects.create(nombre="Legado 4B", modo=Workflow.Modo.LEGACY_EJECUTABLE)
        servicio = self._servicio("Legado", workflow=legado)
        pagina = self.client.get(self._url(servicio, "ejecucion"))
        self.assertEqual(pagina.status_code, 200)
        self.assertNotContains(pagina, "data-lienzo")
        self.assertTrue(self.client.get(self._url(servicio, "publicacion")).context["es_legacy"])

    def test_fondos_neutros_y_documento_de_diseno(self):
        tokens = (RAIZ / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
        self.assertIn("--surface-page: var(--palette-gray-50);", tokens)
        self.assertIn("--color-primary: var(--palette-lime-500);", tokens)  # el lima corporativo no cambia
        self.assertTrue((RAIZ / "docs" / "design" / "ORBITA_DESIGN.md").exists())
        estudio = (RAIZ / "static" / "css" / "studio.css").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", estudio)
        self.assertIn("--dock-reserve", estudio)


# --- Ajustes UX 4.B: creación por tarjetas y Básico como asistente -----------------------------------------------


class CreacionPorTarjetasTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()

    def _crear(self, **datos):
        base = {"nombre": "Nuevo 4B", "categoria": self.categoria.pk}
        base.update(datos)
        return self.client.post(reverse("core:disenador_servicio_crear"), base)

    def _programado(self, **extra):
        datos = {"modo": "PROGRAMADO", "frecuencia": "MENSUAL", "dia_creacion": "10", "periodo": "MES_SIGUIENTE",
                 "responsable_inicial": f"U:{self.persona.pk}"}
        datos.update(extra)
        return datos

    def test_estado_seleccion_con_dos_tarjetas_y_sin_tipo_preseleccionado(self):
        contenido = self.client.get(reverse("core:disenador_servicio_crear")).content.decode()
        self.assertIn('data-crear-estado="seleccion"', contenido)
        self.assertIn("¿Qué quieres crear?", contenido)
        self.assertIn('name="tipo" value="SERVICIO" required>', contenido)
        self.assertIn('name="tipo" value="PROCESO" required>', contenido)  # ninguno marcado
        self.assertIn("Resolvamos un problema puntual", contenido)
        self.assertIn("Elaboremos un plan de trabajo", contenido)
        self.assertEqual(contenido.count('class="opcion-tarjeta"'), 4)
        # Proceso: «¿Cómo inicia?» con Manual / Programado, oculto y sin enviarse mientras no se elija Proceso.
        self.assertIn("data-solo-proceso hidden", contenido)
        self.assertIn('name="modo" value="MANUAL" checked', contenido)
        self.assertIn("Cuando alguien lo necesite", contenido)
        self.assertIn("Automáticamente según una programación", contenido)

    def test_iconografia_lucide_en_el_sprite(self):
        contenido = self.client.get(reverse("core:disenador_servicio_crear")).content.decode()
        for icono in ("hand-helping", "workflow", "mouse-pointer-click", "calendar-clock"):
            self.assertIn(f'<symbol id="i-{icono}"', contenido)
            self.assertIn(f'href="#i-{icono}"', contenido)

    def test_una_sola_vista_un_formulario_y_una_accion_principal(self):
        contenido = self.client.get(reverse("core:disenador_servicio_crear")).content.decode()
        self.assertEqual(contenido.count('action="' + reverse("core:disenador_servicio_crear") + '"'), 1)
        self.assertIn("data-envio-unico", contenido)
        formulario = contenido.split('<form method="post" action="' + reverse("core:disenador_servicio_crear"))[1].split("</form>")[0]
        self.assertEqual(formulario.count("btn btn--primary"), 1)
        self.assertIn(">Crear borrador</button>", contenido)
        self.assertIn(">Cancelar</a>", contenido)
        codigo = (RAIZ / "static" / "js" / "studio.js").read_text(encoding="utf-8")
        self.assertIn("function iniciarEnvioUnico()", codigo)  # un segundo envío se ignora
        self.assertIn('"data-enviando"', codigo)
        self.assertIn("function iniciarCrear()", codigo)  # seleccion → configuracion sin recargar

    def test_un_error_vuelve_en_configuracion_y_conserva_los_datos_comunes(self):
        respuesta = self._crear(tipo="PROCESO", nombre="Plan mensual", descripcion="Texto que se conserva",
                                categoria="", categoria_nueva="")
        self.assertEqual(respuesta.status_code, 200)
        contenido = respuesta.content.decode()
        self.assertIn('data-crear-estado="configuracion"', contenido)
        self.assertIn('value="Plan mensual"', contenido)
        self.assertIn("Texto que se conserva", contenido)
        self.assertIn('name="tipo" value="PROCESO" required checked', contenido)
        self.assertFalse(Servicio.objects.filter(nombre="Plan mensual").exists())

    def test_cambiar_a_servicio_no_aplica_lo_del_proceso(self):
        # Lo de un Proceso que llegue con un Servicio (p. ej. sin JS) se ignora: nada de programación.
        self._crear(tipo="SERVICIO", **self._programado())
        servicio = Servicio.objects.get(nombre="Nuevo 4B")
        self.assertEqual(servicio.tipo, "SERVICIO")
        self.assertFalse(ProgramacionProceso.objects.filter(servicio=servicio).exists())

    def test_crear_un_servicio(self):
        respuesta = self._crear(tipo="SERVICIO")
        servicio = Servicio.objects.get(nombre="Nuevo 4B")
        self.assertRedirects(respuesta, self._url(servicio, "general", asistente=1), fetch_redirect_response=False)
        self.assertEqual((servicio.tipo, servicio.activo), ("SERVICIO", False))
        self.assertFalse(ProgramacionProceso.objects.filter(servicio=servicio).exists())

    def test_crear_un_proceso_manual(self):
        self._crear(tipo="PROCESO", modo="MANUAL")
        proceso = Servicio.objects.get(nombre="Nuevo 4B")
        self.assertEqual(proceso.tipo, "PROCESO")
        self.assertFalse(ProgramacionProceso.objects.filter(servicio=proceso).exists())

    def test_crear_un_proceso_programado_persiste_la_modalidad(self):
        self._crear(tipo="PROCESO", **self._programado())
        proceso = Servicio.objects.get(nombre="Nuevo 4B")
        programacion = ProgramacionProceso.objects.get(servicio=proceso)
        self.assertEqual((programacion.activa, programacion.dia_creacion), (True, 10))
        self.assertEqual(programacion.responsable_inicial.usuario, self.persona)
        self.assertTrue(proceso.datos_inicio_en_ejecucion)
        # Básico › Inicio muestra la MISMA modalidad (no hay una segunda configuración).
        basico = self.client.get(self._url(proceso, "general"))
        self.assertEqual(basico.context["form_inicio"]["modo"].value(), "PROGRAMADO")
        self.assertTrue(basico.context["programacion_activa"])

    def test_programado_incompleto_no_crea_nada_y_conserva_la_eleccion(self):
        respuesta = self._crear(tipo="PROCESO", modo="PROGRAMADO", frecuencia="MENSUAL")
        self.assertEqual(respuesta.status_code, 200)
        self.assertFalse(Servicio.objects.filter(nombre="Nuevo 4B").exists())
        contenido = respuesta.content.decode()
        self.assertIn('name="tipo" value="PROCESO" required checked', contenido)
        self.assertIn('name="modo" value="PROGRAMADO" checked', contenido)

    def test_sin_tipo_no_se_crea(self):
        respuesta = self._crear()
        self.assertEqual(respuesta.status_code, 200)
        self.assertFalse(Servicio.objects.filter(nombre="Nuevo 4B").exists())


class BasicoComoAsistenteTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()
        self.servicio = self._servicio()
        self.proceso = self._servicio("Proceso asistente", tipo=Servicio.Tipo.PROCESO)

    def test_asistente_con_anterior_continuar_y_guardado_integrado(self):
        pagina = self.client.get(self._url(self.proceso, "general"))
        for marca in ("data-subnav-asistente", "data-subnav-anterior", "data-subnav-siguiente", "data-subnav-posicion"):
            self.assertContains(pagina, marca)
        # Los tres «Guardar» del formulario general se integran a Continuar (sin JS siguen visibles).
        self.assertContains(pagina, "data-guardar-integrado", count=3)
        # Tiempos y prórrogas son operaciones propias: conservan su botón.
        self.assertContains(pagina, "Guardar tiempo objetivo")
        self.assertContains(pagina, "Guardar política de prórroga")
        orden = [pagina.content.decode().index(f'data-subnav-ir="{g}"') for g in
                 ("identidad", "inicio", "responsables", "visibilidad", "tiempos")]
        self.assertEqual(orden, sorted(orden))

    def test_desbloqueo_progresivo_solo_al_llegar_desde_crear(self):
        self.assertContains(self.client.get(self._url(self.proceso, "general", asistente=1)), "data-subnav-progresivo")
        # Un registro existente navega libremente entre sus grupos.
        self.assertNotContains(self.client.get(self._url(self.proceso, "general")), "data-subnav-progresivo")

    def test_abrir_un_grupo_concreto_para_regresar_a_el(self):
        pagina = self.client.get(self._url(self.proceso, "general", grupo="responsables"))
        self.assertContains(pagina, 'data-subnav-inicial="responsables"')
        self.assertNotContains(self.client.get(self._url(self.proceso, "general", grupo="otro")), "data-subnav-inicial")

    def test_un_servicio_existente_no_tiene_el_grupo_inicio_activo(self):
        pagina = self.client.get(self._url(self.servicio, "general")).content.decode()
        boton = pagina.split('data-subnav-ir="inicio"')[1].split(">")[0] if 'data-subnav-ir="inicio"' in pagina else " hidden"
        self.assertIn("hidden", boton)

    def test_guardar_desde_el_asistente_sigue_siendo_una_operacion_del_servidor(self):
        respuesta = self.client.post(reverse("catalogo:studio_general_guardar", args=[self.servicio.pk]), {
            "nombre": "", "categoria": self.categoria.pk, "tipo": "SERVICIO", "alcance_visibilidad": "PUBLICO_INTERNO",
        })
        self.assertEqual(respuesta.status_code, 302)
        self.servicio.refresh_from_db()
        self.assertEqual(self.servicio.nombre, "Servicio 4A")  # error del servidor: nada guardado, nada simulado
        self.assertTrue(any("Revise los datos generales" in m for m in self._mensajes(respuesta)))

    def test_el_js_del_studio_arranca_y_protege_los_cambios(self):
        """Regresión: `studio.js` llamaba a `iniciar()` sin definirla y ningún comportamiento arrancaba."""
        import re

        codigo = (RAIZ / "static" / "js" / "studio.js").read_text(encoding="utf-8")
        self.assertIn("function iniciar()", codigo)
        arranque = codigo.split("function iniciar()")[1].split("}")[0]
        for nombre in re.findall(r"iniciar[A-Z]\w*", arranque):
            self.assertIn(f"function {nombre}(", codigo, nombre)
        for comportamiento in ("iniciarSubnav", "iniciarCambiosSinGuardar", "iniciarInicio", "iniciarMultiselect"):
            self.assertIn(comportamiento, arranque)
        self.assertIn("Hay cambios sin guardar en otro grupo", codigo)  # enviar un formulario no pierde otros
        self.assertIn('"pendiente"', codigo)  # Continuar abre el siguiente grupo solo si el servidor confirmó


class ComponentesVisualesTests(_Base4B, TestCase):
    """Componentes compartidos (`.ui-form`, `.ui-panel`, `.opcion-tarjeta`) y movimiento Órbita: la creación los
    adopta y las reglas de contención existen. La validación visual es manual."""

    def setUp(self):
        self._preparar()

    def test_la_creacion_adopta_los_componentes_compartidos(self):
        contenido = self.client.get(reverse("core:disenador_servicio_crear")).content.decode()
        self.assertIn('class="crear__formulario ui-form"', contenido)
        self.assertIn('class="crear__configuracion ui-panel"', contenido)
        self.assertEqual(contenido.count('class="opcion-tarjeta__marca"'), 4)  # la selección no depende solo del color

    def test_tokens_contencion_y_movimiento_reducido(self):
        tokens = (RAIZ / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
        for token in ("--control-h-form: 44px", "--radius-control: 10px", "--textarea-min-h: 110px",
                      "--border-selected", "--ring-selected", "--ring-focus", "--ring-error", "--ease-orbita",
                      "--motion-fast: 150ms", "--motion-slow: 240ms"):
            self.assertIn(token, tokens)
        componentes = (RAIZ / "static" / "css" / "components.css").read_text(encoding="utf-8")
        bloque = componentes.split("3.1 Formulario Órbita")[1].split("4. Badge y status badge")[0]
        for regla in ("box-sizing: border-box;", "max-width: 100%;", "min-width: 0;", "resize: vertical;",
                      "prefers-reduced-motion", "focus-visible"):
            self.assertIn(regla, bloque)
        self.assertNotIn("!important", bloque)
        self.assertNotIn("resize: both", bloque)


class EstadosDeBasicoTests(_Base4B, TestCase):
    """Estados de Básico desde UNA fuente (`apps.catalogo.estado_basico`): solo datos guardados."""

    def setUp(self):
        self._preparar()

    def _estados(self, servicio):
        from apps.catalogo import estado_basico

        servicio.refresh_from_db()
        return {a.clave: a for a in estado_basico.apartados(servicio)}

    def _agregado(self, servicio):
        from apps.catalogo import estado_basico

        servicio.refresh_from_db()
        return estado_basico.agregado(estado_basico.apartados(servicio))[0]

    def _proceso_programado(self):
        self.client.post(reverse("core:disenador_servicio_crear"), {
            "tipo": "PROCESO", "nombre": "Programado 4B", "categoria": self.categoria.pk, "modo": "PROGRAMADO",
            "frecuencia": "MENSUAL", "dia_creacion": "10", "periodo": "MES_SIGUIENTE",
            "responsable_inicial": f"U:{self.persona.pk}",
        })
        return Servicio.objects.get(nombre="Programado 4B")

    def test_identidad_completa_y_opcionales_sin_usar(self):
        servicio = self._servicio()
        Servicio.objects.filter(pk=servicio.pk).update(alcance_visibilidad="PUBLICO_INTERNO")
        estados = self._estados(servicio)
        self.assertEqual(estados["identidad"].estado, "complete")
        self.assertEqual(estados["responsables"].estado, "optional")
        self.assertEqual(estados["tiempos"].estado, "optional")
        self.assertEqual(estados["visibilidad"].estado, "complete")
        self.assertFalse(estados["inicio"].aplica)  # un Servicio no tiene «Inicio»
        self.assertEqual(self._agregado(servicio), "complete")  # opcionales sin configurar no lo impiden

    def test_inicio_manual_completo(self):
        proceso = self._servicio("Proceso manual", tipo=Servicio.Tipo.PROCESO)
        Servicio.objects.filter(pk=proceso.pk).update(alcance_visibilidad="PUBLICO_INTERNO")
        inicio = self._estados(proceso)["inicio"]
        self.assertEqual((inicio.aplica, inicio.obligatorio, inicio.estado), (True, True, "complete"))
        self.assertIn("Manual", inicio.texto)
        self.assertEqual(self._agregado(proceso), "complete")

    def test_inicio_programado_completo_y_visibilidad_no_aplica(self):
        proceso = self._proceso_programado()
        estados = self._estados(proceso)
        self.assertEqual(estados["inicio"].estado, "complete")
        self.assertIn("día 10", estados["inicio"].texto)
        self.assertFalse(estados["visibilidad"].aplica)
        self.assertEqual(estados["responsables"].estado, "complete")  # el responsable inicial queda como responsable
        self.assertEqual(self._agregado(proceso), "complete")

    def test_inicio_programado_incompleto_no_se_guarda_y_uno_invalido_requiere_atencion(self):
        proceso = self._servicio("Proceso a medias", tipo=Servicio.Tipo.PROCESO)
        self.client.post(reverse("catalogo:studio_general_guardar", args=[proceso.pk]), {
            "nombre": proceso.nombre, "categoria": self.categoria.pk, "tipo": "PROCESO",
            "alcance_visibilidad": "PUBLICO_INTERNO", "modo": "PROGRAMADO", "frecuencia": "MENSUAL",
        })
        self.assertFalse(ProgramacionProceso.objects.filter(servicio=proceso).exists())
        self.assertEqual(self._estados(proceso)["inicio"].estado, "complete")  # sigue siendo Manual
        programado = self._proceso_programado()
        programacion = ProgramacionProceso.objects.get(servicio=programado)
        type(programacion.responsable_inicial).objects.filter(pk=programacion.responsable_inicial_id).update(activo=False)
        inicio = self._estados(programado)["inicio"]
        self.assertEqual(inicio.estado, "attention")
        self.assertTrue(inicio.mensajes)
        self.assertEqual(self._agregado(programado), "attention")

    def test_visibilidad_sin_acceso_falta_y_prorroga_invalida_requiere_atencion(self):
        servicio = self._servicio()
        Servicio.objects.filter(pk=servicio.pk).update(alcance_visibilidad="RESTRINGIDO")
        # Restringido sin nadie con acceso (lo normal en un borrador nuevo, RN-038): falta un requisito.
        self.assertEqual(self._estados(servicio)["visibilidad"].estado, "progress")
        self.assertEqual(self._agregado(servicio), "progress")
        # Prórroga con aprobación cuyo aprobador ya no está activo (la BD exige que haya uno: constraint
        # `ck_servicio_prorroga_coherente`; el validador exige además que siga activo).
        aprobador = Usuario.objects.create_user("f4b_aprobador_inactivo", password=CLAVE, is_active=False)
        Servicio.objects.filter(pk=servicio.pk).update(
            alcance_visibilidad="PUBLICO_INTERNO", politica_prorroga="CON_APROBACION", prorroga_aprobador_usuario=aprobador,
        )
        estados = self._estados(servicio)
        self.assertEqual(estados["tiempos"].estado, "attention")
        self.assertEqual(self._agregado(servicio), "attention")  # un opcional inválido también se señala

    def test_el_estado_se_actualiza_tras_guardar(self):
        servicio = self._servicio()
        pagina = self.client.get(self._url(servicio, "general"))
        self.assertContains(pagina, 'data-subnav-ir="tiempos" data-estado-apartado="optional"')
        self.client.post(reverse("catalogo:studio_tiempo_objetivo_guardar", args=[servicio.pk]),
                         {"cantidad": "2", "unidad": "DIAS"})
        pagina = self.client.get(self._url(servicio, "general"))
        self.assertContains(pagina, 'data-subnav-ir="tiempos" data-estado-apartado="complete"')
        self.assertContains(pagina, "subnav__estado--complete")
        self.assertContains(pagina, '<span class="sr-only"> · Completo</span>')

    def test_visitar_no_completa_y_la_seccion_usa_el_mismo_agregado(self):
        servicio = self._servicio()
        Servicio.objects.filter(pk=servicio.pk).update(alcance_visibilidad="RESTRINGIDO")
        for grupo in ("identidad", "responsables", "visibilidad", "tiempos"):
            self.client.get(self._url(servicio, "general", grupo=grupo))
        self.assertEqual(self._estados(servicio)["visibilidad"].estado, "progress")
        secciones = {s["clave"]: s for s in self.client.get(self._url(servicio, "general")).context["secciones"]}
        self.assertEqual(secciones["general"]["estado"], "progress")

    def test_rejilla_y_controles_compartidos_en_basico(self):
        pagina = self.client.get(self._url(self._servicio("Con rejilla", tipo=Servicio.Tipo.PROCESO), "general"))
        self.assertContains(pagina, 'class="svc-section svc-section--basic ui-form"')
        self.assertContains(pagina, "ui-rejilla ui-rejilla--tres")
        self.assertContains(pagina, "field ui-campo--doble")  # responsable inicial ocupa más columnas
        self.assertNotContains(pagina, "svc-form-grid")


class EstadoBasicoCausaTests(_Base4B, TestCase):
    """Corrección del estado de Básico: lo que hace «Completa» es explícito y un borrador nuevo no lo es."""

    def setUp(self):
        self._preparar()

    def test_un_borrador_recien_creado_no_queda_completo(self):
        self.client.post(reverse("core:disenador_servicio_crear"),
                         {"tipo": "SERVICIO", "nombre": "Recién creado", "categoria": self.categoria.pk})
        servicio = Servicio.objects.get(nombre="Recién creado")
        self.assertEqual(servicio.alcance_visibilidad, "RESTRINGIDO")  # RN-038: nada visible hasta concederlo
        pagina = self.client.get(self._url(servicio, "general"))
        secciones = {s["clave"]: s for s in pagina.context["secciones"]}
        self.assertEqual(secciones["general"]["estado"], "progress")
        self.assertIn("Visibilidad", secciones["general"]["texto"])
        self.assertEqual(pagina.context["basico_resumen"]["estado"], "progress")
        self.assertContains(pagina, "data-basico-resumen")

    def test_completa_nombra_los_opcionales_sin_configurar(self):
        servicio = self._servicio()  # público interno, sin responsables ni tiempos
        Servicio.objects.filter(pk=servicio.pk).update(alcance_visibilidad="PUBLICO_INTERNO")
        pagina = self.client.get(self._url(servicio, "general"))
        resumen = pagina.context["basico_resumen"]
        self.assertEqual(resumen["estado"], "complete")
        self.assertIn("opcional sin configurar: Responsables, Tiempos y prórrogas", resumen["texto"])
        # Opcional y sin configurar se distinguen también en el icono.
        self.assertContains(pagina, 'subnav__estado--optional" aria-hidden="true"><svg class="icon icon--sm" aria-hidden="true" focusable="false"><use href="#i-circle"/>')


class NavegacionFlotanteTests(_Base4B, TestCase):
    """Navegación global única, flotante y superior (header + destinos + acciones)."""

    def setUp(self):
        self._preparar()

    def test_una_sola_navegacion_arriba_con_sus_tres_zonas(self):
        contenido = self.client.get(reverse("core:inicio")).content.decode()
        self.assertEqual(contenido.count('<header class="app-header">'), 1)
        self.assertEqual(contenido.count('class="dock"'), 1)
        cabecera = contenido.split('<header class="app-header">')[1].split("</header>")[0]
        self.assertIn('class="app-header__inicio"', cabecera)  # logo + búsqueda
        self.assertIn('<nav class="dock" aria-label="Navegación principal">', cabecera)  # destinos en el centro
        self.assertIn('class="app-header__actions"', cabecera)  # notificaciones + perfil
        self.assertIn("Notificaciones (próximamente)", cabecera)
        self.assertIn("(próximamente)", cabecera.split('class="app-header__inicio"')[1].split("</div>")[0])
        # El dock ya no se dibuja al pie de la página, fuera de la cabecera.
        self.assertNotIn('class="dock"', contenido.split("</header>", 1)[1])
        self.assertIn("app-shell app-shell--nav", contenido)

    def test_perfil_y_cerrar_sesion_siguen_en_el_menu(self):
        contenido = self.client.get(reverse("core:inicio")).content.decode()
        menu = contenido.split('id="menu-usuario"')[1].split("</header>")[0]
        self.assertIn(reverse("core:perfil"), menu)
        self.assertIn(reverse("core:logout"), menu)
        self.assertIn("Cerrar sesión", menu)

    def test_sin_sesion_no_hay_navegacion(self):
        self.client.logout()
        contenido = self.client.get(reverse("core:login")).content.decode()
        self.assertNotIn('class="app-header"', contenido)
        self.assertNotIn("app-shell--nav", contenido)

    def test_el_contenido_reserva_arriba_y_no_abajo(self):
        tokens = (RAIZ / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
        self.assertIn("--header-h: calc(var(--nav-h) + 2 * var(--nav-offset));", tokens)
        self.assertIn("--dock-clearance: var(--space-6);", tokens)
        self.assertIn("--dock-reserve: var(--space-4);", tokens)
        layout = (RAIZ / "static" / "css" / "layout.css").read_text(encoding="utf-8")
        self.assertIn(".app-shell--nav {\n  padding-top: var(--header-h);", layout)
        self.assertNotIn("bottom: var(--dock-offset)", layout)
        self.assertIn("prefers-reduced-motion", layout)


class ConstructorDeFormularioLayoutTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()

    def test_barra_de_herramientas_y_estado_vacio(self):
        servicio = self._servicio()
        pagina = self.client.get(self._url(servicio, "entrada"))
        self.assertContains(pagina, 'class="constructor-barra" role="toolbar"')
        self.assertContains(pagina, "Editar formulario")  # la acción de versión vive en la barra
        self.assertContains(pagina, "constructor-vacio")
        self.assertContains(pagina, "data-constructor")

    def test_colecciones_siguen_en_la_estructura(self):
        proceso = self._servicio("Proceso colecciones", tipo=Servicio.Tipo.PROCESO)
        pagina = self.client.get(self._url(proceso, "entrada", uso="parrilla"))
        self.assertEqual(pagina.status_code, 200)
        self.assertContains(pagina, "Colecciones de ítems")
        self.assertContains(pagina, "constructor__estructura")

    def test_rejilla_por_ancho_del_area_de_trabajo(self):
        estudio = (RAIZ / "static" / "css" / "studio.css").read_text(encoding="utf-8")
        self.assertIn("container-name: trabajo;", estudio)
        self.assertIn("@container trabajo (min-width: 1040px)", estudio)
        self.assertIn("clamp(240px, 22%, 270px) minmax(0, 1fr) clamp(300px, 28%, 340px)", estudio)
        self.assertIn("grid-template-columns: minmax(0, 1fr);\n  gap: var(--space-2);\n  margin: 0;", estudio)  # estructura en una columna


# --- Formulario de inicio opcional en Procesos + lista y edición de campos/columnas ---------------------------


class FormularioInicialOpcionalTests(_Base4B, TestCase):
    """Un PROCESO puede publicarse con solo datos de inicio, solo colecciones, ambos o ninguno; un SERVICIO sigue
    necesitando su formulario de entrada activo."""

    def setUp(self):
        self._preparar()

    def _proceso(self, nombre, *, formulario=False, coleccion=False):
        workflow = self._flujo(f"Flujo {nombre}")
        proceso = Servicio.objects.create(
            nombre=nombre, categoria=self.categoria, tipo=Servicio.Tipo.PROCESO, workflow=workflow,
            alcance_visibilidad="PUBLICO_INTERNO",
            formulario=self._formulario_activo(f"Entrada {nombre}") if formulario else None,
        )
        config = preparar_configuracion_ejecucion(proceso, self.admin)
        self._actividad(config, self._fase(workflow, "Recepción"), "Atender")
        activar_configuracion_ejecucion(proceso, config, self.admin)
        if coleccion:
            from apps.catalogo.parrilla import agregar_coleccion
            from apps.catalogo.versionamiento import activar_version

            definicion = agregar_coleccion(proceso, self.admin, nombre_elemento="Ítem")
            version = definicion.formulario.versiones.get(numero=1)
            Campo.objects.create(version=version, tipo="TEXTO", etiqueta="Nombre del ítem", orden=1)
            activar_version(definicion.formulario, version, self.admin)
        proceso.refresh_from_db()
        return proceso

    def _estado(self, servicio, clave="entrada"):
        from apps.catalogo import evaluacion
        from apps.catalogo.studio import _estado_secciones

        # Bloque 2: la misma evaluación (simulada) que usa el Studio.
        return next(s for s in _estado_secciones(servicio, evaluacion.evaluar(servicio, self.admin)) if s["clave"] == clave)

    def test_servicio_sin_formulario_activo_no_se_publica(self):
        from apps.catalogo.operaciones import errores_de_formulario_inicial, validar_publicacion

        servicio = Servicio.objects.create(nombre="Sin entrada", categoria=self.categoria, tipo=Servicio.Tipo.SERVICIO)
        self.assertEqual(errores_de_formulario_inicial(servicio), ["Se requiere un formulario con una versión ACTIVA propia."])
        with self.assertRaises(ValidationError):
            validar_publicacion(servicio)

    def test_proceso_sin_formulario_no_lo_exige(self):
        from apps.catalogo.operaciones import errores_de_formulario_inicial

        proceso = self._proceso("Sin nada")
        self.assertEqual(errores_de_formulario_inicial(proceso), [])
        estado = self._estado(proceso)
        self.assertEqual(estado["estado"], "optional")
        self.assertNotIn("formulario", " ".join(m.lower() for m in self._pendientes(proceso)))

    def _pendientes(self, servicio):
        from apps.catalogo.operaciones import validar_publicacion

        try:
            validar_publicacion(servicio)
        except ValidationError as exc:
            return exc.messages
        return []

    def test_proceso_con_borrador_de_datos_sin_activar_lo_senala(self):
        from apps.catalogo.operaciones import asociar_formulario_nuevo, errores_de_formulario_inicial

        proceso = self._proceso("Borrador iniciado")
        asociar_formulario_nuevo(proceso, self.admin, nombre="Datos")
        proceso.refresh_from_db()
        self.assertTrue(errores_de_formulario_inicial(proceso)[0].startswith("Los datos de inicio"))
        self.assertEqual(self._estado(proceso)["estado"], "progress")

    def test_proceso_solo_con_formulario(self):
        from apps.catalogo.operaciones import activar_servicio

        proceso = self._proceso("Solo datos", formulario=True)
        formulario_id = proceso.formulario_id
        activar_servicio(proceso, self.admin)
        proceso.refresh_from_db()
        self.assertTrue(proceso.activo)
        self.assertEqual(proceso.formulario_id, formulario_id)  # no se reemplaza lo configurado

    def test_proceso_solo_con_colecciones_se_publica_y_recibe_datos_de_inicio_vacios(self):
        from apps.catalogo.operaciones import activar_servicio

        proceso = self._proceso("Solo colecciones", coleccion=True)
        self.assertEqual(self._pendientes(proceso), [])
        estado = self._estado(proceso)
        self.assertEqual(estado["estado"], "complete")
        self.assertIn("1 colección", estado["texto"])
        activar_servicio(proceso, self.admin)
        proceso.refresh_from_db()
        self.assertTrue(proceso.activo)
        self.assertIsNotNone(proceso.formulario.version_activa)
        self.assertFalse(proceso.formulario.version_activa.campos.exists())

    def test_proceso_con_ambos(self):
        from apps.catalogo.operaciones import activar_servicio

        proceso = self._proceso("Ambos", formulario=True, coleccion=True)
        self.assertEqual(self._pendientes(proceso), [])
        activar_servicio(proceso, self.admin)
        proceso.refresh_from_db()
        self.assertTrue(proceso.activo)

    def test_proceso_sin_ambos_ejecuta_sin_nada_que_diligenciar(self):
        from apps.catalogo.operaciones import activar_servicio
        from apps.tickets import datos_inicio
        from apps.tickets.operaciones import _crear_borrador_de_servicio

        proceso = self._proceso("Vacío")
        proceso.datos_inicio_en_ejecucion = True
        proceso.save(update_fields=["datos_inicio_en_ejecucion"])
        activar_servicio(proceso, self.admin)
        proceso.refresh_from_db()  # `activar_servicio` le asignó el formulario técnico vacío en otra instancia
        ticket = _crear_borrador_de_servicio(self.persona, proceso)
        self.assertEqual(ticket.detalle_servicio.formulario_version_id, proceso.formulario.version_activa_id)
        self.assertFalse(datos_inicio.requiere_diligenciamiento(ticket))
        self.assertTrue(datos_inicio.datos_completos(ticket))

    def test_asegurar_datos_inicio_es_idempotente_y_no_toca_servicios(self):
        from apps.catalogo.operaciones import asegurar_datos_inicio

        proceso = self._proceso("Idempotente")
        primero = asegurar_datos_inicio(proceso, self.admin)
        proceso.refresh_from_db()
        self.assertIsNotNone(primero)
        self.assertIsNone(asegurar_datos_inicio(proceso, self.admin))
        servicio = Servicio.objects.create(nombre="Servicio X", categoria=self.categoria, tipo=Servicio.Tipo.SERVICIO)
        self.assertIsNone(asegurar_datos_inicio(servicio, self.admin))
        self.assertIsNone(Servicio.objects.get(pk=servicio.pk).formulario_id)

    def test_studio_muestra_datos_de_inicio_como_opcionales(self):
        proceso = self._proceso("Studio opcional")
        pagina = self.client.get(self._url(proceso, "entrada"))
        self.assertEqual(pagina.status_code, 200)
        self.assertContains(pagina, "Este proceso no pide datos de inicio")
        self.assertContains(pagina, "No los usa (opcional)")
        self.assertNotContains(pagina, "Este servicio todavía no tiene un formulario de entrada")
        publicar = self.client.get(self._url(proceso, "publicacion"))
        self.assertNotContains(publicar, "Datos de inicio: sin una versión activa")


class ListaYEdicionDeCamposTests(_Base4B, TestCase):
    def setUp(self):
        self._preparar()

    def test_campos_en_lista_alineada_y_edicion_agrupada(self):
        servicio = self._servicio("Con campos")
        version = crear_nueva_version(servicio.formulario, self.admin)
        campo = Campo.objects.create(version=version, tipo="FECHA", etiqueta="Fecha deseada", orden=1, obligatorio=True)
        pagina = self.client.get(self._url(servicio, "entrada"))
        self.assertContains(pagina, 'class="campo-tabla"')
        self.assertContains(pagina, f'data-mostrar-propiedad="propiedad-campo-{campo.pk}"')
        self.assertContains(pagina, "requisito--obligatorio")
        self.assertContains(pagina, "#i-calendar")  # icono del tipo
        for grupo in ("Identidad", "Comportamiento", "Ayuda y configuración adicional"):
            self.assertContains(pagina, grupo)
        self.assertContains(pagina, "data-propiedades-ampliar")
        self.assertContains(pagina, f'name="campo-{campo.pk}-editar-es_fecha_requerida"')  # solo para fechas
        self.assertNotContains(pagina, "svc-fake-field")  # el panel ya no repite la lista

    def test_columnas_de_coleccion_usan_el_mismo_patron(self):
        from apps.catalogo.parrilla import agregar_coleccion

        proceso = self._servicio("Proceso col", tipo=Servicio.Tipo.PROCESO)
        definicion = agregar_coleccion(proceso, self.admin, nombre_elemento="Ítem")
        version = definicion.formulario.versiones.get(numero=1)
        columna = Campo.objects.create(version=version, tipo="USUARIO", etiqueta="Responsable", orden=1)
        pagina = self.client.get(self._url(proceso, "entrada", uso="parrilla"))
        self.assertContains(pagina, 'class="campo-tabla"')
        self.assertContains(pagina, f'id="panel-campo-{columna.pk}"')
        self.assertContains(pagina, "Opcional")
        self.assertContains(pagina, "Guardar columna")

    def test_editar_un_campo_conserva_el_contrato(self):
        servicio = self._servicio("Edición")
        version = crear_nueva_version(servicio.formulario, self.admin)
        campo = Campo.objects.create(version=version, tipo="TEXTO", etiqueta="Asunto", orden=1)
        prefijo = f"campo-{campo.pk}-editar"
        url = reverse("catalogo:studio_campo_editar", args=[servicio.pk, campo.pk])
        self.client.post(url, {
            f"{prefijo}-tipo": "TEXTO", f"{prefijo}-etiqueta": "Asunto del ticket", f"{prefijo}-clave": campo.clave,
            f"{prefijo}-ayuda": "Resume tu necesidad", f"{prefijo}-orden": "1", f"{prefijo}-obligatorio": "on",
        })
        campo.refresh_from_db()
        self.assertEqual(campo.etiqueta, "Asunto del ticket")
        self.assertTrue(campo.obligatorio)

    def test_estilos_y_comportamiento_del_editor(self):
        estudio = (RAIZ / "static" / "css" / "studio.css").read_text(encoding="utf-8")
        self.assertIn(".campo-tabla", estudio)
        self.assertIn("@container propiedades (min-width: 620px)", estudio)
        self.assertIn(".constructor.is-ampliado", estudio)
        self.assertIn("resize: vertical", estudio)
        js = (RAIZ / "static" / "js" / "studio.js").read_text(encoding="utf-8")
        self.assertIn("data-propiedades-ampliar", js)
        self.assertIn("is-ampliado", js)


class FormularioCompletoConUnaPiezaTests(_Base4B, TestCase):
    """Los datos de entrada de un Proceso pueden ser el formulario de inicio O una colección: con uno de los dos
    activo, la sección Formulario está completa aunque el otro tenga un borrador."""

    setUp = FormularioInicialOpcionalTests.setUp
    _proceso = FormularioInicialOpcionalTests._proceso
    _estado = FormularioInicialOpcionalTests._estado


    def test_solo_formulario_de_inicio_activo_completa(self):
        proceso = self._proceso("Solo inicio", formulario=True)
        version = crear_nueva_version(proceso.formulario, self.admin)
        Campo.objects.create(version=version, tipo="TEXTO", etiqueta="Asunto", orden=1)
        from apps.catalogo.versionamiento import activar_version

        activar_version(proceso.formulario, version, self.admin)
        proceso.refresh_from_db()
        self.assertEqual(self._estado(proceso)["estado"], "complete")

    def test_solo_coleccion_activa_completa_aunque_el_inicio_tenga_borrador(self):
        from apps.catalogo.operaciones import asociar_formulario_nuevo

        proceso = self._proceso("Colección y borrador", coleccion=True)
        asociar_formulario_nuevo(proceso, self.admin, nombre="Datos")
        proceso.refresh_from_db()
        estado = self._estado(proceso)
        self.assertEqual(estado["estado"], "complete")
        self.assertIn("cambios sin activar", estado["texto"])

    def test_sin_ninguna_pieza_es_opcional(self):
        self.assertEqual(self._estado(self._proceso("Nada"))["estado"], "optional")
