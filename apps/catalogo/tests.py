"""Pruebas de `apps.catalogo` — incrementos 1.1 (Catálogo) y 1.2 (Form Builder).

Único archivo de pruebas de esta app (misma decisión que `apps.core`: sin
paquete `tests/`). No se ejecutan como parte de la implementación — se
entregan junto con los comandos exactos para correrlas vía Docker.
"""

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.catalogo.admin import CampoAdmin, FormularioAdmin, FormularioVersionAdmin, ServicioAdmin
from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import (
    Campo,
    Categoria,
    Formulario,
    FormularioVersion,
    OpcionCampo,
    ReglaCondicional,
    Servicio,
    ServicioResponsable,
    ServicioVisibilidad,
)
from apps.catalogo.reglas import EspecificacionRegla
from apps.catalogo.versionamiento import activar_version, crear_nueva_version
from apps.catalogo.visibilidad import servicios_visibles_para
from apps.core.models import (
    Area,
    AsignacionRol,
    Equipo,
    Permiso,
    RegistroAuditoria,
    RolFuncional,
    RolPermiso,
    UnidadNegocio,
    UsuarioArea,
    UsuarioUnidadNegocio,
)

Usuario = get_user_model()

CLAVE_PRUEBA = "Clave-Segura-123"


def _otorgar_permiso(usuario, codigo, *, nombre_rol=None):
    """Helper de pruebas: crea un `RolFuncional` con `codigo` en alcance
    GLOBAL y lo asigna a `usuario`. Evita repetir las mismas 3 líneas en
    cada test de autorización de 1.2.
    """
    permiso = Permiso.objects.get(codigo=codigo)
    rol = RolFuncional.objects.create(nombre=nombre_rol or f"Rol {codigo}")
    RolPermiso.objects.create(rol=rol, permiso=permiso)
    AsignacionRol.objects.create(usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL)
    return rol


class CatalogoTests(TestCase):
    """CU-010 Gestionar catálogo de servicios (RQF-029/030/031, RN-002)."""

    def test_crear_categoria(self):
        categoria = Categoria.objects.create(nombre="Tecnología")
        self.assertTrue(categoria.activo)

    def test_crear_servicio_es_restringido_por_defecto(self):
        # RN-038: el valor por defecto es RESTRINGIDO, evita publicación accidental
        categoria = Categoria.objects.create(nombre="Tecnología")
        servicio = Servicio.objects.create(nombre="Soporte técnico", categoria=categoria)
        self.assertTrue(servicio.activo)
        self.assertEqual(servicio.alcance_visibilidad, Servicio.AlcanceVisibilidad.RESTRINGIDO)

    def test_desactivar_servicio_no_elimina_registro(self):
        # RQF-031, RN-002
        categoria = Categoria.objects.create(nombre="Tecnología")
        servicio = Servicio.objects.create(nombre="Soporte técnico", categoria=categoria)
        servicio.activo = False
        servicio.save()
        self.assertTrue(Servicio.objects.filter(pk=servicio.pk, activo=False).exists())


class ServicioVisibilidadTests(TestCase):
    """RQF-032, RN-038 — integridad de `ServicioVisibilidad`."""

    def setUp(self):
        categoria = Categoria.objects.create(nombre="Tecnología")
        self.servicio = Servicio.objects.create(nombre="Soporte técnico", categoria=categoria)
        self.usuario = Usuario.objects.create_user(username="mvargas", password=CLAVE_PRUEBA)
        self.area = Area.objects.create(nombre="TIC", codigo="TIC-CAT")
        self.unidad = UnidadNegocio.objects.create(nombre="Infraestructura", codigo="INFRA-CAT")

    def test_constraint_usuario_exige_usuario_y_rechaza_area_unidad(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioVisibilidad.objects.create(
                    servicio=self.servicio,
                    tipo_alcance=ServicioVisibilidad.TipoAlcance.USUARIO,
                    area=self.area,
                )

    def test_constraint_area_exige_area_y_rechaza_unidad(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioVisibilidad.objects.create(
                    servicio=self.servicio,
                    tipo_alcance=ServicioVisibilidad.TipoAlcance.AREA,
                    unidad_negocio=self.unidad,
                )

    def test_constraint_unidad_exige_unidad_y_rechaza_area(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioVisibilidad.objects.create(
                    servicio=self.servicio,
                    tipo_alcance=ServicioVisibilidad.TipoAlcance.UNIDAD,
                    area=self.area,
                )

    def test_no_permite_duplicar_visibilidad_usuario_activa(self):
        ServicioVisibilidad.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.USUARIO, usuario=self.usuario
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioVisibilidad.objects.create(
                    servicio=self.servicio,
                    tipo_alcance=ServicioVisibilidad.TipoAlcance.USUARIO,
                    usuario=self.usuario,
                )

    def test_no_permite_duplicar_visibilidad_area_activa(self):
        ServicioVisibilidad.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.AREA, area=self.area
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioVisibilidad.objects.create(
                    servicio=self.servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.AREA, area=self.area
                )

    def test_no_permite_duplicar_visibilidad_unidad_activa(self):
        ServicioVisibilidad.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.UNIDAD, unidad_negocio=self.unidad
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioVisibilidad.objects.create(
                    servicio=self.servicio,
                    tipo_alcance=ServicioVisibilidad.TipoAlcance.UNIDAD,
                    unidad_negocio=self.unidad,
                )


class ServicioResponsableTests(TestCase):
    """RQF-033, RN-009 — integridad de `ServicioResponsable`."""

    def setUp(self):
        categoria = Categoria.objects.create(nombre="Tecnología")
        self.servicio = Servicio.objects.create(nombre="Soporte técnico", categoria=categoria)
        self.usuario = Usuario.objects.create_user(username="jgarcia", password=CLAVE_PRUEBA)
        self.equipo = Equipo.objects.create(nombre="Mesa de ayuda")

    def test_constraint_usuario_exige_usuario_y_rechaza_equipo(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioResponsable.objects.create(
                    servicio=self.servicio,
                    tipo_responsable=ServicioResponsable.TipoResponsable.USUARIO,
                    equipo=self.equipo,
                )

    def test_constraint_equipo_exige_equipo_y_rechaza_usuario(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioResponsable.objects.create(
                    servicio=self.servicio,
                    tipo_responsable=ServicioResponsable.TipoResponsable.EQUIPO,
                    usuario=self.usuario,
                )

    def test_no_permite_duplicar_responsable_usuario_activo(self):
        ServicioResponsable.objects.create(
            servicio=self.servicio,
            tipo_responsable=ServicioResponsable.TipoResponsable.USUARIO,
            usuario=self.usuario,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioResponsable.objects.create(
                    servicio=self.servicio,
                    tipo_responsable=ServicioResponsable.TipoResponsable.USUARIO,
                    usuario=self.usuario,
                )

    def test_no_permite_duplicar_responsable_equipo_activo(self):
        ServicioResponsable.objects.create(
            servicio=self.servicio, tipo_responsable=ServicioResponsable.TipoResponsable.EQUIPO, equipo=self.equipo
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioResponsable.objects.create(
                    servicio=self.servicio,
                    tipo_responsable=ServicioResponsable.TipoResponsable.EQUIPO,
                    equipo=self.equipo,
                )

    def test_pertenencia_organizacional_no_otorga_responsabilidad(self):
        # RN-009
        area = Area.objects.create(nombre="TIC", codigo="TIC-RESP")
        UsuarioArea.objects.create(usuario=self.usuario, area=area)
        self.assertEqual(self.servicio.responsables.count(), 0)


class VisibilidadCatalogoTests(TestCase):
    """CU-011 Consultar catálogo — `servicios_visibles_para()`. RN-038."""

    def setUp(self):
        self.categoria = Categoria.objects.create(nombre="Tecnología")
        self.usuario = Usuario.objects.create_user(username="lrios", password=CLAVE_PRUEBA)
        self.area = Area.objects.create(nombre="TIC", codigo="TIC-VIS")
        self.unidad = UnidadNegocio.objects.create(nombre="Infraestructura", codigo="INFRA-VIS")

    def test_publico_interno_visible_para_cualquier_autenticado(self):
        servicio = Servicio.objects.create(
            nombre="Servicio público",
            categoria=self.categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO,
        )
        self.assertIn(servicio, servicios_visibles_para(self.usuario))

    def test_restringido_visible_por_concesion_directa_a_usuario(self):
        servicio = Servicio.objects.create(
            nombre="Servicio restringido",
            categoria=self.categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.RESTRINGIDO,
        )
        ServicioVisibilidad.objects.create(
            servicio=servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.USUARIO, usuario=self.usuario
        )
        self.assertIn(servicio, servicios_visibles_para(self.usuario))

    def test_restringido_visible_por_area(self):
        servicio = Servicio.objects.create(
            nombre="Servicio por área",
            categoria=self.categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.RESTRINGIDO,
        )
        UsuarioArea.objects.create(usuario=self.usuario, area=self.area)
        ServicioVisibilidad.objects.create(
            servicio=servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.AREA, area=self.area
        )
        self.assertIn(servicio, servicios_visibles_para(self.usuario))

    def test_restringido_visible_por_unidad(self):
        servicio = Servicio.objects.create(
            nombre="Servicio por unidad",
            categoria=self.categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.RESTRINGIDO,
        )
        UsuarioUnidadNegocio.objects.create(usuario=self.usuario, unidad_negocio=self.unidad)
        ServicioVisibilidad.objects.create(
            servicio=servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.UNIDAD, unidad_negocio=self.unidad
        )
        self.assertIn(servicio, servicios_visibles_para(self.usuario))

    def test_restringido_sin_concesion_no_es_visible(self):
        # RN-038: la ausencia de concesiones no otorga visibilidad
        servicio = Servicio.objects.create(
            nombre="Servicio sin conceder",
            categoria=self.categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.RESTRINGIDO,
        )
        self.assertNotIn(servicio, servicios_visibles_para(self.usuario))

    def test_servicio_inactivo_nunca_es_visible(self):
        servicio = Servicio.objects.create(
            nombre="Servicio inactivo",
            categoria=self.categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO,
            activo=False,
        )
        self.assertNotIn(servicio, servicios_visibles_para(self.usuario))

    def test_sin_duplicados_cuando_hay_varias_concesiones_simultaneas(self):
        servicio = Servicio.objects.create(
            nombre="Servicio doble concesión",
            categoria=self.categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.RESTRINGIDO,
        )
        UsuarioArea.objects.create(usuario=self.usuario, area=self.area)
        ServicioVisibilidad.objects.create(
            servicio=servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.USUARIO, usuario=self.usuario
        )
        ServicioVisibilidad.objects.create(
            servicio=servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.AREA, area=self.area
        )
        resultado = list(servicios_visibles_para(self.usuario))
        self.assertEqual(resultado.count(servicio), 1)


class CatalogoVistasTests(TestCase):
    """CU-011 — vistas de lista y detalle (RQF-036/037)."""

    def setUp(self):
        self.categoria = Categoria.objects.create(nombre="Tecnología")
        self.usuario = Usuario.objects.create_user(username="pariza", password=CLAVE_PRUEBA)
        self.publico = Servicio.objects.create(
            nombre="Servicio público",
            categoria=self.categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO,
        )
        self.restringido_no_concedido = Servicio.objects.create(
            nombre="Servicio ajeno",
            categoria=self.categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.RESTRINGIDO,
        )

    def test_lista_exige_autenticacion(self):
        respuesta = self.client.get(reverse("catalogo:lista"))
        self.assertEqual(respuesta.status_code, 302)

    def test_lista_solo_muestra_servicios_visibles(self):
        self.client.login(username="pariza", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("catalogo:lista"))
        self.assertContains(respuesta, "Servicio público")
        self.assertNotContains(respuesta, "Servicio ajeno")

    def test_lista_filtra_por_texto(self):
        self.client.login(username="pariza", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("catalogo:lista"), {"q": "público"})
        self.assertContains(respuesta, "Servicio público")

    def test_lista_filtra_por_categoria(self):
        otra_categoria = Categoria.objects.create(nombre="Administrativos")
        Servicio.objects.create(
            nombre="Otro servicio",
            categoria=otra_categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO,
        )
        self.client.login(username="pariza", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("catalogo:lista"), {"categoria": self.categoria.pk})
        self.assertContains(respuesta, "Servicio público")
        self.assertNotContains(respuesta, "Otro servicio")

    def test_detalle_exige_autenticacion(self):
        respuesta = self.client.get(reverse("catalogo:detalle", args=[self.publico.pk]))
        self.assertEqual(respuesta.status_code, 302)

    def test_detalle_accesible_si_es_visible(self):
        self.client.login(username="pariza", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("catalogo:detalle", args=[self.publico.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Servicio público")

    def test_detalle_404_si_no_es_visible(self):
        # Acceso directo por URL respeta la misma visibilidad que la lista
        self.client.login(username="pariza", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("catalogo:detalle", args=[self.restringido_no_concedido.pk]))
        self.assertEqual(respuesta.status_code, 404)


class CatalogoAdminAutorizacionTests(TestCase):
    """CU-010 — `catalogo.administrar` gatea Django Admin, alcance GLOBAL."""

    def setUp(self):
        self.permiso = Permiso.objects.get(codigo="catalogo.administrar")
        self.rol = RolFuncional.objects.create(nombre="Gestor de Catálogo")
        RolPermiso.objects.create(rol=self.rol, permiso=self.permiso)
        self.usuario_con_permiso = Usuario.objects.create_user(
            username="gcastro", password=CLAVE_PRUEBA, is_staff=True
        )
        AsignacionRol.objects.create(
            usuario=self.usuario_con_permiso, rol=self.rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
        )
        self.usuario_sin_permiso = Usuario.objects.create_user(
            username="staff_sin_permiso_cat", password=CLAVE_PRUEBA, is_staff=True
        )

    def test_sin_permiso_no_puede_ver_catalogo_en_admin(self):
        admin_instance = ServicioAdmin(Servicio, django_admin.site)
        request = RequestFactory().get("/admin/catalogo/servicio/")
        request.user = self.usuario_sin_permiso
        self.assertFalse(admin_instance.has_view_permission(request))

    def test_con_permiso_global_puede_administrar_catalogo(self):
        admin_instance = ServicioAdmin(Servicio, django_admin.site)
        request = RequestFactory().get("/admin/catalogo/servicio/")
        request.user = self.usuario_con_permiso
        self.assertTrue(admin_instance.has_view_permission(request))
        self.assertTrue(admin_instance.has_add_permission(request))
        self.assertTrue(admin_instance.has_change_permission(request))


class CatalogoAuditoriaTests(TestCase):
    """CU-010 — auditoría de cambios administrativos (RQF-116/120)."""

    def setUp(self):
        self.staff = Usuario.objects.create_user(
            username="auditor_catalogo", password=CLAVE_PRUEBA, is_staff=True, is_superuser=True
        )
        permiso = Permiso.objects.get(codigo="catalogo.administrar")
        rol = RolFuncional.objects.create(nombre="Gestor de Catálogo (auditoría)")
        RolPermiso.objects.create(rol=rol, permiso=permiso)
        AsignacionRol.objects.create(usuario=self.staff, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL)
        self.categoria = Categoria.objects.create(nombre="Tecnología")

    def test_crear_servicio_via_admin_registra_evento_crear(self):
        self.client.login(username="auditor_catalogo", password=CLAVE_PRUEBA)
        datos = {
            "nombre": "Nuevo servicio",
            "descripcion": "",
            "categoria": self.categoria.pk,
            "instrucciones": "",
            "activo": "on",
            "alcance_visibilidad": Servicio.AlcanceVisibilidad.RESTRINGIDO,
            "visibilidad-TOTAL_FORMS": "0",
            "visibilidad-INITIAL_FORMS": "0",
            "visibilidad-MIN_NUM_FORMS": "0",
            "visibilidad-MAX_NUM_FORMS": "1000",
            "responsables-TOTAL_FORMS": "0",
            "responsables-INITIAL_FORMS": "0",
            "responsables-MIN_NUM_FORMS": "0",
            "responsables-MAX_NUM_FORMS": "1000",
        }
        respuesta = self.client.post(reverse("admin:catalogo_servicio_add"), datos)
        self.assertEqual(respuesta.status_code, 302)
        servicio = Servicio.objects.get(nombre="Nuevo servicio")
        evento = RegistroAuditoria.objects.get(modelo="catalogo.servicio", object_id=servicio.pk)
        self.assertEqual(evento.accion, RegistroAuditoria.Accion.CREAR)


# ---------------------------------------------------------------------------
# Incremento 1.2 — Form Builder (CU-012/013, RQF-034/038-047, RN-012/013)
# ---------------------------------------------------------------------------


class FormularioModelosTests(TestCase):
    """Guarda contra desincronización entre `Campo.TipoCampo` (14 tipos de
    RQF-040) y `ESTRATEGIAS_POR_TIPO` (campos.py)."""

    def test_estrategias_cubren_todos_los_tipos_campo(self):
        self.assertEqual(set(ESTRATEGIAS_POR_TIPO), set(Campo.TipoCampo.values))


class FormularioVersionamientoTests(TestCase):
    """RQF-046/047, RN-013 — `crear_nueva_version`."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="gdiaz", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="Solicitud de equipo")

    def test_primera_version_es_borrador_vacia(self):
        version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.assertEqual(version.numero, 1)
        self.assertEqual(version.estado, FormularioVersion.Estado.BORRADOR)
        self.assertEqual(version.campos.count(), 0)

    def test_siguiente_numero_es_incremental(self):
        v1 = crear_nueva_version(self.formulario, actor=self.usuario)
        activar_version(self.formulario, v1, actor=self.usuario)
        v2 = crear_nueva_version(self.formulario, actor=self.usuario)
        self.assertEqual(v2.numero, 2)

    def test_clona_campos_opciones_y_reglas_de_la_version_activa(self):
        v1 = crear_nueva_version(self.formulario, actor=self.usuario)
        origen = Campo.objects.create(version=v1, tipo=Campo.TipoCampo.LISTA, etiqueta="Prioridad", orden=1)
        OpcionCampo.objects.create(campo=origen, valor="ALTA", etiqueta="Alta")
        objetivo = Campo.objects.create(version=v1, tipo=Campo.TipoCampo.TEXTO, etiqueta="Justificación", orden=2)
        ReglaCondicional.objects.create(
            campo_origen=origen,
            operador=ReglaCondicional.Operador.IGUAL_A,
            valor="ALTA",
            campo_objetivo=objetivo,
            efecto=ReglaCondicional.Efecto.REQUERIR,
        )
        activar_version(self.formulario, v1, actor=self.usuario)

        v2 = crear_nueva_version(self.formulario, actor=self.usuario)
        self.assertEqual(v2.campos.count(), 2)
        self.assertEqual(
            set(v2.campos.values_list("pk", flat=True)) & set(v1.campos.values_list("pk", flat=True)), set()
        )
        clon_origen = v2.campos.get(etiqueta="Prioridad")
        self.assertEqual(clon_origen.opciones.count(), 1)
        self.assertEqual(ReglaCondicional.objects.filter(campo_origen__version=v2).count(), 1)

    def test_clonar_desde_version_historica_permite_recuperar_configuracion(self):
        v1 = crear_nueva_version(self.formulario, actor=self.usuario)
        Campo.objects.create(version=v1, tipo=Campo.TipoCampo.TEXTO, etiqueta="Campo v1")
        activar_version(self.formulario, v1, actor=self.usuario)
        v2 = crear_nueva_version(self.formulario, actor=self.usuario)
        activar_version(self.formulario, v2, actor=self.usuario)
        v1.refresh_from_db()
        self.assertEqual(v1.estado, FormularioVersion.Estado.HISTORICA)

        v3 = crear_nueva_version(self.formulario, actor=self.usuario, clonar_desde=v1)
        self.assertEqual(v3.campos.first().etiqueta, "Campo v1")


class ActivarVersionTests(TestCase):
    """RQF-047 — `activar_version`."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="lvega", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="Solicitud de acceso")

    def test_activar_version_borrador(self):
        version = crear_nueva_version(self.formulario, actor=self.usuario)
        activar_version(self.formulario, version, actor=self.usuario)
        version.refresh_from_db()
        self.formulario.refresh_from_db()
        self.assertEqual(version.estado, FormularioVersion.Estado.ACTIVA)
        self.assertEqual(self.formulario.version_activa_id, version.pk)

    def test_activar_nueva_version_degrada_la_anterior_a_historica(self):
        v1 = crear_nueva_version(self.formulario, actor=self.usuario)
        activar_version(self.formulario, v1, actor=self.usuario)
        v2 = crear_nueva_version(self.formulario, actor=self.usuario)
        activar_version(self.formulario, v2, actor=self.usuario)
        v1.refresh_from_db()
        self.assertEqual(v1.estado, FormularioVersion.Estado.HISTORICA)

    def test_no_permite_activar_version_historica_directamente(self):
        v1 = crear_nueva_version(self.formulario, actor=self.usuario)
        activar_version(self.formulario, v1, actor=self.usuario)
        v2 = crear_nueva_version(self.formulario, actor=self.usuario)
        activar_version(self.formulario, v2, actor=self.usuario)
        v1.refresh_from_db()
        with self.assertRaises(ValueError):
            activar_version(self.formulario, v1, actor=self.usuario)

    def test_no_permite_activar_version_de_otro_formulario(self):
        otro_formulario = Formulario.objects.create(nombre="Otro")
        version_ajena = crear_nueva_version(otro_formulario, actor=self.usuario)
        with self.assertRaises(ValueError):
            activar_version(self.formulario, version_ajena, actor=self.usuario)


class InmutabilidadVersionTests(TestCase):
    """RN-013 — una versión fuera de BORRADOR es estructuralmente inmutable
    por las vías ordinarias de dominio/Admin."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="rmora", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="Solicitud de viaje")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.campo = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Destino")

    def test_permite_editar_campo_en_borrador(self):
        self.campo.etiqueta = "Destino final"
        self.campo.save()
        self.campo.refresh_from_db()
        self.assertEqual(self.campo.etiqueta, "Destino final")

    def test_no_permite_editar_campo_de_version_activa(self):
        activar_version(self.formulario, self.version, actor=self.usuario)
        self.campo.etiqueta = "Cambio no permitido"
        with self.assertRaises(ValidationError):
            self.campo.save()

    def test_no_permite_agregar_campo_a_version_activa(self):
        activar_version(self.formulario, self.version, actor=self.usuario)
        with self.assertRaises(ValidationError):
            Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.NUMERO, etiqueta="Nuevo")

    def test_no_permite_eliminar_campo_de_version_activa(self):
        activar_version(self.formulario, self.version, actor=self.usuario)
        with self.assertRaises(ValidationError):
            self.campo.delete()

    def test_no_permite_editar_opcion_de_version_activa(self):
        campo_lista = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.LISTA, etiqueta="Prioridad")
        opcion = OpcionCampo.objects.create(campo=campo_lista, valor="A", etiqueta="Alta")
        activar_version(self.formulario, self.version, actor=self.usuario)
        opcion.etiqueta = "Cambiada"
        with self.assertRaises(ValidationError):
            opcion.save()

    def test_no_permite_editar_regla_de_version_activa(self):
        objetivo = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Detalle")
        regla = ReglaCondicional.objects.create(
            campo_origen=self.campo,
            operador=ReglaCondicional.Operador.NO_ESTA_VACIO,
            campo_objetivo=objetivo,
            efecto=ReglaCondicional.Efecto.MOSTRAR,
        )
        activar_version(self.formulario, self.version, actor=self.usuario)
        regla.valor = "x"
        with self.assertRaises(ValidationError):
            regla.save()


class EstrategiaTextoTests(TestCase):
    """RQF-040/041 — familia TEXTO/TEXTO_LARGO/CORREO/URL."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="atorres", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="F")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)

    def test_texto_respeta_longitud_maxima(self):
        campo = Campo.objects.create(
            version=self.version,
            tipo=Campo.TipoCampo.TEXTO,
            etiqueta="Nombre",
            configuracion={"longitud_maxima": 5},
        )
        estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
        with self.assertRaises(ValidationError):
            estrategia.validar_valor(campo, "más de cinco caracteres")

    def test_correo_valida_formato(self):
        campo = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.CORREO, etiqueta="Correo")
        estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
        with self.assertRaises(ValidationError):
            estrategia.validar_valor(campo, "no-es-un-correo")
        estrategia.validar_valor(campo, "valido@ejemplo.com")

    def test_url_valida_formato(self):
        campo = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.URL, etiqueta="Enlace")
        estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
        with self.assertRaises(ValidationError):
            estrategia.validar_valor(campo, "no es una url")

    def test_configuracion_rechaza_clave_desconocida(self):
        campo = Campo(
            version=self.version, tipo=Campo.TipoCampo.TEXTO, etiqueta="X", configuracion={"clave_rara": 1}
        )
        with self.assertRaises(ValidationError):
            campo.clean()


class EstrategiaNumericaTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="bcruz", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="F")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.campo = Campo.objects.create(
            version=self.version,
            tipo=Campo.TipoCampo.NUMERO,
            etiqueta="Edad",
            configuracion={"minimo": 0, "maximo": 100},
        )
        self.estrategia = ESTRATEGIAS_POR_TIPO[Campo.TipoCampo.NUMERO]

    def test_rechaza_fuera_de_rango(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(self.campo, 150)

    def test_rechaza_decimales_si_no_estan_permitidos(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(self.campo, 10.5)

    def test_acepta_valor_valido(self):
        self.estrategia.validar_valor(self.campo, 42)

    def test_configuracion_rechaza_minimo_mayor_que_maximo(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_configuracion({"minimo": 10, "maximo": 5})

    def test_normalizar_valor_no_numerico_lanza_validationerror_controlado(self):
        # Corrección 2.2: antes `float("abc")` dejaba propagar un
        # ValueError sin controlar; ahora es el mismo tipo de error
        # controlado que usa el resto del Form Builder.
        with self.assertRaises(ValidationError):
            self.estrategia.normalizar("abc")
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(self.campo, "abc")


class EstrategiaTemporalTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="cnieto", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="F")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.campo = Campo.objects.create(
            version=self.version,
            tipo=Campo.TipoCampo.FECHA,
            etiqueta="Fecha límite",
            configuracion={"fecha_minima": "2026-01-01"},
        )
        self.estrategia = ESTRATEGIAS_POR_TIPO[Campo.TipoCampo.FECHA]

    def test_rechaza_fecha_anterior_a_la_minima(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(self.campo, "2025-01-01")

    def test_acepta_fecha_valida(self):
        self.estrategia.validar_valor(self.campo, "2026-06-01")

    def test_rechaza_formato_invalido(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(self.campo, "no-es-fecha")


class EstrategiaSeleccionTests(TestCase):
    """RQF-042 — opciones en `OpcionCampo`, nunca en el JSON."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="dlopez", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="F")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.lista = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.LISTA, etiqueta="Prioridad")
        OpcionCampo.objects.create(campo=self.lista, valor="ALTA", etiqueta="Alta")
        OpcionCampo.objects.create(campo=self.lista, valor="BAJA", etiqueta="Baja")
        self.multilista = Campo.objects.create(
            version=self.version,
            tipo=Campo.TipoCampo.MULTILISTA,
            etiqueta="Etiquetas",
            configuracion={"minimo_selecciones": 1},
        )
        OpcionCampo.objects.create(campo=self.multilista, valor="A", etiqueta="A")
        OpcionCampo.objects.create(campo=self.multilista, valor="B", etiqueta="B")

    def test_lista_rechaza_opcion_no_registrada(self):
        estrategia = ESTRATEGIAS_POR_TIPO[Campo.TipoCampo.LISTA]
        with self.assertRaises(ValidationError):
            estrategia.validar_valor(self.lista, "MEDIA")

    def test_lista_acepta_opcion_valida(self):
        estrategia = ESTRATEGIAS_POR_TIPO[Campo.TipoCampo.LISTA]
        estrategia.validar_valor(self.lista, "ALTA")

    def test_multilista_rechaza_seleccion_vacia_si_hay_minimo(self):
        estrategia = ESTRATEGIAS_POR_TIPO[Campo.TipoCampo.MULTILISTA]
        with self.assertRaises(ValidationError):
            estrategia.validar_valor(self.multilista, [])

    def test_multilista_rechaza_opcion_invalida(self):
        estrategia = ESTRATEGIAS_POR_TIPO[Campo.TipoCampo.MULTILISTA]
        with self.assertRaises(ValidationError):
            estrategia.validar_valor(self.multilista, ["C"])

    def test_multilista_acepta_seleccion_valida(self):
        estrategia = ESTRATEGIAS_POR_TIPO[Campo.TipoCampo.MULTILISTA]
        estrategia.validar_valor(self.multilista, ["A", "B"])


class EstrategiaBooleanoTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="ejara", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="F")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.campo = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.BOOLEANO, etiqueta="Acepta")
        self.estrategia = ESTRATEGIAS_POR_TIPO[Campo.TipoCampo.BOOLEANO]

    def test_acepta_valor_booleano(self):
        self.estrategia.validar_valor(self.campo, True)

    def test_rechaza_valor_no_booleano(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(self.campo, "tal vez")


class EstrategiaArchivoTests(TestCase):
    """La carga real (RQF-007) es Sprint 2 — aquí solo se valida
    configuración y metadatos simulados (ver `campos.py::EstrategiaArchivo`).
    """

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="fperez", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="F")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.campo = Campo.objects.create(
            version=self.version,
            tipo=Campo.TipoCampo.ARCHIVO,
            etiqueta="Soporte",
            configuracion={"extensiones_permitidas": ["pdf"], "tamano_maximo_mb": 5},
        )
        self.estrategia = ESTRATEGIAS_POR_TIPO[Campo.TipoCampo.ARCHIVO]

    def test_rechaza_extension_no_permitida(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(self.campo, {"nombre": "foto.png", "tamano_mb": 1})

    def test_rechaza_tamano_excesivo(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(self.campo, {"nombre": "doc.pdf", "tamano_mb": 10})

    def test_acepta_archivo_valido(self):
        self.estrategia.validar_valor(self.campo, {"nombre": "doc.pdf", "tamano_mb": 2})

    def test_configuracion_rechaza_extensiones_no_lista(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_configuracion({"extensiones_permitidas": "pdf"})


class EstrategiaReferenciaOrganizacionalTests(TestCase):
    """USUARIO/AREA/UNIDAD reutilizan los modelos reales de `apps.core`."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="gsilva", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="F")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.area = Area.objects.create(nombre="Compras", codigo="COMP-1.2")
        self.area_inactiva = Area.objects.create(nombre="Obsoleta", codigo="OBS-1.2", activo=False)
        self.campo = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.AREA, etiqueta="Área")
        self.estrategia = ESTRATEGIAS_POR_TIPO[Campo.TipoCampo.AREA]

    def test_acepta_area_activa(self):
        self.estrategia.validar_valor(self.campo, self.area.pk)

    def test_rechaza_area_inactiva_por_defecto(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(self.campo, self.area_inactiva.pk)

    def test_permite_incluir_inactivos_si_se_configura(self):
        self.campo.configuracion = {"solo_activos": False}
        self.campo.save()
        self.estrategia.validar_valor(self.campo, self.area_inactiva.pk)

    def test_rechaza_referencia_inexistente(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(self.campo, 999999)


class OpcionCampoTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="pcarrillo", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="F")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.campo = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.LISTA, etiqueta="Prioridad")

    def test_no_permite_valores_duplicados_en_el_mismo_campo(self):
        OpcionCampo.objects.create(campo=self.campo, valor="ALTA", etiqueta="Alta")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OpcionCampo.objects.create(campo=self.campo, valor="ALTA", etiqueta="Alta (otra etiqueta)")


class EspecificacionReglaTests(TestCase):
    """RQF-043 — `EspecificacionRegla.es_satisfecha_por`."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="hrios", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="F")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.origen = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.NUMERO, etiqueta="Monto")
        self.objetivo = Campo.objects.create(
            version=self.version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Justificación"
        )

    def _regla(self, operador, valor, efecto=ReglaCondicional.Efecto.MOSTRAR):
        return ReglaCondicional.objects.create(
            campo_origen=self.origen,
            operador=operador,
            valor=valor,
            campo_objetivo=self.objetivo,
            efecto=efecto,
        )

    def test_igual_a_satisfecha(self):
        regla = self._regla(ReglaCondicional.Operador.IGUAL_A, "100")
        self.assertTrue(EspecificacionRegla(regla).es_satisfecha_por({self.origen.pk: 100}))

    def test_igual_a_no_satisfecha(self):
        regla = self._regla(ReglaCondicional.Operador.IGUAL_A, "100")
        self.assertFalse(EspecificacionRegla(regla).es_satisfecha_por({self.origen.pk: 50}))

    def test_mayor_que_satisfecha(self):
        regla = self._regla(ReglaCondicional.Operador.MAYOR_QUE, "50")
        self.assertTrue(EspecificacionRegla(regla).es_satisfecha_por({self.origen.pk: 100}))

    def test_mayor_que_sin_respuesta_no_satisfecha(self):
        # Sin recomputación iterativa: comparar None > 50 no satisface, no lanza error.
        regla = self._regla(ReglaCondicional.Operador.MAYOR_QUE, "50")
        self.assertFalse(EspecificacionRegla(regla).es_satisfecha_por({}))

    def test_esta_vacio_satisfecha_sin_respuesta(self):
        regla = self._regla(ReglaCondicional.Operador.ESTA_VACIO, "")
        self.assertTrue(EspecificacionRegla(regla).es_satisfecha_por({}))

    def test_no_esta_vacio_satisfecha_con_respuesta(self):
        regla = self._regla(ReglaCondicional.Operador.NO_ESTA_VACIO, "")
        self.assertTrue(EspecificacionRegla(regla).es_satisfecha_por({self.origen.pk: 100}))

    def test_contiene_para_multilista(self):
        multilista = Campo.objects.create(
            version=self.version, tipo=Campo.TipoCampo.MULTILISTA, etiqueta="Etiquetas"
        )
        regla = ReglaCondicional.objects.create(
            campo_origen=multilista,
            operador=ReglaCondicional.Operador.CONTIENE,
            valor="URGENTE",
            campo_objetivo=self.objetivo,
            efecto=ReglaCondicional.Efecto.REQUERIR,
        )
        self.assertTrue(EspecificacionRegla(regla).es_satisfecha_por({multilista.pk: ["URGENTE", "INTERNO"]}))
        self.assertFalse(EspecificacionRegla(regla).es_satisfecha_por({multilista.pk: ["INTERNO"]}))


class ReglaCondicionalIntegridadTests(TestCase):
    """Compatibilidad operador/tipo, misma versión, sin autorreferencia;
    ciclos permitidos (ver `reglas.py::validar_integridad_regla`)."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="ivargas", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="F")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.otra_version = crear_nueva_version(Formulario.objects.create(nombre="Otro"), actor=self.usuario)
        self.origen = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Origen")
        self.objetivo = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Objetivo")
        self.campo_ajeno = Campo.objects.create(
            version=self.otra_version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Ajeno"
        )

    def test_rechaza_autorreferencia(self):
        regla = ReglaCondicional(
            campo_origen=self.origen,
            operador=ReglaCondicional.Operador.IGUAL_A,
            valor="x",
            campo_objetivo=self.origen,
            efecto=ReglaCondicional.Efecto.MOSTRAR,
        )
        with self.assertRaises(ValidationError):
            regla.clean()

    def test_rechaza_campos_de_distinta_version(self):
        regla = ReglaCondicional(
            campo_origen=self.origen,
            operador=ReglaCondicional.Operador.IGUAL_A,
            valor="x",
            campo_objetivo=self.campo_ajeno,
            efecto=ReglaCondicional.Efecto.MOSTRAR,
        )
        with self.assertRaises(ValidationError):
            regla.clean()

    def test_rechaza_operador_incompatible_con_tipo_origen(self):
        # MAYOR_QUE no es compatible con TEXTO.
        regla = ReglaCondicional(
            campo_origen=self.origen,
            operador=ReglaCondicional.Operador.MAYOR_QUE,
            valor="5",
            campo_objetivo=self.objetivo,
            efecto=ReglaCondicional.Efecto.MOSTRAR,
        )
        with self.assertRaises(ValidationError):
            regla.clean()

    def test_acepta_operador_compatible(self):
        regla = ReglaCondicional(
            campo_origen=self.origen,
            operador=ReglaCondicional.Operador.NO_ESTA_VACIO,
            valor="",
            campo_objetivo=self.objetivo,
            efecto=ReglaCondicional.Efecto.MOSTRAR,
        )
        regla.clean()

    def test_permite_dependencia_cruzada_entre_dos_campos(self):
        # Documenta la decisión aprobada: A->B y B->A no se prohíben.
        regla_ab = ReglaCondicional.objects.create(
            campo_origen=self.origen,
            operador=ReglaCondicional.Operador.NO_ESTA_VACIO,
            valor="",
            campo_objetivo=self.objetivo,
            efecto=ReglaCondicional.Efecto.MOSTRAR,
        )
        regla_ba = ReglaCondicional.objects.create(
            campo_origen=self.objetivo,
            operador=ReglaCondicional.Operador.NO_ESTA_VACIO,
            valor="",
            campo_objetivo=self.origen,
            efecto=ReglaCondicional.Efecto.MOSTRAR,
        )
        self.assertIsNotNone(regla_ab.pk)
        self.assertIsNotNone(regla_ba.pk)


class ReglaCondicionalComposicionTests(TestCase):
    """2.2 — decisión aprobada: impedir configuraciones contradictorias
    (MOSTRAR+OCULTAR, REQUERIR+NO_REQUERIR sobre el mismo campo_objetivo)
    en vez de resolverlas con una precedencia en tiempo de evaluación."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="jsalas", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="F")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        self.origen_1 = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Origen1")
        self.origen_2 = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Origen2")
        self.objetivo = Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Objetivo")

    def _crear(self, origen, efecto):
        return ReglaCondicional.objects.create(
            campo_origen=origen,
            operador=ReglaCondicional.Operador.NO_ESTA_VACIO,
            valor="",
            campo_objetivo=self.objetivo,
            efecto=efecto,
        )

    def test_rechaza_mostrar_y_ocultar_sobre_el_mismo_objetivo(self):
        self._crear(self.origen_1, ReglaCondicional.Efecto.MOSTRAR)
        with self.assertRaises(ValidationError):
            self._crear(self.origen_2, ReglaCondicional.Efecto.OCULTAR)

    def test_rechaza_ocultar_y_mostrar_en_orden_inverso(self):
        self._crear(self.origen_1, ReglaCondicional.Efecto.OCULTAR)
        with self.assertRaises(ValidationError):
            self._crear(self.origen_2, ReglaCondicional.Efecto.MOSTRAR)

    def test_rechaza_requerir_y_no_requerir_sobre_el_mismo_objetivo(self):
        self._crear(self.origen_1, ReglaCondicional.Efecto.REQUERIR)
        with self.assertRaises(ValidationError):
            self._crear(self.origen_2, ReglaCondicional.Efecto.NO_REQUERIR)

    def test_permite_multiples_reglas_del_mismo_efecto_sobre_el_mismo_objetivo(self):
        regla_1 = self._crear(self.origen_1, ReglaCondicional.Efecto.MOSTRAR)
        regla_2 = self._crear(self.origen_2, ReglaCondicional.Efecto.MOSTRAR)
        self.assertIsNotNone(regla_1.pk)
        self.assertIsNotNone(regla_2.pk)

    def test_mostrar_y_requerir_sobre_el_mismo_objetivo_son_independientes(self):
        # Familias distintas (visibilidad vs. obligatoriedad) — no conflictan entre sí.
        regla_mostrar = self._crear(self.origen_1, ReglaCondicional.Efecto.MOSTRAR)
        regla_requerir = self._crear(self.origen_2, ReglaCondicional.Efecto.REQUERIR)
        self.assertIsNotNone(regla_mostrar.pk)
        self.assertIsNotNone(regla_requerir.pk)


class PrevisualizarVersionTests(TestCase):
    """CU-013/RQF-045 — previsualización real, sin persistencia."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="jquintero", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.usuario, "formulario.administrar")
        self.sin_permiso = Usuario.objects.create_user(username="sin_permiso_form", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="Solicitud de compra")
        self.version = crear_nueva_version(self.formulario, actor=self.usuario)
        Campo.objects.create(version=self.version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Descripción")

    def test_exige_autenticacion(self):
        respuesta = self.client.get(reverse("formularios:previsualizar_version", args=[self.version.pk]))
        self.assertEqual(respuesta.status_code, 302)

    def test_exige_permiso_formulario_administrar(self):
        self.client.login(username="sin_permiso_form", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("formularios:previsualizar_version", args=[self.version.pk]))
        self.assertEqual(respuesta.status_code, 403)

    def test_renderiza_campos_sin_persistir_nada(self):
        self.client.login(username="jquintero", password=CLAVE_PRUEBA)
        total_auditoria_antes = RegistroAuditoria.objects.count()
        respuesta = self.client.get(reverse("formularios:previsualizar_version", args=[self.version.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Descripción")
        self.assertEqual(RegistroAuditoria.objects.count(), total_auditoria_antes)
        self.assertEqual(Campo.objects.filter(version=self.version).count(), 1)


class ServicioFormularioAsociacionTests(TestCase):
    """RQF-034 (CU-010) — cierre del pendiente de 1.1."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="kmedina", password=CLAVE_PRUEBA)
        self.categoria = Categoria.objects.create(nombre="Compras")
        self.formulario = Formulario.objects.create(nombre="Solicitud de compra")

    def test_servicio_sin_formulario_asociado_es_valido(self):
        servicio = Servicio.objects.create(nombre="Compra de insumos", categoria=self.categoria)
        self.assertIsNone(servicio.formulario)

    def test_asociar_formulario_a_servicio(self):
        servicio = Servicio.objects.create(nombre="Compra de insumos", categoria=self.categoria)
        servicio.formulario = self.formulario
        servicio.save()
        servicio.refresh_from_db()
        self.assertEqual(servicio.formulario_id, self.formulario.pk)

    def test_activar_nueva_version_no_requiere_cambios_en_servicio(self):
        servicio = Servicio.objects.create(
            nombre="Compra de insumos", categoria=self.categoria, formulario=self.formulario
        )
        version = crear_nueva_version(self.formulario, actor=self.usuario)
        activar_version(self.formulario, version, actor=self.usuario)
        servicio.refresh_from_db()
        self.assertEqual(servicio.formulario_id, self.formulario.pk)
        self.assertEqual(servicio.formulario.version_activa_id, version.pk)


class AutorizacionFormularioTests(TestCase):
    """`formulario.administrar` separado de `catalogo.administrar`."""

    def setUp(self):
        self.con_permiso = Usuario.objects.create_user(username="lsalas", password=CLAVE_PRUEBA, is_staff=True)
        _otorgar_permiso(self.con_permiso, "formulario.administrar")
        self.solo_catalogo = Usuario.objects.create_user(
            username="msolo_cat", password=CLAVE_PRUEBA, is_staff=True
        )
        _otorgar_permiso(self.solo_catalogo, "catalogo.administrar")
        self.sin_permiso = Usuario.objects.create_user(username="nsin_permiso", password=CLAVE_PRUEBA, is_staff=True)

    def test_con_permiso_formulario_administrar_puede_administrar(self):
        admin_instance = FormularioAdmin(Formulario, django_admin.site)
        request = RequestFactory().get("/admin/catalogo/formulario/")
        request.user = self.con_permiso
        self.assertTrue(admin_instance.has_view_permission(request))
        self.assertTrue(admin_instance.has_add_permission(request))

    def test_permiso_catalogo_administrar_no_otorga_formulario_administrar(self):
        admin_instance = FormularioAdmin(Formulario, django_admin.site)
        request = RequestFactory().get("/admin/catalogo/formulario/")
        request.user = self.solo_catalogo
        self.assertFalse(admin_instance.has_view_permission(request))

    def test_sin_permiso_no_puede_administrar_formularios(self):
        admin_instance = FormularioAdmin(Formulario, django_admin.site)
        request = RequestFactory().get("/admin/catalogo/formulario/")
        request.user = self.sin_permiso
        self.assertFalse(admin_instance.has_view_permission(request))

    def test_version_admin_no_permite_alta_directa(self):
        admin_instance = FormularioVersionAdmin(FormularioVersion, django_admin.site)
        request = RequestFactory().get("/admin/catalogo/formularioversion/")
        request.user = self.con_permiso
        self.assertFalse(admin_instance.has_add_permission(request))

    def test_campo_admin_bloquea_edicion_de_version_no_borrador(self):
        formulario = Formulario.objects.create(nombre="F")
        version = crear_nueva_version(formulario, actor=self.con_permiso)
        campo = Campo.objects.create(version=version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Campo")
        activar_version(formulario, version, actor=self.con_permiso)
        campo.refresh_from_db()

        admin_instance = CampoAdmin(Campo, django_admin.site)
        request = RequestFactory().get("/admin/catalogo/campo/")
        request.user = self.con_permiso
        self.assertFalse(admin_instance.has_change_permission(request, campo))


class AuditoriaVersionamientoTests(TestCase):
    """`crear_nueva_version`/`activar_version` reutilizan la infraestructura
    de auditoría de 0.4 — ningún historial paralelo."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="opardo", password=CLAVE_PRUEBA)
        self.formulario = Formulario.objects.create(nombre="Solicitud de vacaciones")

    def test_crear_nueva_version_registra_evento_crear(self):
        version = crear_nueva_version(self.formulario, actor=self.usuario)
        evento = RegistroAuditoria.objects.get(modelo="catalogo.formularioversion", object_id=version.pk)
        self.assertEqual(evento.accion, RegistroAuditoria.Accion.CREAR)
        self.assertEqual(evento.usuario, self.usuario)

    def test_activar_version_registra_evento_actualizar_sobre_formulario(self):
        version = crear_nueva_version(self.formulario, actor=self.usuario)
        activar_version(self.formulario, version, actor=self.usuario)
        evento = RegistroAuditoria.objects.filter(
            modelo="catalogo.formulario",
            object_id=self.formulario.pk,
            accion=RegistroAuditoria.Accion.ACTUALIZAR,
        ).latest("creado_en")
        self.assertEqual(evento.datos_nuevos["version_activa_id"], version.pk)
