"""Pruebas de la Fase 4.A — Studio guiado y arquitectura funcional.

Cubren: navegación entre las cinco secciones (Atrás / Continuar / Omitir, progreso y estados), persistencia de
borradores al navegar, controles dependientes, diferencias Servicio/Proceso, el selector «¿Qué necesitas que
suceda?» y su traducción a bloques existentes (compatibilidad con el alta por tipo), reordenar, la interfaz de
dominio del flujo (`flujo_api`), la reconciliación de la configuración con una versión nueva de la plantilla
(sin perder bloques ni rutas, conflictos explícitos, ejecución sobre la versión propia, copia del flujo), la
publicación de cambios (pendientes, simulación sin efectos, todo o nada, mensajes veraces), permisos, edición de
opciones y reglas, la entrega formal retirable, procesos programados y flujos LEGACY.

Se entregan SIN ejecutar (ver los comandos en el informe de entrega)."""

from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.catalogo import dependencias, flujo_api, intenciones, publicacion, reconciliacion
from apps.catalogo.configuracion_ejecucion import (
    activar_configuracion_ejecucion,
    agregar_bloque_operativo,
    conectar_bloques_operativos,
    preparar_configuracion_ejecucion,
)
from apps.catalogo.models import (
    BloqueOperativo,
    Campo,
    Categoria,
    ConfiguracionEjecucionVersion,
    Formulario,
    FormularioVersion,
    OpcionCampo,
    ReglaCondicional,
    Servicio,
    TransicionBloqueOperativo,
)
from apps.catalogo.operaciones import configurar_politica_entrega, validar_ejecucion
from apps.catalogo.tests import _permiso_get_or_create
from apps.catalogo.versionamiento import activar_version, crear_nueva_version
from apps.core.models import RegistroAuditoria

Usuario = get_user_model()
CLAVE = "Clave-Segura-4A"


class _Base4A:
    PERMISOS = ("catalogo.administrar", "formulario.administrar", "workflows.administrar", "workflows.vincular")

    def _preparar(self):
        self.admin = Usuario.objects.create_user("f4a_admin", password=CLAVE)
        for codigo in self.PERMISOS:
            _permiso_get_or_create(self.admin, codigo, f"Rol 4A {codigo}")
        self.persona = Usuario.objects.create_user("f4a_persona", password=CLAVE)
        # Bloque 2: publicar exige que alguien pueda atender lo publicado (aquí, con alcance global).
        self.atencion_global = Usuario.objects.create_user("f4a_atencion_global", password=CLAVE)
        _permiso_get_or_create(self.atencion_global, "tickets.atender", "Rol 4A atención global")
        self.categoria = Categoria.objects.create(nombre="Categoría 4A")
        self.client.login(username="f4a_admin", password=CLAVE)

    def _formulario_activo(self, nombre):
        formulario = Formulario.objects.create(nombre=nombre)
        version = crear_nueva_version(formulario, self.admin)
        activar_version(formulario, version, self.admin)
        return formulario

    def _flujo(self, nombre="Flujo 4A", fases=("Recepción", "Revisión")):
        from apps.workflows import fases as fases_ops
        from apps.workflows.versionamiento import activar_version as activar_workflow

        workflow = fases_ops.crear_plantilla_fases(self.admin, nombre=nombre)
        version = workflow.versiones.get(numero=1)
        creadas = [fases_ops.agregar_fase(version, self.admin, nombre=n) for n in fases]
        for origen, destino in zip(creadas, creadas[1:]):
            fases_ops.conectar_fases(origen, destino, self.admin)
        activar_workflow(workflow, version, self.admin)
        workflow.refresh_from_db()
        return workflow

    def _servicio(self, nombre="Servicio 4A", *, tipo=Servicio.Tipo.SERVICIO, workflow=None, activo=False):
        return Servicio.objects.create(
            nombre=nombre, categoria=self.categoria, tipo=tipo, workflow=workflow, activo=activo,
            formulario=self._formulario_activo(f"Entrada {nombre}"),
        )

    def _fase(self, workflow, nombre, version=None):
        version = version or workflow.version_activa
        return version.fases.get(nombre=nombre)

    def _actividad(self, config, fase, nombre, **extra):
        return agregar_bloque_operativo(
            config, self.admin, fase=fase, tipo="ACTIVIDAD", nombre=nombre,
            configuracion={"tipo_actor": "USUARIO", "usuario_id": self.persona.pk}, **extra,
        )

    def _url(self, servicio, tab, **params):
        extra = "".join(f"&{k}={v}" for k, v in params.items())
        return f"{reverse('catalogo:studio', args=[servicio.pk])}?tab={tab}{extra}"

    def _mensajes(self, respuesta):
        return [str(m) for m in get_messages(respuesta.wsgi_request)]

    def _eventos(self, objeto):
        return RegistroAuditoria.objects.filter(
            content_type=ContentType.objects.get_for_model(type(objeto)), object_id=objeto.pk
        )

    def _crear(self, servicio, **datos):
        base = {"nuevo-nombre": datos.pop("nombre", "Paso"), "nuevo-descripcion": ""}
        base.update(datos)
        return self.client.post(reverse("catalogo:studio_bloque_crear", args=[servicio.pk]), base)


# --- 1/2. Navegación guiada, progreso y estados ----------------------------------------------------------------


class NavegacionGuiadaTests(_Base4A, TestCase):
    def setUp(self):
        self._preparar()
        self.servicio = self._servicio()

    def test_atras_continuar_y_posicion_en_las_cinco_secciones(self):
        orden = ["general", "entrada", "salida", "ejecucion", "publicacion"]
        for indice, tab in enumerate(orden):
            respuesta = self.client.get(self._url(self.servicio, tab))
            self.assertEqual(respuesta.status_code, 200, tab)
            nav = respuesta.context["navegacion"]
            self.assertEqual(nav["posicion"], indice + 1)
            self.assertEqual(nav["anterior"]["clave"] if nav["anterior"] else None, orden[indice - 1] if indice else None)
            self.assertEqual(
                nav["siguiente"]["clave"] if nav["siguiente"] else None, orden[indice + 1] if indice < 4 else None
            )
        general = self.client.get(self._url(self.servicio, "general"))
        self.assertContains(general, "Continuar: Formulario")
        self.assertNotContains(general, "Atrás:")
        self.assertContains(self.client.get(self._url(self.servicio, "publicacion")), "Atrás: Flujo")

    def test_omitir_solo_en_secciones_opcionales_y_no_borra_nada(self):
        salida = self.client.get(self._url(self.servicio, "salida"))  # Servicio sin entregables: opcional
        self.assertTrue(salida.context["navegacion"]["omitible"])
        self.assertContains(salida, ">Omitir</a>")
        general = self.client.get(self._url(self.servicio, "general"))
        self.assertFalse(general.context["navegacion"]["omitible"])
        self.assertNotContains(general, ">Omitir</a>")
        # Omitir es un enlace a la siguiente sección: no es un POST ni cambia datos.
        self.assertIn("?tab=ejecucion", salida.content.decode().split(">Omitir</a>")[0].rsplit("href=", 1)[1])

    def test_estados_de_seccion_distinguen_completa_opcional_atencion_y_progreso(self):
        # Básico completo exige visibilidad resuelta (4.B: `estado_basico`); un borrador nace RESTRINGIDO sin acceso.
        Servicio.objects.filter(pk=self.servicio.pk).update(alcance_visibilidad="PUBLICO_INTERNO")
        estados = {s["clave"]: s for s in self.client.get(self._url(self.servicio, "general")).context["secciones"]}
        self.assertEqual(estados["general"]["estado"], "complete")
        self.assertEqual(estados["entrada"]["estado"], "complete")
        self.assertEqual(estados["salida"]["estado"], "optional")
        self.assertEqual(estados["ejecucion"]["estado"], "optional")
        self.assertEqual(estados["general"]["etiqueta"], "Básico")
        self.assertEqual(estados["salida"]["estado_etiqueta"], "Opcional")
        crear_nueva_version(self.servicio.formulario, self.admin)
        estados = {s["clave"]: s for s in self.client.get(self._url(self.servicio, "general")).context["secciones"]}
        # Bloque 2: un borrador válido no invalida la versión activa: sigue completa, con cambios sin activar.
        self.assertEqual(estados["entrada"]["estado"], "complete")
        self.assertIn("cambios sin activar", estados["entrada"]["texto"])
        proceso = self._servicio("Proceso sin flujo", tipo=Servicio.Tipo.PROCESO)
        estados = {s["clave"]: s for s in self.client.get(self._url(proceso, "general")).context["secciones"]}
        self.assertEqual(estados["ejecucion"]["estado"], "empty")
        sin_formulario = Servicio.objects.create(nombre="Vacío", categoria=self.categoria)
        estados = {s["clave"]: s for s in self.client.get(self._url(sin_formulario, "general")).context["secciones"]}
        self.assertEqual(estados["entrada"]["estado"], "empty")

    def test_progreso_cuenta_las_secciones_listas(self):
        Servicio.objects.filter(pk=self.servicio.pk).update(alcance_visibilidad="PUBLICO_INTERNO")  # Básico completo
        nav = self.client.get(self._url(self.servicio, "general")).context["navegacion"]
        self.assertEqual(nav["total"], 5)
        self.assertGreaterEqual(nav["listas"], 4)  # básico, formulario, salida y flujo opcionales
        self.assertContains(self.client.get(self._url(self.servicio, "general")), "secciones listas")

    def test_navegar_no_guarda_ni_activa_ni_publica_y_conserva_el_borrador(self):
        borrador = crear_nueva_version(self.servicio.formulario, self.admin)
        Campo.objects.create(version=borrador, tipo="TEXTO", etiqueta="Asunto", orden=1)
        for tab in ("general", "entrada", "salida", "ejecucion", "publicacion", "entrada"):
            self.client.get(self._url(self.servicio, tab))
        borrador.refresh_from_db()
        self.servicio.refresh_from_db()
        self.assertEqual(borrador.estado, FormularioVersion.Estado.BORRADOR)
        self.assertEqual(borrador.campos.count(), 1)
        self.assertFalse(self.servicio.activo)

    def test_aviso_de_cambios_sin_guardar_es_explicito(self):
        pagina = self.client.get(self._url(self.servicio, "general"))
        self.assertContains(pagina, "data-studio-navegacion")
        self.assertContains(pagina, "Continuar solo cambia de sección")


# --- 4/5. Controles dependientes y diferencias Servicio/Proceso -------------------------------------------------


class ControlesDependientesTests(_Base4A, TestCase):
    def setUp(self):
        self._preparar()
        self.workflow = self._flujo()
        self.servicio = self._servicio(workflow=self.workflow)
        self.config = preparar_configuracion_ejecucion(self.servicio, self.admin)

    def test_responsable_muestra_cada_selector_solo_con_su_opcion(self):
        pagina = self.client.get(self._url(self.servicio, "ejecucion"))
        self.assertContains(pagina, 'data-mostrar-si="tipo_actor=USUARIO"')
        self.assertContains(pagina, 'data-mostrar-si="tipo_actor=EQUIPO"')
        self.assertContains(pagina, 'data-mostrar-si="modo_fechas=PLAZO"')
        self.assertContains(pagina, "Opciones avanzadas")

    def test_parametros_de_campo_por_tipo_y_revision_solo_en_proceso(self):
        crear_nueva_version(self.servicio.formulario, self.admin)
        entrada = self.client.get(self._url(self.servicio, "entrada"))
        self.assertContains(entrada, 'data-mostrar-si="-tipo=ARCHIVO"')
        self.assertContains(entrada, 'data-mostrar-si="-tipo=MULTILISTA"')
        salida = self.client.get(self._url(self.servicio, "salida"))
        self.assertNotContains(salida, 'data-mostrar-si="-tipo=ELEMENTOS"')
        tipos = [v for v, _ in salida.context["entregable_form_nuevo"].fields["tipo"].choices]
        self.assertNotIn("ELEMENTOS", tipos)  # un Servicio no ofrece la revisión de elementos
        proceso = self._servicio("Proceso", tipo=Servicio.Tipo.PROCESO, workflow=self.workflow)
        salida_proceso = self.client.get(self._url(proceso, "salida"))
        self.assertContains(salida_proceso, 'data-mostrar-si="-tipo=ELEMENTOS"')

    def test_un_servicio_no_ofrece_colecciones_ni_gacetas(self):
        codigos = [g["intencion"].codigo for g in intenciones.opciones_para(self.servicio)]
        self.assertNotIn("coleccion", codigos)
        self.assertNotIn("colaborativa", codigos)
        self.assertIn("tarea", codigos)

    def test_un_proceso_sin_coleccion_ve_la_opcion_deshabilitada_con_el_motivo(self):
        proceso = self._servicio("Proceso", tipo=Servicio.Tipo.PROCESO, workflow=self.workflow)
        grupo = next(g for g in intenciones.opciones_para(proceso) if g["intencion"].codigo == "coleccion")
        recorrido = next(o for o in grupo["opciones"] if o["opcion"].codigo == "recorrido")
        self.assertFalse(recorrido["disponible"])
        self.assertIn("colección", recorrido["motivo"])
        self.assertEqual(recorrido["seccion"], ("entrada", "parrilla"))


# --- 6/7/8. Selector unificado y traducción a bloques existentes ------------------------------------------------


class SelectorDeActividadesTests(_Base4A, TestCase):
    def setUp(self):
        self._preparar()
        self.workflow = self._flujo()
        self.servicio = self._servicio(workflow=self.workflow)
        self.config = preparar_configuracion_ejecucion(self.servicio, self.admin)
        self.recepcion = self._fase(self.workflow, "Recepción")

    def _tarea(self, **extra):
        datos = {
            "fase_id": self.recepcion.pk, "intencion": "tarea", "nuevo-config-tipo_actor": "USUARIO",
            "nuevo-config-usuario": self.persona.pk, "nuevo-config-modo_fechas": "SIN_FECHA",
        }
        datos.update(extra)
        return self._crear(self.servicio, **datos)

    def test_cada_opcion_se_traduce_a_un_tipo_de_bloque_existente(self):
        esperado = {
            "tarea": "ACTIVIDAD", "tareas_paralelo": "GRUPO_ACTIVIDADES", "informacion": "ACTIVIDAD",
            "resultado": "ENTREGABLE", "aprobacion": "APROBACION", "recorrido": "RECORRIDO_ITEMS",
            "tareas_elementos": "TAREAS_ELEMENTOS", "revision_conjunto": "ENTREGABLE", "gaceta": "GACETA",
            "decision": "DECISION",
        }
        self.assertEqual({c: intenciones.traducir(c) for c in esperado}, esperado)
        for tipo in esperado.values():
            self.assertIn(tipo, BloqueOperativo.TIPOS_CONFIGURABLES)
        with self.assertRaises(ValidationError):
            intenciones.traducir("inexistente")

    def test_la_pagina_pregunta_que_necesitas_que_suceda(self):
        pagina = self.client.get(self._url(self.servicio, "ejecucion"))
        self.assertContains(pagina, "¿Qué necesitas que suceda?")
        self.assertContains(pagina, "Agregar actividad")
        # 4.B: el menú de intenciones vive en el inspector de cada fase; el alta, en un panel por opción.
        self.assertContains(pagina, f'data-fase-agregar="{self.recepcion.pk}"')
        self.assertContains(pagina, 'id="inspector-nuevo-tarea"')
        self.assertContains(pagina, 'name="intencion" value="tarea"')

    def test_realizar_una_tarea_crea_una_actividad(self):
        self._tarea(**{"nuevo-nombre": "Revisar solicitud"})
        bloque = BloqueOperativo.objects.get(version=self.config, nombre="Revisar solicitud")
        self.assertEqual(bloque.tipo, "ACTIVIDAD")
        self.assertEqual(bloque.configuracion["usuario_id"], self.persona.pk)

    def test_solicitar_evidencia_es_una_actividad_que_la_exige(self):
        self._crear(
            self.servicio, fase_id=self.recepcion.pk, intencion="informacion", nombre="Adjuntar soporte",
            **{"nuevo-config-tipo_actor": "RESPONSABLE_TICKET", "nuevo-config-requiere_evidencia": "on",
               "nuevo-config-modo_fechas": "SIN_FECHA"},
        )
        bloque = BloqueOperativo.objects.get(version=self.config, nombre="Adjuntar soporte")
        self.assertEqual(bloque.tipo, "ACTIVIDAD")
        self.assertTrue(bloque.configuracion["requiere_evidencia"])

    def test_varias_tareas_al_mismo_tiempo_es_un_grupo_y_admite_tareas_dentro(self):
        self._crear(self.servicio, fase_id=self.recepcion.pk, intencion="tareas_paralelo", nombre="En paralelo")
        grupo = BloqueOperativo.objects.get(version=self.config, nombre="En paralelo")
        self.assertEqual(grupo.tipo, "GRUPO_ACTIVIDADES")
        self._crear(
            self.servicio, contenedor_id=grupo.pk, rama="GENERAL", intencion="tarea", nombre="Dentro",
            **{"nuevo-config-tipo_actor": "USUARIO", "nuevo-config-usuario": self.persona.pk,
               "nuevo-config-modo_fechas": "SIN_FECHA"},
        )
        self.assertEqual(BloqueOperativo.objects.get(nombre="Dentro").contenedor_id, grupo.pk)
        # Una condición no es una opción de una rama general: se rechaza sin crear nada.
        self._crear(self.servicio, contenedor_id=grupo.pk, rama="GENERAL", intencion="decision", nombre="No")
        self.assertFalse(BloqueOperativo.objects.filter(nombre="No").exists())

    def test_una_opcion_que_no_aplica_se_rechaza_en_el_servidor(self):
        respuesta = self._crear(self.servicio, fase_id=self.recepcion.pk, intencion="recorrido", nombre="Recorrer")
        self.assertFalse(BloqueOperativo.objects.filter(nombre="Recorrer").exists())
        self.assertTrue(any("proceso" in m for m in self._mensajes(respuesta)))

    def test_una_aprobacion_puede_crearse_con_sus_rutas(self):
        self._tarea(**{"nuevo-nombre": "Hacer"})
        hacer = BloqueOperativo.objects.get(nombre="Hacer")
        self._crear(
            self.servicio, fase_id=self.recepcion.pk, intencion="aprobacion", nombre="Aprobar",
            **{"nuevo-config-cantidad": "1", "nuevo-participantes-0-tipo": "USUARIO",
               "nuevo-participantes-0-usuario": self.persona.pk, "nuevo-ruta-destino_aprobada": "FIN",
               "nuevo-ruta-destino_devuelta": hacer.pk, "nuevo-ruta-destino_rechazada": "FIN"},
        )
        aprobar = BloqueOperativo.objects.get(nombre="Aprobar")
        rutas = {t.resultado_aprobacion: t for t in aprobar.transiciones_salientes.all()}
        self.assertEqual(set(rutas), {"APROBADA", "DEVUELTA", "RECHAZADA"})
        self.assertTrue(rutas["APROBADA"].finaliza)
        self.assertEqual(rutas["DEVUELTA"].bloque_destino_id, hacer.pk)

    def test_una_aprobacion_sin_rutas_sigue_siendo_posible_y_publicar_las_exige(self):
        self._crear(
            self.servicio, fase_id=self.recepcion.pk, intencion="aprobacion", nombre="Aprobar luego",
            **{"nuevo-config-cantidad": "1", "nuevo-participantes-0-tipo": "USUARIO",
               "nuevo-participantes-0-usuario": self.persona.pk, "nuevo-ruta-destino_aprobada": "",
               "nuevo-ruta-destino_devuelta": "", "nuevo-ruta-destino_rechazada": ""},
        )
        aprobar = BloqueOperativo.objects.get(nombre="Aprobar luego")
        self.assertFalse(aprobar.transiciones_salientes.exists())
        from apps.catalogo.configuracion_ejecucion import validar_configuracion_ejecucion

        self.assertTrue(any("Aprobar luego" in e for e in validar_configuracion_ejecucion(self.config)))

    def test_evaluar_una_condicion_crea_la_decision_con_sus_rutas(self):
        self._crear(
            self.servicio, fase_id=self.recepcion.pk, intencion="decision", nombre="¿Urgente?",
            **{"nuevo-cond-variable": "formulario.prioridad", "nuevo-cond-operador": "IGUAL_A",
               "nuevo-cond-valor": "alta", "nuevo-cond-destino": "FIN", "nuevo-fallback-destino": "FIN"},
        )
        decision = BloqueOperativo.objects.get(nombre="¿Urgente?")
        self.assertEqual(decision.tipo, "DECISION")
        self.assertEqual(decision.transiciones_salientes.count(), 2)

    def test_el_modo_de_fechas_descarta_lo_que_no_corresponde(self):
        self._tarea(**{"nuevo-nombre": "Sin fecha", "nuevo-config-plazo_limite_cantidad": "3"})
        self.assertIsNone(BloqueOperativo.objects.get(nombre="Sin fecha").configuracion["plazo_limite"])
        self._tarea(**{
            "nuevo-nombre": "Con plazo", "nuevo-config-modo_fechas": "PLAZO", "nuevo-config-plazo_limite_cantidad": "3",
            "nuevo-config-plazo_limite_unidad": "DIAS",
        })
        self.assertEqual(BloqueOperativo.objects.get(nombre="Con plazo").configuracion["plazo_limite"]["cantidad"], 3)

    def test_compatibilidad_el_alta_por_tipo_sigue_funcionando(self):
        self._crear(
            self.servicio, fase_id=self.recepcion.pk, nombre="Por tipo",
            **{"nuevo-tipo": "ACTIVIDAD", "nuevo-config-tipo_actor": "USUARIO", "nuevo-config-usuario": self.persona.pk},
        )
        self.assertEqual(BloqueOperativo.objects.get(nombre="Por tipo").tipo, "ACTIVIDAD")
        self.assertIn("ACTIVIDAD", [c for c, _e, _d in self.client.get(self._url(self.servicio, "ejecucion")).context["bloques_disponibles"]])

    def test_errores_identifican_el_campo(self):
        respuesta = self._tarea(**{"nuevo-nombre": "Sin persona", "nuevo-config-usuario": ""})
        self.assertFalse(BloqueOperativo.objects.filter(nombre="Sin persona").exists())
        self.assertTrue(any("usuario" in m.lower() for m in self._mensajes(respuesta)))

    def test_crear_y_editar_ofrecen_las_mismas_opciones(self):
        self._tarea(**{"nuevo-nombre": "Comparar"})
        pagina = self.client.get(self._url(self.servicio, "ejecucion")).content.decode()
        bloque = BloqueOperativo.objects.get(nombre="Comparar")
        for campo in ("tipo_actor", "modo_fechas", "requiere_evidencia", "verificacion_obligatoria", "permite_subtareas",
                      "exige_datos_inicio"):
            self.assertIn(f'name="nuevo-config-{campo}"', pagina)
            self.assertIn(f'name="bloque-{bloque.pk}-config-{campo}"', pagina)


# --- Reordenar y la interfaz de dominio del flujo (preparación de la 4.B) ---------------------------------------


class FlujoApiYReordenTests(_Base4A, TestCase):
    def setUp(self):
        self._preparar()
        self.workflow = self._flujo()
        self.servicio = self._servicio(workflow=self.workflow)
        self.config = preparar_configuracion_ejecucion(self.servicio, self.admin)
        self.recepcion = self._fase(self.workflow, "Recepción")
        self.a = self._actividad(self.config, self.recepcion, "A")
        self.b = self._actividad(self.config, self.recepcion, "B")

    def test_mover_cambia_el_orden_de_ejecucion_y_se_audita(self):
        respuesta = self.client.post(
            reverse("catalogo:studio_bloque_mover", args=[self.servicio.pk, self.b.pk]), {"direccion": "subir"}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertLess(self.b.orden, self.a.orden)
        self.assertEqual([b.nombre for b in flujo_api.actividades(self.config, fase=self.recepcion)], ["B", "A"])
        self.assertTrue(self._eventos(self.b).filter(datos_nuevos__orden=self.b.orden).exists())
        # En el borde (ya es la primera) no hace nada.
        orden = self.b.orden
        flujo_api.reordenar(self.config, self.b, self.admin, direccion=-1)
        self.b.refresh_from_db()
        self.assertEqual(self.b.orden, orden)

    def test_conexiones_distinguen_secuencia_y_rutas(self):
        conexiones = flujo_api.conexiones(self.config)
        secuencia = next(c for c in conexiones if c["origen"].pk == self.a.pk)
        self.assertEqual((secuencia["tipo"], secuencia["destino"].pk), ("secuencia", self.b.pk))
        fases = flujo_api.fases(self.config)
        self.assertEqual([f["fase"].nombre for f in fases], ["Recepción", "Revisión"])
        self.assertTrue(all(f["clave"] for f in fases))

    def test_destinos_validos_y_validar_conexion_respetan_el_motor(self):
        destinos = dict(flujo_api.destinos_validos(self.config, origen=self.a))
        self.assertIn("FIN", destinos)
        self.assertNotIn(self.a.pk, destinos)
        # Una actividad no tiene rutas: el modelo lo rechaza sin guardar nada.
        errores = flujo_api.validar_conexion(self.a, self.b, variable="x", operador="IGUAL_A", valor="1")
        self.assertTrue(errores)
        self.assertFalse(TransicionBloqueOperativo.objects.filter(bloque_origen=self.a).exists())


# --- 10/11/12. Actualización de flujos compartidos: conservar bloques y rutas, conflictos ---------------------


class ReconciliacionTests(_Base4A, TestCase):
    def setUp(self):
        self._preparar()
        self.workflow = self._flujo("Compartido 4A")
        self.servicio = self._servicio("Con flujo", workflow=self.workflow)
        config = preparar_configuracion_ejecucion(self.servicio, self.admin)
        recepcion, revision = self._fase(self.workflow, "Recepción"), self._fase(self.workflow, "Revisión")
        self.hacer = self._actividad(config, recepcion, "Hacer")
        aprobar = agregar_bloque_operativo(
            config, self.admin, fase=recepcion, tipo="APROBACION", nombre="Aprobar",
            configuracion={"modo": "SECUENCIAL", "participantes": [{"tipo": "USUARIO", "usuario_id": self.persona.pk}]},
        )
        conectar_bloques_operativos(aprobar, None, self.admin, resultado_aprobacion="APROBADA", finaliza=True)
        conectar_bloques_operativos(aprobar, None, self.admin, resultado_aprobacion="RECHAZADA", finaliza=True)
        conectar_bloques_operativos(aprobar, self.hacer, self.admin, resultado_aprobacion="DEVUELTA")
        self._actividad(config, revision, "Cerrar")
        activar_configuracion_ejecucion(self.servicio, config, self.admin)
        self.servicio.refresh_from_db()
        self.config_v1 = config

    def _publicar_version(self, cambios):
        from apps.workflows.versionamiento import activar_version as activar_wf, crear_nueva_version as crear_wf

        version = crear_wf(self.workflow, self.admin)
        cambios(version)
        activar_wf(self.workflow, version, self.admin)
        self.workflow.refresh_from_db()
        self.servicio.refresh_from_db()
        return version

    def test_las_fases_tienen_clave_estable_que_se_copia_y_no_cambia_al_renombrar(self):
        from apps.workflows import fases as fases_ops

        anterior = {f.nombre: f.clave for f in self.workflow.version_activa.fases.all()}
        version = self._publicar_version(
            lambda v: fases_ops.editar_fase(v.fases.get(nombre="Revisión"), self.admin, nombre="Revisión final")
        )
        nuevas = {f.nombre: f.clave for f in version.fases.all()}
        self.assertEqual(nuevas["Recepción"], anterior["Recepción"])
        self.assertEqual(nuevas["Revisión final"], anterior["Revisión"])

    def test_una_fase_nueva_no_hereda_la_clave_de_una_eliminada(self):
        from apps.workflows import fases as fases_ops

        anterior = self.workflow.version_activa.fases.get(nombre="Revisión").clave

        def cambios(version):
            fases_ops.eliminar_fase(version.fases.get(nombre="Revisión"), self.admin)
            nueva = fases_ops.agregar_fase(version, self.admin, nombre="Revisión")
            fases_ops.conectar_fases(version.fases.get(nombre="Recepción"), nueva, self.admin)

        version = self._publicar_version(cambios)
        self.assertNotEqual(version.fases.get(nombre="Revisión").clave, anterior)

    def test_publicar_otra_version_no_deja_al_servicio_sin_ejecucion(self):
        from apps.workflows import fases as fases_ops

        self._publicar_version(lambda v: fases_ops.editar_fase(v.fases.get(nombre="Revisión"), self.admin, nombre="Cierre"))
        self.assertTrue(reconciliacion.requiere_reconciliacion(self.servicio))
        validar_ejecucion(self.servicio)  # sigue ejecutándose sobre su versión de la plantilla

    def test_sin_conflictos_se_conservan_bloques_y_rutas(self):
        from apps.workflows import fases as fases_ops

        def cambios(version):
            fases_ops.editar_fase(version.fases.get(nombre="Revisión"), self.admin, nombre="Revisión final")
            nueva = fases_ops.agregar_fase(version, self.admin, nombre="Archivo")
            fases_ops.conectar_fases(version.fases.get(nombre="Revisión final"), nueva, self.admin)

        version = self._publicar_version(cambios)
        borrador = preparar_configuracion_ejecucion(self.servicio, self.admin)
        self.assertEqual(borrador.workflow_version_id, version.pk)
        self.assertEqual(
            sorted(borrador.bloques.values_list("nombre", flat=True)), ["Aprobar", "Cerrar", "Hacer"]
        )
        self.assertEqual(borrador.bloques.get(nombre="Cerrar").fase.nombre, "Revisión final")
        aprobar = borrador.bloques.get(nombre="Aprobar")
        rutas = {t.resultado_aprobacion: t for t in aprobar.transiciones_salientes.all()}
        self.assertEqual(rutas["DEVUELTA"].bloque_destino, borrador.bloques.get(nombre="Hacer"))
        self.assertTrue(rutas["APROBADA"].finaliza)
        self.assertEqual(aprobar.clave, self.config_v1.bloques.get(nombre="Aprobar").clave)
        activar_configuracion_ejecucion(self.servicio, borrador, self.admin)
        self.config_v1.refresh_from_db()
        self.assertEqual(self.config_v1.estado, ConfiguracionEjecucionVersion.Estado.HISTORICA)

    def test_una_fase_eliminada_con_bloques_es_un_conflicto_que_exige_decidir(self):
        from apps.workflows import fases as fases_ops

        self._publicar_version(lambda v: fases_ops.eliminar_fase(v.fases.get(nombre="Revisión"), self.admin))
        analisis = reconciliacion.analizar(self.servicio)
        self.assertEqual([c.fase.nombre for c in analisis.conflictos], ["Revisión"])
        with self.assertRaises(ValidationError):
            preparar_configuracion_ejecucion(self.servicio, self.admin)
        with self.assertRaises(ValidationError):
            reconciliacion.reconciliar(self.servicio, self.admin, resoluciones={})
        self.assertFalse(self.servicio.configuraciones_ejecucion.filter(estado="BORRADOR").exists())
        # La configuración activa no cambió.
        self.servicio.refresh_from_db()
        self.assertEqual(self.servicio.configuracion_ejecucion_activa_id, self.config_v1.pk)

    def test_mover_los_bloques_de_una_fase_eliminada(self):
        from apps.workflows import fases as fases_ops

        version = self._publicar_version(lambda v: fases_ops.eliminar_fase(v.fases.get(nombre="Revisión"), self.admin))
        conflicto = reconciliacion.analizar(self.servicio).conflictos[0]
        recepcion = version.fases.get(nombre="Recepción")
        borrador = reconciliacion.reconciliar(
            self.servicio, self.admin, resoluciones={conflicto.fase.pk: recepcion.pk}
        )
        cerrar = borrador.bloques.get(nombre="Cerrar")
        self.assertEqual(cerrar.fase_id, recepcion.pk)
        ordenes = list(borrador.bloques.filter(fase=recepcion, contenedor__isnull=True).order_by("orden").values_list("nombre", flat=True))
        self.assertEqual(ordenes[-1], "Cerrar")  # va después de los de la fase original

    def test_quitar_es_explicito_y_queda_auditado(self):
        from apps.workflows import fases as fases_ops

        self._publicar_version(lambda v: fases_ops.eliminar_fase(v.fases.get(nombre="Revisión"), self.admin))
        conflicto = reconciliacion.analizar(self.servicio).conflictos[0]
        respuesta = self.client.post(
            reverse("catalogo:studio_reconciliar", args=[self.servicio.pk]), {f"fase-{conflicto.fase.pk}": "QUITAR"}
        )
        self.assertEqual(respuesta.status_code, 302)
        borrador = self.servicio.configuraciones_ejecucion.get(estado="BORRADOR")
        self.assertFalse(borrador.bloques.filter(nombre="Cerrar").exists())
        evento = self._eventos(borrador).filter(accion=RegistroAuditoria.Accion.CREAR).latest("pk")
        self.assertEqual(evento.datos_nuevos["bloques_quitados"][0]["bloques"], ["Cerrar"])
        # La versión anterior de la configuración se conserva.
        self.assertTrue(BloqueOperativo.objects.filter(version=self.config_v1, nombre="Cerrar").exists())

    def test_la_pagina_muestra_el_cambio_y_no_edita_hasta_actualizar(self):
        from apps.workflows import fases as fases_ops

        self._publicar_version(lambda v: fases_ops.eliminar_fase(v.fases.get(nombre="Revisión"), self.admin))
        pagina = self.client.get(self._url(self.servicio, "ejecucion"))
        self.assertContains(pagina, "cambió")
        self.assertContains(pagina, "Quitar estas actividades")
        self.assertFalse(pagina.context["editable_ejecucion"])
        # Las actividades siguen a la vista (con las fases de su versión).
        self.assertContains(pagina, "Cerrar")
        estado = next(s for s in pagina.context["secciones"] if s["clave"] == "ejecucion")
        self.assertEqual(estado["estado"], "attention")

    def test_el_motor_ejecuta_la_configuracion_activa_sobre_su_version(self):
        from apps.workflows import fases as fases_ops
        from apps.workflows.motor import iniciar_workflow

        servicio = self._servicio("Con decisión", workflow=self.workflow)
        config = preparar_configuracion_ejecucion(servicio, self.admin)
        decision = agregar_bloque_operativo(
            config, self.admin, fase=self._fase(self.workflow, "Recepción"), tipo="DECISION", nombre="¿Seguir?",
        )
        conectar_bloques_operativos(decision, None, self.admin, variable="x", operador="IGUAL_A", valor="1", finaliza=True)
        conectar_bloques_operativos(decision, None, self.admin, es_fallback=True, finaliza=True)
        activar_configuracion_ejecucion(servicio, config, self.admin)
        version_original = self.workflow.version_activa
        self._publicar_version(lambda v: fases_ops.editar_fase(v.fases.get(nombre="Revisión"), self.admin, nombre="Otra"))
        instancia = iniciar_workflow(
            self.workflow, origen=RegistroAuditoria.Origen.SISTEMA,
            datos_iniciales={"configuracion_ejecucion_version_id": config.pk},
        )
        self.assertEqual(instancia.workflow_version_id, version_original.pk)

    def test_la_copia_del_flujo_conserva_la_configuracion_activa(self):
        from apps.catalogo.ejecucion import crear_copia_de_ejecucion

        self._servicio("Otro que comparte", workflow=self.workflow)
        nuevo = crear_copia_de_ejecucion(self.servicio, self.admin)
        self.servicio.refresh_from_db()
        activa = self.servicio.configuracion_ejecucion_activa
        self.assertIsNotNone(activa)
        self.assertEqual(activa.workflow_version.workflow_id, nuevo.pk)
        self.assertEqual(sorted(activa.bloques.values_list("nombre", flat=True)), ["Aprobar", "Cerrar", "Hacer"])
        # Una sola configuración ACTIVA: la anterior (del flujo original) pasó a historia, sin borrarse.
        self.config_v1.refresh_from_db()
        self.assertEqual(self.config_v1.estado, ConfiguracionEjecucionVersion.Estado.HISTORICA)
        self.assertEqual(self.servicio.configuraciones_ejecucion.filter(estado="ACTIVA").count(), 1)

    def test_el_lienzo_advierte_las_fases_en_uso(self):
        from apps.workflows.versionamiento import crear_nueva_version as crear_wf

        borrador = crear_wf(self.workflow, self.admin)
        uso = reconciliacion.fases_en_uso(self.workflow)
        clave = self.workflow.version_activa.fases.get(nombre="Revisión").clave
        self.assertEqual([s.pk for s in uso[clave]], [self.servicio.pk])
        from apps.workflows import fases as fases_ops

        fases_ops.eliminar_fase(borrador.fases.get(nombre="Revisión"), self.admin)
        afectados = reconciliacion.servicios_afectados_por(borrador)
        self.assertEqual([s.pk for s, _ in afectados], [self.servicio.pk])
        pagina = self.client.get(reverse("flujos:lienzo", args=[self.workflow.pk]))
        self.assertContains(pagina, "quita fases con actividades")


# --- 13. Publicación de versiones -------------------------------------------------------------------------------


class PublicacionDeCambiosTests(_Base4A, TestCase):
    def setUp(self):
        self._preparar()
        self.servicio = self._servicio("Publicado", activo=True)

    def _publicar(self, servicio=None):
        servicio = servicio or self.servicio
        # Bloque 2: lo publicado debe poder iniciarse (visibilidad); la atención global la da `_preparar`.
        Servicio.objects.filter(pk=servicio.pk).update(alcance_visibilidad="PUBLICO_INTERNO")
        return self.client.post(reverse("catalogo:studio_publicar", args=[servicio.pk]))

    def test_publicar_muestra_lo_activo_y_lo_pendiente_sin_cambiar_nada(self):
        borrador = crear_nueva_version(self.servicio.formulario, self.admin)
        pagina = self.client.get(self._url(self.servicio, "publicacion"))
        pendientes = pagina.context["piezas_pendientes"]
        self.assertEqual([p.clave for p in pendientes], ["formulario"])
        self.assertEqual(pagina.context["accion_publicar"], "Publicar cambios")
        self.assertIn("Formulario de inicio v2", pagina.context["activaria"])
        self.assertContains(pagina, "entrará en vigor al publicar")
        borrador.refresh_from_db()
        self.assertEqual(borrador.estado, FormularioVersion.Estado.BORRADOR)  # la simulación se deshace

    def test_publicar_cambios_activa_los_borradores_y_lo_dice(self):
        borrador = crear_nueva_version(self.servicio.formulario, self.admin)
        respuesta = self._publicar()
        borrador.refresh_from_db()
        self.assertEqual(borrador.estado, FormularioVersion.Estado.ACTIVA)
        self.assertTrue(any("Entraron en vigor" in m and "v2" in m for m in self._mensajes(respuesta)))

    def test_sin_cambios_pendientes_no_afirma_que_publico(self):
        respuesta = self._publicar()
        self.assertTrue(any("No había cambios pendientes" in m for m in self._mensajes(respuesta)))
        pagina = self.client.get(self._url(self.servicio, "publicacion"))
        self.assertEqual(pagina.context["accion_publicar"], "")
        self.assertContains(pagina, "sin cambios pendientes")

    def test_es_todo_o_nada(self):
        workflow = self._flujo("Flujo de proceso")
        proceso = self._servicio("Proceso", tipo=Servicio.Tipo.PROCESO, workflow=workflow)
        preparar_configuracion_ejecucion(proceso, self.admin)  # borrador sin bloques: inválido
        borrador_form = crear_nueva_version(proceso.formulario, self.admin)
        respuesta = self._publicar(proceso)
        borrador_form.refresh_from_db()
        proceso.refresh_from_db()
        self.assertEqual(borrador_form.estado, FormularioVersion.Estado.BORRADOR)
        self.assertFalse(proceso.activo)
        self.assertTrue(any("No se publicó nada" in m for m in self._mensajes(respuesta)))

    def test_publicar_un_servicio_nuevo_activa_y_publica(self):
        nuevo = self._servicio("Nuevo")
        self._publicar(nuevo)
        nuevo.refresh_from_db()
        self.assertTrue(nuevo.activo)

    def test_piezas_informan_la_configuracion_del_flujo(self):
        workflow = self._flujo("Flujo piezas")
        servicio = self._servicio("Con flujo", workflow=workflow)
        config = preparar_configuracion_ejecucion(servicio, self.admin)
        self._actividad(config, self._fase(workflow, "Recepción"), "Atender")
        flujo = next(p for p in publicacion.piezas(servicio) if p.clave == "flujo")
        self.assertTrue(flujo.pendiente)
        self.assertIsNone(flujo.activa)


# --- 14/15. Permisos, edición de opciones y reglas, entrega retirable ---------------------------------------------


class PermisosYEdicionTests(_Base4A, TestCase):
    def setUp(self):
        self._preparar()
        self.workflow = self._flujo()
        self.servicio = self._servicio(workflow=self.workflow)
        self.config = preparar_configuracion_ejecucion(self.servicio, self.admin)
        self.limitado = Usuario.objects.create_user("f4a_limitado", password=CLAVE)
        _permiso_get_or_create(self.limitado, "catalogo.administrar", "Rol 4A limitado")

    def test_sin_permiso_de_formularios_no_se_ofrecen_controles_inutiles(self):
        self.client.login(username="f4a_limitado", password=CLAVE)
        pagina = self.client.get(self._url(self.servicio, "entrada"))
        self.assertContains(pagina, "requiere el permiso de administrar formularios")
        self.assertNotContains(pagina, "Editar formulario")
        self.assertNotContains(pagina, "+ Campo")

    def test_sin_permiso_de_flujos_no_se_ofrece_agregar_ni_reordenar(self):
        bloque = self._actividad(self.config, self._fase(self.workflow, "Recepción"), "Atender")
        self.client.login(username="f4a_limitado", password=CLAVE)
        pagina = self.client.get(self._url(self.servicio, "ejecucion"))
        self.assertContains(pagina, "requiere el permiso de administrar flujos")
        self.assertNotContains(pagina, "Agregar actividad")
        respuesta = self.client.post(
            reverse("catalogo:studio_bloque_mover", args=[self.servicio.pk, bloque.pk]), {"direccion": "subir"}
        )
        self.assertEqual(respuesta.status_code, 403)

    def test_editar_una_opcion_de_lista(self):
        borrador = crear_nueva_version(self.servicio.formulario, self.admin)
        campo = Campo.objects.create(version=borrador, tipo="LISTA", etiqueta="Canal", orden=1)
        opcion = OpcionCampo.objects.create(campo=campo, valor="ig", etiqueta="Insta", orden=0)
        prefijo = f"campo-{campo.pk}-opcion-{opcion.pk}"
        self.client.post(
            reverse("catalogo:studio_opcion_editar", args=[self.servicio.pk, campo.pk, opcion.pk]),
            {f"{prefijo}-valor": "ig", f"{prefijo}-etiqueta": "Instagram", f"{prefijo}-orden": "0"},
        )
        opcion.refresh_from_db()
        self.assertEqual(opcion.etiqueta, "Instagram")
        self.assertContains(self.client.get(self._url(self.servicio, "entrada")), "Editar opción")

    def test_editar_una_regla_condicional(self):
        borrador = crear_nueva_version(self.servicio.formulario, self.admin)
        origen = Campo.objects.create(version=borrador, tipo="TEXTO", etiqueta="Tipo", orden=1)
        objetivo = Campo.objects.create(version=borrador, tipo="TEXTO", etiqueta="Detalle", orden=2)
        regla = ReglaCondicional.objects.create(
            campo_origen=origen, operador="IGUAL_A", valor="otro", campo_objetivo=objetivo, efecto="MOSTRAR"
        )
        prefijo = f"regla-{regla.pk}-editar"
        self.client.post(
            reverse("catalogo:studio_regla_editar", args=[self.servicio.pk, regla.pk]),
            {f"{prefijo}-campo_origen": origen.pk, f"{prefijo}-operador": "IGUAL_A", f"{prefijo}-valor": "otro",
             f"{prefijo}-campo_objetivo": objetivo.pk, f"{prefijo}-efecto": "REQUERIR"},
        )
        regla.refresh_from_db()
        self.assertEqual(regla.efecto, "REQUERIR")
        self.assertContains(self.client.get(self._url(self.servicio, "entrada")), "Editar regla")

    def test_la_entrega_formal_se_puede_retirar_y_se_audita(self):
        configurar_politica_entrega(self.servicio, self.admin, politica="CIERRE_DIRECTO")
        self.client.post(reverse("catalogo:studio_entrega_guardar", args=[self.servicio.pk]), {"politica": "SIN_ENTREGA"})
        self.servicio.refresh_from_db()
        self.assertEqual((self.servicio.politica_entrega, self.servicio.dias_observacion), ("", None))
        evento = self._eventos(self.servicio).latest("pk")
        self.assertEqual(evento.datos_nuevos, {"politica_entrega": "", "dias_observacion": None})

    def test_responsables_generales_en_basico_tambien_en_un_servicio(self):
        self.assertContains(self.client.get(self._url(self.servicio, "general")), 'id="responsables-generales"')


# --- 19/20. Procesos programados y flujos LEGACY ----------------------------------------------------------------


class ProgramadosYLegacyTests(_Base4A, TestCase):
    def setUp(self):
        self._preparar()

    def test_un_proceso_programado_no_ofrece_al_solicitante_como_responsable(self):
        from apps.catalogo.models import ProgramacionProceso, ServicioResponsable

        workflow = self._flujo()
        proceso = self._servicio("Programado", tipo=Servicio.Tipo.PROCESO, workflow=workflow)
        responsable = ServicioResponsable.objects.create(
            servicio=proceso, tipo_responsable="USUARIO", usuario=self.persona
        )
        ProgramacionProceso.objects.create(
            servicio=proceso, activa=True, dia_creacion=1, periodo="MES_ACTUAL", responsable_inicial=responsable,
            activada_desde=date(2026, 1, 1),
        )
        preparar_configuracion_ejecucion(proceso, self.admin)
        pagina = self.client.get(self._url(proceso, "ejecucion"))
        opciones = [v for v, _ in pagina.context["form_actividad_nuevo"].fields["tipo_actor"].choices]
        self.assertNotIn("SOLICITANTE", opciones)
        # La entrega formal se puede retirar aunque esté programado (no se puede DEFINIR).
        configurar_politica_entrega(proceso, self.admin, politica="")
        with self.assertRaises(ValidationError):
            configurar_politica_entrega(proceso, self.admin, politica="CIERRE_DIRECTO")

    def test_un_flujo_legacy_conserva_su_publicacion_y_no_se_reconcilia(self):
        from apps.workflows.models import Workflow

        legado = Workflow.objects.create(nombre="Legado", modo=Workflow.Modo.LEGACY_EJECUTABLE)
        servicio = self._servicio("Legado", workflow=legado)
        self.assertFalse(reconciliacion.requiere_reconciliacion(servicio))
        pagina = self.client.get(self._url(servicio, "publicacion"))
        self.assertEqual(pagina.status_code, 200)
        self.assertTrue(pagina.context["es_legacy"])
        self.assertEqual(dependencias.detectar(servicio), [])
