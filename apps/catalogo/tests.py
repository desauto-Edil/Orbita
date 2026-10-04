"""Pruebas de `apps.catalogo` — incrementos 1.1 (Catálogo) y 1.2 (Form Builder).

Único archivo de pruebas de esta app (misma decisión que `apps.core`: sin
paquete `tests/`). No se ejecutan como parte de la implementación — se
entregan junto con los comandos exactos para correrlas vía Docker.
"""

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse

from apps.catalogo.admin import CampoAdmin, FormularioAdmin, FormularioVersionAdmin, ServicioAdmin
from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import (
    BloqueOperativo,
    Campo,
    Categoria,
    ConfiguracionEjecucionVersion,
    DefinicionEntregable,
    Formulario,
    FormularioVersion,
    OpcionCampo,
    ReglaCondicional,
    Servicio,
    ServicioContextoAtencion,
    ServicioResponsable,
    ServicioVisibilidad,
    TransicionBloqueOperativo,
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


class ServicioContextoAtencionTests(TestCase):
    """RQF-061, RQF-028, RN-009 — integridad de `ServicioContextoAtencion`
    (2.3, alternativa B aprobada). Mismo patrón de constraints que
    `ServicioVisibilidad`/`ServicioResponsable`: CheckConstraint de
    coherencia + UniqueConstraint parcial independiente por rama."""

    def setUp(self):
        categoria = Categoria.objects.create(nombre="Tecnología")
        self.servicio = Servicio.objects.create(nombre="Soporte técnico", categoria=categoria)
        self.area = Area.objects.create(nombre="TIC", codigo="TIC-CTX")
        self.otra_area = Area.objects.create(nombre="Financiera", codigo="FIN-CTX")
        self.unidad = UnidadNegocio.objects.create(nombre="Infraestructura", codigo="INFRA-CTX")

    def test_constraint_area_exige_area_y_rechaza_unidad(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioContextoAtencion.objects.create(
                    servicio=self.servicio,
                    tipo_alcance=ServicioContextoAtencion.TipoAlcance.AREA,
                    unidad_negocio=self.unidad,
                )

    def test_constraint_unidad_exige_unidad_y_rechaza_area(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioContextoAtencion.objects.create(
                    servicio=self.servicio,
                    tipo_alcance=ServicioContextoAtencion.TipoAlcance.UNIDAD,
                    area=self.area,
                )

    def test_no_permite_duplicar_contexto_area_activo(self):
        ServicioContextoAtencion.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioContextoAtencion.TipoAlcance.AREA, area=self.area
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioContextoAtencion.objects.create(
                    servicio=self.servicio,
                    tipo_alcance=ServicioContextoAtencion.TipoAlcance.AREA,
                    area=self.area,
                )

    def test_no_permite_duplicar_contexto_unidad_activo(self):
        ServicioContextoAtencion.objects.create(
            servicio=self.servicio,
            tipo_alcance=ServicioContextoAtencion.TipoAlcance.UNIDAD,
            unidad_negocio=self.unidad,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ServicioContextoAtencion.objects.create(
                    servicio=self.servicio,
                    tipo_alcance=ServicioContextoAtencion.TipoAlcance.UNIDAD,
                    unidad_negocio=self.unidad,
                )

    def test_servicio_transversal_admite_varios_contextos_simultaneos(self):
        # Un servicio transversal conserva varias AREA (y/o UNIDAD) a la
        # vez — no hay cardinalidad 1 impuesta (RN-003/004/005).
        ServicioContextoAtencion.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioContextoAtencion.TipoAlcance.AREA, area=self.area
        )
        ServicioContextoAtencion.objects.create(
            servicio=self.servicio,
            tipo_alcance=ServicioContextoAtencion.TipoAlcance.AREA,
            area=self.otra_area,
        )
        ServicioContextoAtencion.objects.create(
            servicio=self.servicio,
            tipo_alcance=ServicioContextoAtencion.TipoAlcance.UNIDAD,
            unidad_negocio=self.unidad,
        )
        self.assertEqual(self.servicio.contextos_atencion.filter(activo=True).count(), 3)


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
            "tipo": Servicio.Tipo.SERVICIO,
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
            # 2.3: `ServicioContextoAtencionInline` — sin su management form
            # el POST de Admin re-renderiza con errores (200) en vez de
            # redirigir (302), aunque el inline se deje vacío.
            "contextos_atencion-TOTAL_FORMS": "0",
            "contextos_atencion-INITIAL_FORMS": "0",
            "contextos_atencion-MIN_NUM_FORMS": "0",
            "contextos_atencion-MAX_NUM_FORMS": "1000",
        }
        respuesta = self.client.post(reverse("admin:catalogo_servicio_add"), datos)
        self.assertEqual(respuesta.status_code, 302)
        servicio = Servicio.objects.get(nombre="Nuevo servicio")
        # 4.1: el POST no puede publicar con el antiguo checkbox activo.
        self.assertFalse(servicio.activo)
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


class PublicacionCatalogoTests(TestCase):
    """4.1: publicación explícita, sin reinterpretar visibilidad histórica."""

    def setUp(self):
        self.actor = Usuario.objects.create_user("publicador41")
        self.otro = Usuario.objects.create_user("solicitante41")
        _otorgar_permiso(self.actor, "catalogo.administrar")
        self.categoria = Categoria.objects.create(nombre="Mercadeo")
        self.formulario = Formulario.objects.create(nombre="Entrada")
        version = crear_nueva_version(self.formulario, self.actor)
        activar_version(self.formulario, version, self.actor)
        self.formulario.refresh_from_db()

    def _crear(self, **datos):
        return Servicio.objects.create(
            nombre="Definición", categoria=self.categoria, activo=False,
            alcance_visibilidad="PUBLICO_INTERNO", **datos,
        )

    def test_servicio_por_defecto_conserva_compatibilidad(self):
        servicio = Servicio.objects.create(nombre="Histórico", categoria=self.categoria, alcance_visibilidad="PUBLICO_INTERNO")
        self.assertEqual(servicio.tipo, "SERVICIO")
        self.assertTrue(servicio.activo)
        self.assertIsNone(servicio.workflow_id)
        self.assertIn(servicio, servicios_visibles_para(self.otro))

    def test_activar_servicio_valido_audita_una_vez(self):
        from apps.catalogo.operaciones import activar_servicio, desactivar_servicio

        servicio = self._crear(formulario=self.formulario)
        self.assertNotIn(servicio, servicios_visibles_para(self.otro))
        eventos = RegistroAuditoria.objects.count()
        servicio = activar_servicio(servicio, self.actor)
        self.assertIn(servicio, servicios_visibles_para(self.otro))
        self.assertEqual(RegistroAuditoria.objects.count(), eventos + 1)
        evento = RegistroAuditoria.objects.filter(modelo="catalogo.servicio", object_id=servicio.pk).get()
        self.assertEqual(evento.usuario, self.actor)
        self.assertEqual(evento.accion, "ACTUALIZAR")
        self.assertEqual(evento.datos_anteriores, {"activo": False})
        self.assertTrue(evento.datos_nuevos["activo"])
        activar_servicio(servicio, self.actor)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos + 1)
        desactivar_servicio(servicio, self.actor)
        self.assertNotIn(servicio, servicios_visibles_para(self.otro))
        self.assertEqual(RegistroAuditoria.objects.count(), eventos + 2)

    def test_no_activa_sin_formulario_o_version_activa(self):
        from apps.catalogo.operaciones import activar_servicio

        vacio = Formulario.objects.create(nombre="Sin activar")
        for formulario in (None, vacio):
            with self.subTest(formulario=formulario):
                servicio = self._crear(formulario=formulario)
                eventos = RegistroAuditoria.objects.count()
                with self.assertRaises(ValidationError):
                    activar_servicio(servicio, self.actor)
                servicio.refresh_from_db()
                self.assertFalse(servicio.activo)
                self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_proceso_exige_workflow_con_version_activa(self):
        from apps.catalogo.operaciones import activar_servicio
        from apps.workflows.models import Workflow, WorkflowVersion

        workflow = Workflow.objects.create(nombre="Proceso")
        servicio = self._crear(tipo="PROCESO", formulario=self.formulario)
        with self.assertRaises(ValidationError):
            activar_servicio(servicio, self.actor)
        servicio.workflow = workflow
        servicio.save()
        with self.assertRaises(ValidationError):
            activar_servicio(servicio, self.actor)
        # La publicación 4.3 también exige una estructura utilizable.
        from apps.workflows.models import Etapa, TransicionEtapa
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo="INICIO", nombre="Inicio")
        fin = Etapa.objects.create(version=version, tipo="FIN", nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=fin)
        workflow.version_activa = version
        workflow.save()
        with self.assertRaises(ValidationError):
            activar_servicio(servicio, self.actor)
        version.estado = "ACTIVA"
        version.save()
        servicio = activar_servicio(servicio, self.actor)
        self.assertTrue(servicio.activo)
        self.assertEqual(servicio.tipo, "PROCESO")
        self.assertEqual(servicio.formulario_id, self.formulario.pk)
        self.assertEqual(servicio.workflow_id, workflow.pk)
        self.assertIn(servicio, servicios_visibles_para(self.otro))
        self.assertEqual(RegistroAuditoria.objects.filter(modelo="catalogo.servicio", object_id=servicio.pk).count(), 1)

    def test_autorizacion_sin_bypass_de_superusuario(self):
        from django.core.exceptions import PermissionDenied
        from apps.catalogo.operaciones import activar_servicio, desactivar_servicio

        servicio = self._crear(formulario=self.formulario)
        self.otro.is_superuser = True
        self.otro.save()
        eventos = RegistroAuditoria.objects.count()
        for operacion in (activar_servicio, desactivar_servicio):
            with self.assertRaises(PermissionDenied):
                operacion(servicio, self.otro)
        servicio.refresh_from_db()
        self.assertFalse(servicio.activo)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_fallo_auditoria_revierte_activacion(self):
        from unittest.mock import patch
        from apps.catalogo.operaciones import activar_servicio

        servicio = self._crear(formulario=self.formulario)
        with patch("apps.catalogo.operaciones.registrar_evento", side_effect=RuntimeError("Fallo")):
            with self.assertRaises(RuntimeError):
                activar_servicio(servicio, self.actor)
        servicio.refresh_from_db()
        self.assertFalse(servicio.activo)

    def test_diagnostico_reporta_historicos_sin_modificarlos(self):
        import json
        from io import StringIO
        from django.core.management import call_command

        antiguo = Servicio.objects.create(nombre="Incompleto", categoria=self.categoria)
        valido = Servicio.objects.create(nombre="Completo", categoria=self.categoria, formulario=self.formulario)
        self._crear()  # Inactivo incompleto: no es un hallazgo.
        antes = list(Servicio.objects.order_by("pk").values())
        eventos = RegistroAuditoria.objects.count()
        salida = StringIO()
        call_command("auditar_catalogo", stdout=salida)
        reporte = json.loads(salida.getvalue())
        self.assertEqual(reporte["cantidad"], 1)
        self.assertEqual(reporte["hallazgos"][0]["id"], antiguo.pk)
        self.assertTrue(reporte["hallazgos"][0]["errores"])
        self.assertNotEqual(antiguo.pk, valido.pk)
        self.assertEqual(list(Servicio.objects.order_by("pk").values()), antes)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_proceso_reutiliza_visibilidad_y_responsables(self):
        from apps.tickets.autorizacion import usuario_es_responsable_configurado

        proceso = self._crear(tipo="PROCESO", formulario=self.formulario)
        proceso.activo = True  # Configuración heredada; no modifica la regla de visibilidad.
        proceso.alcance_visibilidad = "RESTRINGIDO"
        proceso.save()
        self.assertNotIn(proceso, servicios_visibles_para(self.otro))
        ServicioVisibilidad.objects.create(servicio=proceso, tipo_alcance="USUARIO", usuario=self.otro)
        self.assertIn(proceso, servicios_visibles_para(self.otro))
        self.assertFalse(usuario_es_responsable_configurado(self.otro, proceso))
        ServicioResponsable.objects.create(servicio=proceso, tipo_responsable="USUARIO", usuario=self.otro)
        self.assertTrue(usuario_es_responsable_configurado(self.otro, proceso))

    def test_admin_publica_con_validacion_y_conserva_activo_historico_al_editar(self):
        self.actor.is_staff = True
        self.actor.save()
        self.client.force_login(self.actor)
        servicio = self._crear()
        eventos = RegistroAuditoria.objects.count()
        respuesta = self.client.post(reverse("admin:catalogo_servicio_changelist"), {
            "action": "activar_action", "_selected_action": [servicio.pk],
        })
        self.assertEqual(respuesta.status_code, 302)
        servicio.refresh_from_db()
        self.assertFalse(servicio.activo)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)
        servicio.formulario = self.formulario
        servicio.save()
        respuesta = self.client.post(reverse("admin:catalogo_servicio_changelist"), {
            "action": "activar_action", "_selected_action": [servicio.pk],
        })
        self.assertEqual(respuesta.status_code, 302)
        servicio.refresh_from_db()
        self.assertTrue(servicio.activo)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos + 1)
        # Histórico incompleto activo: editar su nombre no lo desactiva.
        servicio.formulario = None
        servicio.save()
        datos = {"nombre": "Nombre editado", "tipo": "SERVICIO", "categoria": self.categoria.pk,
                 "alcance_visibilidad": "PUBLICO_INTERNO", "activo": ""}
        for prefijo in ("visibilidad", "responsables", "contextos_atencion"):
            datos[f"{prefijo}-TOTAL_FORMS"] = "0"
            datos[f"{prefijo}-INITIAL_FORMS"] = "0"
        respuesta = self.client.post(reverse("admin:catalogo_servicio_change", args=[servicio.pk]), datos)
        self.assertEqual(respuesta.status_code, 302)
        servicio.refresh_from_db()
        self.assertTrue(servicio.activo)
        self.assertEqual(servicio.nombre, "Nombre editado")
        self.assertIsNone(servicio.formulario_id)

    def test_version_formulario_no_activa_no_publica(self):
        from apps.catalogo.operaciones import activar_servicio

        servicio = self._crear(formulario=self.formulario)
        version = self.formulario.version_activa
        version.estado = FormularioVersion.Estado.HISTORICA
        version.save()
        eventos = RegistroAuditoria.objects.count()
        with self.assertRaises(ValidationError):
            activar_servicio(servicio, self.actor)
        servicio.refresh_from_db()
        self.assertFalse(servicio.activo)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)


class DefinicionEntregableTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user("def_entregables")
        self.ajeno = Usuario.objects.create_user("def_ajeno")
        _otorgar_permiso(self.actor, "catalogo.administrar")
        self.categoria = Categoria.objects.create(nombre="Salidas")
        self.servicio = Servicio.objects.create(nombre="Servicio", categoria=self.categoria)

    def test_cero_a_varios_tipos_orden_y_obligatoriedad_para_ambas_clases(self):
        from apps.catalogo.entregables import configurar_definicion_entregable
        from apps.catalogo.models import DefinicionEntregable

        for tipo_catalogo in Servicio.Tipo.values:
            servicio = Servicio.objects.create(nombre=tipo_catalogo, tipo=tipo_catalogo, categoria=self.categoria)
            self.assertEqual(servicio.definiciones_entregables.count(), 0)
            for orden, tipo in enumerate(DefinicionEntregable.Tipo.values):
                configurar_definicion_entregable(
                    servicio, self.actor, nombre=tipo, tipo=tipo, descripcion="Instrucciones",
                    obligatorio=orden % 2 == 0, orden=orden,
                )
            self.assertEqual(list(servicio.definiciones_entregables.values_list("tipo", flat=True)), list(DefinicionEntregable.Tipo.values))
            self.assertEqual(list(servicio.definiciones_entregables.values_list("obligatorio", flat=True)), [True, False, True, False])

    def test_creacion_edicion_retiro_auditados_una_vez(self):
        from apps.catalogo.entregables import configurar_definicion_entregable, retirar_definicion_entregable

        eventos = RegistroAuditoria.objects.count()
        definicion = configurar_definicion_entregable(self.servicio, self.actor, nombre="Original", tipo="TEXTO")
        configurar_definicion_entregable(self.servicio, self.actor, definicion=definicion, nombre="Final", tipo="ARCHIVO", obligatorio=True)
        retirar_definicion_entregable(definicion, self.actor)
        retirar_definicion_entregable(definicion, self.actor)  # Sin evento redundante.
        self.assertEqual(RegistroAuditoria.objects.count(), eventos + 3)
        registros = RegistroAuditoria.objects.filter(modelo="catalogo.definicionentregable", object_id=definicion.pk).order_by("pk")
        self.assertEqual(list(registros.values_list("accion", flat=True)), ["CREAR", "ACTUALIZAR", "ACTUALIZAR"])
        self.assertEqual(registros[1].datos_anteriores["nombre"], "Original")
        self.assertEqual(registros[1].datos_nuevos["tipo"], "ARCHIVO")
        self.assertEqual(registros[2].usuario, self.actor)
        definicion.refresh_from_db()
        self.assertFalse(definicion.activo)

    def test_no_autorizado_no_configura_ni_retira(self):
        from django.core.exceptions import PermissionDenied
        from apps.catalogo.entregables import configurar_definicion_entregable, retirar_definicion_entregable

        definicion = configurar_definicion_entregable(self.servicio, self.actor, nombre="Final", tipo="TEXTO")
        eventos = RegistroAuditoria.objects.count()
        with self.assertRaises(PermissionDenied):
            configurar_definicion_entregable(self.servicio, self.ajeno, nombre="Intruso", tipo="TEXTO")
        with self.assertRaises(PermissionDenied):
            configurar_definicion_entregable(self.servicio, self.ajeno, definicion=definicion, nombre="Intruso", tipo="TEXTO")
        with self.assertRaises(PermissionDenied):
            retirar_definicion_entregable(definicion, self.ajeno)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)
        definicion.refresh_from_db()
        self.assertEqual(definicion.nombre, "Final")
        self.assertTrue(definicion.activo)

    def test_rechaza_nombre_vacio_tipo_desconocido_y_orden_negativo(self):
        from apps.catalogo.entregables import configurar_definicion_entregable

        for datos in ({"nombre": " ", "tipo": "TEXTO"}, {"nombre": "X", "tipo": "IMAGEN"}, {"nombre": "X", "tipo": "ARCHIVO", "orden": -1}):
            with self.subTest(datos=datos), self.assertRaises(ValidationError):
                configurar_definicion_entregable(self.servicio, self.actor, **datos)
        self.assertFalse(self.servicio.definiciones_entregables.exists())
        self.assertFalse(RegistroAuditoria.objects.filter(modelo="catalogo.definicionentregable").exists())

    def test_fallo_auditoria_revierte_configuracion(self):
        from unittest.mock import patch
        from apps.catalogo.entregables import configurar_definicion_entregable

        with patch("apps.catalogo.entregables.registrar_evento", side_effect=RuntimeError("auditoría")):
            with self.assertRaises(RuntimeError):
                configurar_definicion_entregable(self.servicio, self.actor, nombre="Final", tipo="TEXTO")
        self.assertFalse(self.servicio.definiciones_entregables.exists())


class ConfiguracionEjecucionFasesR1Tests(TestCase):
    def setUp(self):
        from apps.workflows import fases as fases_ops
        from apps.workflows.versionamiento import activar_version

        self.actor = Usuario.objects.create_user(username="conf_fases", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.actor, "catalogo.administrar", nombre_rol="Catalogo configura fases")
        self.categoria = Categoria.objects.create(nombre="Diseno")
        self.servicio = Servicio.objects.create(nombre="Pieza grafica", categoria=self.categoria)
        self.workflow = fases_ops.crear_plantilla_fases(self.actor, nombre="Gestion estandar")
        self.workflow_version = self.workflow.versiones.get(numero=1)
        self.recepcion = fases_ops.agregar_fase(self.workflow_version, self.actor, nombre="Recepcion")
        self.ejecucion = fases_ops.agregar_fase(self.workflow_version, self.actor, nombre="Ejecucion")
        fases_ops.conectar_fases(self.recepcion, self.ejecucion, self.actor)
        activar_version(self.workflow, self.workflow_version, actor=self.actor)
        self.workflow.refresh_from_db()
        self.workflow_version.refresh_from_db()
        self.servicio.workflow = self.workflow
        self.servicio.save(update_fields=["workflow", "actualizado_en"])

    def test_crea_configuracion_operativa_versionada_y_bloque_en_fase(self):
        from apps.catalogo.configuracion_ejecucion import (
            activar_configuracion_ejecucion,
            agregar_bloque_operativo,
            crear_nueva_version_configuracion,
        )

        version = crear_nueva_version_configuracion(self.servicio, self.actor)
        bloque = agregar_bloque_operativo(
            version,
            self.actor,
            fase=self.recepcion,
            tipo=BloqueOperativo.Tipo.ACTIVIDAD,
            nombre="Revisar brief",
            configuracion={"tipo_actor": "SOLICITANTE"},
        )
        agregar_bloque_operativo(
            version,
            self.actor,
            fase=self.ejecucion,
            tipo=BloqueOperativo.Tipo.ACTIVIDAD,
            nombre="Ejecutar solicitud",
            configuracion={"tipo_actor": "SOLICITANTE"},
        )
        activar_configuracion_ejecucion(self.servicio, version, self.actor)
        self.servicio.refresh_from_db()

        self.assertEqual(version.estado, ConfiguracionEjecucionVersion.Estado.ACTIVA)
        self.assertEqual(self.servicio.configuracion_ejecucion_activa_id, version.pk)
        self.assertEqual(bloque.version_id, version.pk)
        self.assertEqual(bloque.fase_id, self.recepcion.pk)

    def test_rechaza_bloque_con_fase_de_otra_workflow_version(self):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, crear_nueva_version_configuracion
        from apps.workflows import fases as fases_ops

        otro_workflow = fases_ops.crear_plantilla_fases(self.actor, nombre="Otro flujo")
        otra_version = otro_workflow.versiones.get(numero=1)
        fase_ajena = fases_ops.agregar_fase(otra_version, self.actor, nombre="Ajena")
        version = crear_nueva_version_configuracion(self.servicio, self.actor)

        with self.assertRaises(ValidationError):
            agregar_bloque_operativo(
                version,
                self.actor,
                fase=fase_ajena,
                tipo=BloqueOperativo.Tipo.ACTIVIDAD,
                nombre="No corresponde",
            )

    def test_rechaza_editar_bloque_desde_otra_configuracion(self):
        from apps.catalogo.configuracion_ejecucion import (
            agregar_bloque_operativo,
            crear_nueva_version_configuracion,
            editar_bloque_operativo,
        )

        version1 = crear_nueva_version_configuracion(self.servicio, self.actor)
        version2 = crear_nueva_version_configuracion(self.servicio, self.actor)
        bloque = agregar_bloque_operativo(
            version1,
            self.actor,
            fase=self.recepcion,
            tipo=BloqueOperativo.Tipo.ACTIVIDAD,
            nombre="Revisar solicitud",
        )

        with self.assertRaises(ValidationError):
            editar_bloque_operativo(version2, bloque, self.actor, nombre="Mezcla invalida")

    def test_clona_configuracion_operativa_a_nueva_version(self):
        from apps.catalogo.configuracion_ejecucion import (
            agregar_bloque_operativo,
            crear_nueva_version_configuracion,
        )

        version1 = crear_nueva_version_configuracion(self.servicio, self.actor)
        agregar_bloque_operativo(
            version1,
            self.actor,
            fase=self.recepcion,
            tipo=BloqueOperativo.Tipo.ACTIVIDAD,
            nombre="Revisar brief",
        )

        version2 = crear_nueva_version_configuracion(self.servicio, self.actor, clonar_desde=version1)

        self.assertEqual(version2.numero, 2)
        self.assertEqual(version2.bloques.count(), 1)
        self.assertEqual(version2.bloques.get().fase_id, self.recepcion.pk)

    def test_borrador_valido_no_cuenta_como_configuracion_activa(self):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, crear_nueva_version_configuracion
        from apps.catalogo.operaciones import validar_publicacion

        version = crear_nueva_version_configuracion(self.servicio, self.actor)
        agregar_bloque_operativo(
            version,
            self.actor,
            fase=self.recepcion,
            tipo=BloqueOperativo.Tipo.ACTIVIDAD,
            nombre="Revisar brief",
            configuracion={"tipo_actor": "SOLICITANTE"},
        )

        with self.assertRaises(ValidationError):
            validar_publicacion(self.servicio)
        self.servicio.refresh_from_db()
        self.assertIsNone(self.servicio.configuracion_ejecucion_activa_id)

    def test_activacion_historiza_configuracion_anterior(self):
        from apps.catalogo.configuracion_ejecucion import (
            activar_configuracion_ejecucion,
            agregar_bloque_operativo,
            crear_nueva_version_configuracion,
            preparar_configuracion_ejecucion,
        )

        version1 = crear_nueva_version_configuracion(self.servicio, self.actor)
        agregar_bloque_operativo(
            version1,
            self.actor,
            fase=self.recepcion,
            tipo=BloqueOperativo.Tipo.ACTIVIDAD,
            nombre="Revisar brief",
            configuracion={"tipo_actor": "SOLICITANTE"},
        )
        activar_configuracion_ejecucion(self.servicio, version1, self.actor)

        version2 = preparar_configuracion_ejecucion(self.servicio, self.actor)
        bloque = version2.bloques.get(nombre="Revisar brief")
        bloque.nombre = "Revisar brief actualizado"
        bloque.save(update_fields=["nombre", "actualizado_en"])
        activar_configuracion_ejecucion(self.servicio, version2, self.actor)

        version1.refresh_from_db()
        version2.refresh_from_db()
        self.servicio.refresh_from_db()
        self.assertEqual(version1.estado, ConfiguracionEjecucionVersion.Estado.HISTORICA)
        self.assertEqual(version2.estado, ConfiguracionEjecucionVersion.Estado.ACTIVA)
        self.assertEqual(self.servicio.configuracion_ejecucion_activa_id, version2.pk)

    def test_rutas_operativas_de_aprobacion_y_decision_son_editables_en_modelo(self):
        from apps.catalogo.configuracion_ejecucion import (
            agregar_bloque_operativo,
            conectar_bloques_operativos,
            crear_nueva_version_configuracion,
        )

        version = crear_nueva_version_configuracion(self.servicio, self.actor)
        aprobacion = agregar_bloque_operativo(
            version,
            self.actor,
            fase=self.recepcion,
            tipo=BloqueOperativo.Tipo.APROBACION,
            nombre="Aprobar documento",
            configuracion={
                "modo": "SECUENCIAL",
                "participantes": [{"tipo": "SOLICITANTE", "usuario_id": None, "equipo_id": None}],
            },
        )
        decision = agregar_bloque_operativo(
            version,
            self.actor,
            fase=self.ejecucion,
            tipo=BloqueOperativo.Tipo.DECISION,
            nombre="Clasificar solicitud",
        )
        destino = agregar_bloque_operativo(
            version,
            self.actor,
            fase=self.ejecucion,
            tipo=BloqueOperativo.Tipo.ACTIVIDAD,
            nombre="Atender",
            configuracion={"tipo_actor": "SOLICITANTE"},
        )

        for resultado in ("APROBADA", "RECHAZADA", "DEVUELTA"):
            conectar_bloques_operativos(aprobacion, destino, self.actor, resultado_aprobacion=resultado)
        conectar_bloques_operativos(
            decision, destino, self.actor, variable="prioridad", operador="IGUAL_A", valor="Alta", prioridad=1
        )
        conectar_bloques_operativos(decision, destino, self.actor, es_fallback=True)

        self.assertEqual(TransicionBloqueOperativo.objects.filter(bloque_origen=aprobacion).count(), 3)
        self.assertEqual(TransicionBloqueOperativo.objects.filter(bloque_origen=decision).count(), 2)


class EjecucionConfigurableTests(TestCase):
    """4.3: operaciones empresariales sobre un único grafo versionado."""

    def setUp(self):
        from apps.catalogo import ejecucion

        self.ejecucion = ejecucion
        self.actor = Usuario.objects.create_user("configurador43")
        self.otro = Usuario.objects.create_user("ajeno43")
        _otorgar_permiso(self.actor, "catalogo.administrar")
        _otorgar_permiso(self.actor, "workflows.administrar")
        self.categoria = Categoria.objects.create(nombre="Servicios configurables")
        self.formulario = Formulario.objects.create(nombre="Entrada")
        entrada = crear_nueva_version(self.formulario, self.actor)
        activar_version(self.formulario, entrada, self.actor)
        self.servicio = Servicio.objects.create(
            nombre="Diseño", categoria=self.categoria, formulario=self.formulario, activo=False,
        )
        self.version = ejecucion.preparar_ejecucion(self.servicio, self.actor)
        self.inicio = self.version.etapas.get(tipo="INICIO")
        self.fin = self.version.etapas.get(tipo="FIN")
        self.conexion = self.inicio.transiciones_salientes.get()

    def _agregar(self, tipo="ACTIVIDAD", **configuracion):
        if tipo == "ACTIVIDAD" and not configuracion:
            configuracion = {"tipo_actor": "SOLICITANTE"}
        return self.ejecucion.agregar_bloque(
            self.servicio, self.version, self.actor, tipo=tipo,
            nombre="Bloque", descripcion="Instrucciones", **configuracion,
        )

    def _lineal(self, bloque):
        self.ejecucion.conectar_bloques(
            self.servicio, self.version, self.inicio, bloque, self.actor, conexion=self.conexion,
        )
        self.ejecucion.conectar_bloques(self.servicio, self.version, bloque, self.fin, self.actor)

    def test_preparar_idempotente_sin_publicacion_ni_segundo_grafo(self):
        from apps.workflows.models import Workflow, WorkflowVersion

        eventos = RegistroAuditoria.objects.count()
        repetida = self.ejecucion.preparar_ejecucion(self.servicio, self.actor)
        self.assertEqual(repetida.pk, self.version.pk)
        self.assertEqual(self.version.etapas.count(), 2)
        self.assertEqual(Workflow.objects.count(), 1)
        self.assertEqual(WorkflowVersion.objects.count(), 1)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)
        self.servicio.refresh_from_db()
        self.assertFalse(self.servicio.activo)
        self.assertEqual(self.servicio.workflow_id, self.version.workflow_id)

    def test_bloques_reutilizan_configuracion_y_auditoria_del_editor(self):
        from apps.workflows.models import Etapa

        casos = [
            ("ACTIVIDAD", {"tipo_actor": "SOLICITANTE"}, "TAREA"),
            ("APROBACION", {"modo": "SECUENCIAL", "participantes": [("RESPONSABLE_TICKET", None)]}, "APROBACION"),
            ("ESPERA", {"modo": "DURACION", "duracion_valor": 2, "duracion_unidad": "HORAS"}, "ESPERA"),
            ("DECISION", {}, "CONDICION"),
        ]
        for tipo, configuracion, tipo_motor in casos:
            with self.subTest(tipo=tipo):
                eventos = RegistroAuditoria.objects.count()
                bloque = self._agregar(tipo, **configuracion)
                self.assertEqual(bloque.tipo, tipo_motor)
                self.assertTrue(Etapa.objects.filter(pk=bloque.pk, version=self.version).exists())
                registros = RegistroAuditoria.objects.filter(modelo="workflows.etapa", object_id=bloque.pk)
                self.assertEqual(registros.filter(accion="CREAR").count(), 1)
                # Alta de metadatos y configuración: eventos distintos, sin evento exterior duplicado.
                self.assertEqual(RegistroAuditoria.objects.count() - eventos, 1 if tipo == "DECISION" else 2)
                if tipo == "ACTIVIDAD":
                    self.assertEqual(bloque.configuracion_tarea.tipo_responsable, "SOLICITANTE")
                if tipo == "APROBACION":
                    self.assertEqual(bloque.configuracion_aprobacion.participantes.get().tipo_aprobador, "RESPONSABLE_TICKET")

    def test_actor_invalido_no_deja_bloque_ni_eventos_parciales(self):
        for configuracion in (
            {"tipo_actor": "RESPONSABLE_EQUIPO"}, {"tipo_actor": "USUARIO"},
            {"tipo_actor": "SOLICITANTE", "usuario": self.actor}, {"tipo_actor": ""},
        ):
            eventos = RegistroAuditoria.objects.count()
            with self.subTest(configuracion=configuracion), self.assertRaises(ValidationError):
                self._agregar(**configuracion)
            self.assertEqual(self.version.etapas.count(), 2)
            self.assertEqual(RegistroAuditoria.objects.count(), eventos)
        with self.assertRaises(ValidationError):
            self._agregar("APROBACION", modo="SECUENCIAL", participantes=[("SOLICITANTE", self.actor)])
        self.assertEqual(self.version.etapas.count(), 2)

    def test_exige_ambos_permisos_sin_bypass_y_pertenencia(self):
        from django.core.exceptions import PermissionDenied
        from apps.workflows.models import Workflow, WorkflowVersion

        self.otro.is_superuser = True
        self.otro.save()
        eventos = RegistroAuditoria.objects.count()
        with self.assertRaises(PermissionDenied):
            self.ejecucion.preparar_ejecucion(self.servicio, self.otro)
        _otorgar_permiso(self.otro, "catalogo.administrar", nombre_rol="Solo catálogo43")
        with self.assertRaises(PermissionDenied):
            self.ejecucion.preparar_ejecucion(self.servicio, self.otro)
        ajena = WorkflowVersion.objects.create(workflow=Workflow.objects.create(nombre="Otro"), numero=1)
        with self.assertRaises(ValidationError):
            self.ejecucion.agregar_bloque(self.servicio, ajena, self.actor, tipo="DECISION", nombre="Ajeno")
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_editar_configurar_conectar_desconectar_y_eliminar(self):
        bloque = self._agregar()
        self.ejecucion.editar_bloque(self.servicio, self.version, bloque, self.actor, nombre="Preparar", descripcion="Nuevo")
        self.ejecucion.configurar_bloque(self.servicio, self.version, bloque, self.actor, tipo_actor="USUARIO", usuario=self.actor)
        self._lineal(bloque)
        bloque.refresh_from_db()
        self.assertEqual(bloque.nombre, "Preparar")
        self.assertEqual(bloque.descripcion, "Nuevo")
        self.assertEqual(bloque.configuracion_tarea.usuario_responsable, self.actor)
        with self.assertRaises(ValidationError):
            self.ejecucion.eliminar_bloque(self.servicio, self.version, bloque, self.actor)
        for conexion in list(bloque.transiciones_entrantes.all()) + list(bloque.transiciones_salientes.all()):
            self.ejecucion.desconectar_bloques(self.servicio, self.version, conexion, self.actor)
        self.ejecucion.eliminar_bloque(self.servicio, self.version, bloque, self.actor)
        self.assertEqual(self.version.etapas.count(), 2)
        with self.assertRaises(ValidationError):
            self.ejecucion.eliminar_bloque(self.servicio, self.version, self.inicio, self.actor)

    def test_publicar_rechaza_estructura_incompleta_y_revierte_si_falta_formulario(self):
        bloque = self._agregar()
        eventos = RegistroAuditoria.objects.count()
        with self.assertRaises(ValidationError):
            self.ejecucion.publicar_ejecucion(self.servicio, self.version, self.actor)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)
        self._lineal(bloque)
        self.servicio.formulario = None
        self.servicio.save()
        eventos = RegistroAuditoria.objects.count()
        with self.assertRaises(ValidationError):
            self.ejecucion.publicar_ejecucion(self.servicio, self.version, self.actor)
        self.servicio.refresh_from_db()
        self.version.refresh_from_db()
        self.assertFalse(self.servicio.activo)
        self.assertIsNone(self.servicio.workflow.version_activa_id)
        self.assertEqual(self.version.estado, "BORRADOR")
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_inmutabilidad_activa_historica_incluso_con_objetos_obsoletos(self):
        from apps.workflows.models import WorkflowVersion

        bloque = self._agregar()
        self._lineal(bloque)
        obsoleta = WorkflowVersion.objects.get(pk=self.version.pk)
        self.ejecucion.publicar_ejecucion(self.servicio, self.version, self.actor)
        for estado in ("ACTIVA", "HISTORICA"):
            with self.subTest(estado=estado):
                antes = list(self.version.etapas.values())
                eventos = RegistroAuditoria.objects.count()
                with self.assertRaises(ValidationError):
                    self.ejecucion.configurar_bloque(self.servicio, obsoleta, bloque, self.actor, tipo_actor="RESPONSABLE_TICKET")
                with self.assertRaises(ValidationError):
                    self.ejecucion.editar_bloque(self.servicio, obsoleta, bloque, self.actor, nombre="Alterado")
                with self.assertRaises(ValidationError):
                    self.ejecucion.conectar_bloques(self.servicio, obsoleta, self.inicio, self.fin, self.actor)
                self.assertEqual(list(self.version.etapas.values()), antes)
                self.assertEqual(bloque.configuracion_tarea.tipo_responsable, "SOLICITANTE")
                self.assertEqual(RegistroAuditoria.objects.count(), eventos)
                if estado == "ACTIVA":
                    nueva = self.ejecucion.preparar_ejecucion(self.servicio, self.actor)
                    self.assertNotEqual(nueva.pk, self.version.pk)
                    self.assertEqual(nueva.etapas.get(tipo="TAREA").configuracion_tarea.tipo_responsable, "SOLICITANTE")
                    self.ejecucion.publicar_ejecucion(self.servicio, nueva, self.actor)

    def test_servicio_simple_valido_y_asociado_exige_workflow_utilizable(self):
        from apps.catalogo.operaciones import activar_servicio
        from apps.workflows.models import WorkflowVersion

        simple = Servicio.objects.create(nombre="Simple", categoria=self.categoria, formulario=self.formulario, activo=False)
        self.assertTrue(activar_servicio(simple, self.actor).activo)
        with self.assertRaises(ValidationError):
            activar_servicio(self.servicio, self.actor)
        # Incluso una fila marcada ACTIVA por fuera de activar_version debe ser utilizable.
        self.servicio.refresh_from_db()
        corrupta = WorkflowVersion.objects.create(workflow=self.servicio.workflow, numero=2, estado="ACTIVA")
        self.servicio.workflow.version_activa = corrupta
        self.servicio.workflow.save()
        with self.assertRaises(ValidationError):
            activar_servicio(self.servicio, self.actor)
        self.servicio.refresh_from_db()
        self.assertFalse(self.servicio.activo)

    def test_espera_y_decision_reutilizan_estrategias_prioridad_y_fallback(self):
        from datetime import timedelta
        from unittest.mock import patch
        from django.utils import timezone
        from django_celery_beat.models import PeriodicTask
        from apps.workflows.estrategias import ESTRATEGIAS_POR_TIPO
        from apps.workflows.models import InstanciaEtapa
        from apps.workflows.motor import iniciar_workflow, reanudar_instancia

        programaciones = PeriodicTask.objects.count()
        espera = self._agregar("ESPERA", modo="DURACION", duracion_valor=2, duracion_unidad="HORAS")
        decision = self._agregar("DECISION")
        self.ejecucion.conectar_bloques(self.servicio, self.version, self.inicio, espera, self.actor, conexion=self.conexion)
        self.ejecucion.conectar_bloques(self.servicio, self.version, espera, decision, self.actor)
        preferida = self.ejecucion.conectar_bloques(
            self.servicio, self.version, decision, self.fin, self.actor,
            variable="cantidad", operador="MAYOR_QUE", valor="5", prioridad=1,
        )
        self.ejecucion.conectar_bloques(
            self.servicio, self.version, decision, espera, self.actor,
            variable="cantidad", operador="MAYOR_QUE", valor="2", prioridad=2,
        )
        fallback = self.ejecucion.conectar_bloques(
            self.servicio, self.version, decision, self.fin, self.actor, es_fallback=True, prioridad=3,
        )
        self.ejecucion.publicar_ejecucion(self.servicio, self.version, self.actor)
        self.servicio.refresh_from_db()
        instancia = iniciar_workflow(self.servicio.workflow, actor=self.actor)
        self.assertEqual(instancia.estado, "EN_ESPERA")
        espera_real = instancia.ejecuciones_etapa.order_by("-orden").first()
        self.assertEqual(espera_real.motivo_espera, "TEMPORAL")
        estrategia = ESTRATEGIAS_POR_TIPO["CONDICION"]
        ejecucion = InstanciaEtapa(etapa=decision, instancia_workflow=instancia, orden=3)
        self.assertEqual(estrategia.ejecutar(ejecucion, {"variables": {"cantidad": 10}}).transicion_seleccionada.pk, preferida.pk)
        self.assertEqual(estrategia.ejecutar(ejecucion, {"variables": {}}).transicion_seleccionada.pk, fallback.pk)
        with patch("apps.workflows.motor.timezone.now", return_value=timezone.now() + timedelta(hours=3)):
            reanudar_instancia(instancia)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, "COMPLETADA")
        self.assertEqual(PeriodicTask.objects.count(), programaciones)


class ConcurrenciaEjecucionConfigurableTests(TransactionTestCase):
    def setUp(self):
        # TransactionTestCase vacía también los permisos sembrados por migraciones.
        for codigo in ("catalogo.administrar", "workflows.administrar"):
            Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": codigo})
        EjecucionConfigurableTests.setUp(self)

    def test_edicion_espera_activacion_y_relee_estado_bajo_bloqueo(self):
        import threading
        from django.db import connection
        from apps.workflows.models import Workflow
        from apps.workflows.versionamiento import activar_version as activar_workflow

        intentando = threading.Event()
        resultados = []

        def observar(execute, sql, params, many, context):
            if '"workflows_workflow"' in sql and "FOR UPDATE" in sql:
                intentando.set()
            return execute(sql, params, many, context)

        def editar():
            try:
                with connection.execute_wrapper(observar):
                    self.ejecucion.agregar_bloque(self.servicio, self.version, self.actor, tipo="DECISION", nombre="Tardío")
                resultados.append("editado")
            except ValidationError:
                resultados.append("rechazado")
            except Exception as exc:
                resultados.append(exc)
            finally:
                connection.close()

        hilo = threading.Thread(target=editar, daemon=True)
        try:
            with transaction.atomic():
                workflow = Workflow.objects.select_for_update().get(pk=self.version.workflow_id)
                hilo.start()
                self.assertTrue(intentando.wait(timeout=10))
                activar_workflow(workflow, self.version, self.actor)
        finally:
            hilo.join(timeout=15)
        self.assertFalse(hilo.is_alive())
        self.assertEqual(resultados, ["rechazado"])
        self.assertEqual(self.version.etapas.count(), 2)
        self.assertFalse(RegistroAuditoria.objects.filter(datos_nuevos__nombre="Tardío").exists())


class StudioTests(TestCase):
    """4.4 — Studio: superficie de coordinación HTTP sobre Servicio,
    Formulario/Campo (Form Builder, 1.2), ejecución configurable (4.3) y
    DefinicionEntregable (4.2). No repite las pruebas de autorización ni
    de integridad de esos dominios (ya cubiertas en sus propios tests
    — `EjecucionConfigurableTests`, `DefinicionEntregableTests`,
    `PublicacionCatalogoTests`, Form Builder más arriba) — solo verifica
    que el Studio los coordina correctamente vía HTTP, sin duplicarlos."""

    def setUp(self):
        self.administrador = Usuario.objects.create_user("studio_admin", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.administrador, "catalogo.administrar")
        _otorgar_permiso(self.administrador, "formulario.administrar")
        _otorgar_permiso(self.administrador, "workflows.administrar")
        self.sin_permiso = Usuario.objects.create_user("studio_sin_permiso", password=CLAVE_PRUEBA)
        self.categoria = Categoria.objects.create(nombre="Studio Categoria")
        self.servicio = Servicio.objects.create(nombre="Pieza para redes", categoria=self.categoria, activo=False)
        self.proceso = Servicio.objects.create(
            nombre="Parrilla mensual", categoria=self.categoria, tipo=Servicio.Tipo.PROCESO, activo=False,
        )

    def _login_admin(self):
        self.client.login(username="studio_admin", password=CLAVE_PRUEBA)

    def _preparar_ejecucion_http(self, servicio=None):
        servicio = servicio or self.servicio
        self.client.post(reverse("catalogo:studio_ejecucion_configurar", args=[servicio.pk]))
        servicio.refresh_from_db()
        from apps.workflows.models import WorkflowVersion

        return servicio.workflow.versiones.get(estado=WorkflowVersion.Estado.BORRADOR)

    # --- Acceso / autorización / navegación ---

    def test_studio_requiere_autenticacion(self):
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]))
        self.assertEqual(respuesta.status_code, 302)

    def test_studio_rechaza_sin_permiso_catalogo_administrar(self):
        self.client.login(username="studio_sin_permiso", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]))
        self.assertEqual(respuesta.status_code, 403)

    def test_studio_accesible_para_servicio_y_proceso(self):
        self._login_admin()
        for servicio in (self.servicio, self.proceso):
            respuesta = self.client.get(reverse("catalogo:studio", args=[servicio.pk]))
            self.assertEqual(respuesta.status_code, 200)
            self.assertContains(respuesta, servicio.nombre)

    def test_navegacion_entre_las_5_secciones(self):
        self._login_admin()
        for tab, texto in (
            ("general", "Información general"), ("entrada", "Entrada"), ("ejecucion", "Ejecución"),
            ("salida", "Salida"), ("publicacion", "Publicación"),
        ):
            respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": tab})
            self.assertEqual(respuesta.status_code, 200)
            self.assertContains(respuesta, texto)

    def test_tab_invalido_cae_en_general(self):
        self._login_admin()
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "no-existe"})
        self.assertContains(respuesta, 'breadcrumb__current">General')

    def test_studio_lista_requiere_permiso_y_lista_servicios_reales(self):
        self.client.login(username="studio_sin_permiso", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.get(reverse("catalogo:studio_lista")).status_code, 403)
        self._login_admin()
        respuesta = self.client.get(reverse("catalogo:studio_lista"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, self.servicio.nombre)

    def test_studio_visible_en_navegacion_solo_con_permiso(self):
        self._login_admin()
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertContains(respuesta, "Diseñador")
        self.client.login(username="studio_sin_permiso", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertNotContains(respuesta, "Diseñador")

    # --- General ---

    def test_general_edita_datos_reales_y_audita(self):
        self._login_admin()
        eventos = RegistroAuditoria.objects.count()
        respuesta = self.client.post(
            reverse("catalogo:studio_general_guardar", args=[self.servicio.pk]),
            {
                "nombre": "Pieza renovada", "descripcion": "x", "categoria": self.categoria.pk,
                "tipo": "SERVICIO", "instrucciones": "", "alcance_visibilidad": "RESTRINGIDO",
            },
        )
        self.assertRedirects(respuesta, reverse("catalogo:studio", args=[self.servicio.pk]) + "?tab=general")
        self.servicio.refresh_from_db()
        self.assertEqual(self.servicio.nombre, "Pieza renovada")
        self.assertGreater(RegistroAuditoria.objects.count(), eventos)

    def test_general_validacion_real_nombre_vacio_no_guarda(self):
        self._login_admin()
        respuesta = self.client.post(
            reverse("catalogo:studio_general_guardar", args=[self.servicio.pk]),
            {"nombre": "", "categoria": self.categoria.pk, "tipo": "SERVICIO", "alcance_visibilidad": "RESTRINGIDO"},
            follow=True,
        )
        self.servicio.refresh_from_db()
        self.assertEqual(self.servicio.nombre, "Pieza para redes")
        self.assertContains(respuesta, "Revise los datos generales")

    # --- Entrada ---

    def test_entrada_crea_formulario_y_reutiliza_form_builder_sin_duplicar_motor(self):
        self._login_admin()
        self.client.post(
            reverse("catalogo:studio_entrada_version", args=[self.servicio.pk]), {"nombre": "Entrada", "descripcion": ""}
        )
        self.servicio.refresh_from_db()
        self.assertIsNotNone(self.servicio.formulario_id)
        self.assertEqual(self.servicio.formulario.versiones.count(), 1)
        self.assertEqual(self.servicio.formulario.versiones.get().estado, FormularioVersion.Estado.BORRADOR)

    def test_entrada_segunda_vez_abre_nueva_version_no_crea_segundo_formulario(self):
        self._login_admin()
        self.servicio.formulario = Formulario.objects.create(nombre="Existente")
        self.servicio.save()
        version = crear_nueva_version(self.servicio.formulario, self.administrador)
        activar_version(self.servicio.formulario, version, self.administrador)
        total_formularios = Formulario.objects.count()
        self.client.post(reverse("catalogo:studio_entrada_version", args=[self.servicio.pk]), {})
        self.assertEqual(Formulario.objects.count(), total_formularios)
        self.assertEqual(self.servicio.formulario.versiones.count(), 2)

    def test_entrada_agrega_campo_lo_activa_y_enlaza_previsualizacion_real(self):
        self._login_admin()
        self.client.post(
            reverse("catalogo:studio_entrada_version", args=[self.servicio.pk]), {"nombre": "Entrada", "descripcion": ""}
        )
        self.servicio.refresh_from_db()
        version = self.servicio.formulario.versiones.get()
        self.client.post(
            reverse("catalogo:studio_campo_crear", args=[self.servicio.pk]),
            {
                "campo-nuevo-tipo": "TEXTO", "campo-nuevo-etiqueta": "Nombre del contacto",
                "campo-nuevo-ayuda": "", "campo-nuevo-obligatorio": "on", "campo-nuevo-orden": "0",
            },
        )
        self.assertEqual(Campo.objects.filter(version=version).count(), 1)
        campo = Campo.objects.get(version=version)
        self.assertEqual(campo.etiqueta, "Nombre del contacto")
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "entrada"})
        self.assertContains(respuesta, reverse("formularios:previsualizar_version", args=[version.pk]))
        self.client.post(reverse("catalogo:studio_entrada_activar", args=[self.servicio.pk, version.pk]))
        version.refresh_from_db()
        self.assertEqual(version.estado, FormularioVersion.Estado.ACTIVA)

    def test_entrada_bloqueada_sobre_version_activa(self):
        self._login_admin()
        self.servicio.formulario = Formulario.objects.create(nombre="Existente")
        self.servicio.save()
        version = crear_nueva_version(self.servicio.formulario, self.administrador)
        campo = Campo.objects.create(version=version, tipo="TEXTO", etiqueta="Original", orden=0)
        activar_version(self.servicio.formulario, version, self.administrador)
        self.client.post(
            reverse("catalogo:studio_campo_crear", args=[self.servicio.pk]),
            {"campo-nuevo-tipo": "TEXTO", "campo-nuevo-etiqueta": "Intento", "campo-nuevo-orden": "0"},
        )
        self.assertFalse(Campo.objects.filter(etiqueta="Intento").exists())
        campo.refresh_from_db()
        self.assertEqual(campo.etiqueta, "Original")

    # --- Ejecución ---

    def test_ejecucion_configurar_crea_workflow_con_inicio_y_fin(self):
        self._login_admin()
        self.client.post(reverse("catalogo:studio_ejecucion_configurar", args=[self.servicio.pk]))
        self.servicio.refresh_from_db()
        self.assertIsNotNone(self.servicio.workflow_id)
        version = self.servicio.workflow.versiones.get()
        self.assertEqual(version.etapas.count(), 2)

    def test_ejecucion_agrega_actividad_con_actor_dinamico_y_la_inserta_en_la_cadena(self):
        self._login_admin()
        version = self._preparar_ejecucion_http()
        from apps.workflows.models import Etapa

        self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[self.servicio.pk]),
            {
                "nuevo-tipo": "ACTIVIDAD", "nuevo-nombre": "Preparar diseño", "nuevo-descripcion": "",
                "nuevo-config-tipo_actor": "SOLICITANTE",
            },
        )
        bloque = Etapa.objects.get(version=version, tipo="TAREA")
        self.assertEqual(bloque.configuracion_tarea.tipo_responsable, "SOLICITANTE")
        inicio = version.etapas.get(tipo="INICIO")
        self.assertEqual(inicio.transiciones_salientes.get().etapa_destino_id, bloque.pk)
        self.assertEqual(bloque.transiciones_salientes.get().etapa_destino.tipo, "FIN")

    def test_ejecucion_agrega_espera(self):
        self._login_admin()
        version = self._preparar_ejecucion_http()
        from apps.workflows.models import Etapa

        self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[self.servicio.pk]),
            {
                "nuevo-tipo": "ESPERA", "nuevo-nombre": "Esperar publicación", "nuevo-descripcion": "",
                "nuevo-config-modo": "DURACION", "nuevo-config-duracion_valor": "2", "nuevo-config-duracion_unidad": "DIAS",
            },
        )
        bloque = Etapa.objects.get(version=version, tipo="ESPERA")
        self.assertEqual(bloque.configuracion["duracion_valor"], 2)
        # Cableado real de la cadena (no solo configuración) — regresión
        # directa del bug de inserción detectado: sin esta aserción, un
        # bloque desconectado o con un autociclo pasaría inadvertido.
        inicio = version.etapas.get(tipo="INICIO")
        self.assertEqual(inicio.transiciones_salientes.get().etapa_destino_id, bloque.pk)
        self.assertEqual(bloque.transiciones_salientes.get().etapa_destino.tipo, "FIN")

    def test_ejecucion_agrega_aprobacion_con_equipo_y_configura_rutas_incluida_devolucion(self):
        self._login_admin()
        version = self._preparar_ejecucion_http()
        from apps.core.models import Equipo
        from apps.workflows.models import Etapa

        equipo = Equipo.objects.create(nombre="Mercadeo")
        self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[self.servicio.pk]),
            {
                "nuevo-tipo": "ACTIVIDAD", "nuevo-nombre": "Preparar diseño", "nuevo-descripcion": "",
                "nuevo-config-tipo_actor": "SOLICITANTE",
            },
        )
        actividad = Etapa.objects.get(version=version, tipo="TAREA")
        self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[self.servicio.pk]),
            {
                "nuevo-tipo": "APROBACION", "nuevo-nombre": "Revisión interna", "nuevo-descripcion": "",
                "nuevo-config-modo": "SECUENCIAL",
                "nuevo-participantes-TOTAL_FORMS": "1", "nuevo-participantes-INITIAL_FORMS": "0",
                "nuevo-participantes-MIN_NUM_FORMS": "1", "nuevo-participantes-MAX_NUM_FORMS": "1000",
                "nuevo-participantes-0-tipo": "EQUIPO", "nuevo-participantes-0-equipo": equipo.pk,
            },
        )
        aprobacion = Etapa.objects.get(version=version, tipo="APROBACION")
        self.assertEqual(aprobacion.configuracion_aprobacion.participantes.get().equipo_id, equipo.pk)
        self.assertEqual(aprobacion.transiciones_salientes.count(), 0)  # sin rutas todavía (punto de diseño)

        fin = version.etapas.get(tipo="FIN")
        self.client.post(
            reverse("catalogo:studio_ruta_aprobacion_guardar", args=[self.servicio.pk, aprobacion.pk]),
            {
                f"bloque-{aprobacion.pk}-ruta-destino_aprobada": fin.pk,
                f"bloque-{aprobacion.pk}-ruta-destino_devuelta": actividad.pk,
                f"bloque-{aprobacion.pk}-ruta-destino_rechazada": fin.pk,
            },
        )
        rutas = {t.resultado_aprobacion: t.etapa_destino_id for t in aprobacion.transiciones_salientes.all()}
        self.assertEqual(rutas["APROBADA"], fin.pk)
        self.assertEqual(rutas["DEVUELTA"], actividad.pk)
        self.assertEqual(rutas["RECHAZADA"], fin.pk)

    def test_ejecucion_agrega_decision_condicional_y_fallback(self):
        self._login_admin()
        version = self._preparar_ejecucion_http()
        from apps.workflows.models import Etapa

        self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[self.servicio.pk]),
            {"nuevo-tipo": "DECISION", "nuevo-nombre": "¿Cumple requisitos?", "nuevo-descripcion": ""},
        )
        decision = Etapa.objects.get(version=version, tipo="CONDICION")
        fin = version.etapas.get(tipo="FIN")
        self.client.post(
            reverse("catalogo:studio_fallback_guardar", args=[self.servicio.pk, decision.pk]),
            {f"bloque-{decision.pk}-fallback-destino": fin.pk},
        )
        self.assertTrue(decision.transiciones_salientes.filter(es_fallback=True, etapa_destino=fin).exists())
        self.client.post(
            reverse("catalogo:studio_condicional_crear", args=[self.servicio.pk, decision.pk]),
            {
                f"bloque-{decision.pk}-cond-nuevo-variable": "prioridad",
                f"bloque-{decision.pk}-cond-nuevo-operador": "IGUAL_A",
                f"bloque-{decision.pk}-cond-nuevo-valor": "ALTA",
                f"bloque-{decision.pk}-cond-nuevo-prioridad": "0",
                f"bloque-{decision.pk}-cond-nuevo-destino": fin.pk,
            },
        )
        self.assertEqual(decision.transiciones_salientes.filter(es_fallback=False).count(), 1)

    def test_ejecucion_eliminar_bloque_desconecta_y_borra(self):
        self._login_admin()
        version = self._preparar_ejecucion_http()
        from apps.workflows.models import Etapa

        self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[self.servicio.pk]),
            {"nuevo-tipo": "ACTIVIDAD", "nuevo-nombre": "Temporal", "nuevo-descripcion": "", "nuevo-config-tipo_actor": "SOLICITANTE"},
        )
        bloque = Etapa.objects.get(version=version, tipo="TAREA")
        self.client.post(reverse("catalogo:studio_bloque_eliminar", args=[self.servicio.pk, bloque.pk]))
        self.assertFalse(Etapa.objects.filter(pk=bloque.pk).exists())

    def test_ejecucion_protege_version_activa_no_se_puede_editar_desde_studio(self):
        self._login_admin()
        version = self._preparar_ejecucion_http()
        from apps.workflows.models import Etapa
        from apps.workflows import versionamiento as workflows_versionamiento

        workflows_versionamiento.activar_version(version.workflow, version, self.administrador)
        self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[self.servicio.pk]),
            {"nuevo-tipo": "ACTIVIDAD", "nuevo-nombre": "No debería existir", "nuevo-descripcion": "", "nuevo-config-tipo_actor": ""},
        )
        self.assertFalse(Etapa.objects.filter(version=version, nombre="No debería existir").exists())
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "ejecucion"})
        self.assertNotContains(respuesta, 'id="panel-nuevo-ACTIVIDAD"')

    # --- Salida ---

    def test_salida_lista_crea_edita_y_retira(self):
        self._login_admin()
        respuesta = self.client.post(
            reverse("catalogo:studio_entregable_crear", args=[self.servicio.pk]),
            {
                "entregable-nuevo-nombre": "Diseño final", "entregable-nuevo-descripcion": "",
                "entregable-nuevo-tipo": "ARCHIVO", "entregable-nuevo-obligatorio": "on", "entregable-nuevo-orden": "0",
            },
        )
        definicion = DefinicionEntregable.objects.get(servicio=self.servicio)
        self.assertEqual(definicion.tipo, "ARCHIVO")
        self.assertTrue(definicion.obligatorio)

        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "salida"})
        self.assertContains(respuesta, "Diseño final")

        self.client.post(
            reverse("catalogo:studio_entregable_editar", args=[self.servicio.pk, definicion.pk]),
            {
                f"entregable-{definicion.pk}-editar-nombre": "Diseño final revisado",
                f"entregable-{definicion.pk}-editar-descripcion": "",
                f"entregable-{definicion.pk}-editar-tipo": "ARCHIVO",
                f"entregable-{definicion.pk}-editar-orden": "0",
            },
        )
        definicion.refresh_from_db()
        self.assertEqual(definicion.nombre, "Diseño final revisado")
        self.assertFalse(definicion.obligatorio)  # checkbox ausente = False, reemplazo completo correcto

        self.client.post(reverse("catalogo:studio_entregable_retirar", args=[self.servicio.pk, definicion.pk]))
        definicion.refresh_from_db()
        self.assertFalse(definicion.activo)
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "salida"})
        self.assertNotContains(respuesta, "Diseño final revisado")

    def test_salida_admite_los_4_tipos(self):
        self._login_admin()
        for tipo in ("TEXTO", "ARCHIVO", "ENLACE", "CONFIRMACION"):
            with self.subTest(tipo=tipo):
                self.client.post(
                    reverse("catalogo:studio_entregable_crear", args=[self.servicio.pk]),
                    {
                        "entregable-nuevo-nombre": f"Entregable {tipo}", "entregable-nuevo-descripcion": "",
                        "entregable-nuevo-tipo": tipo, "entregable-nuevo-orden": "0",
                    },
                )
        self.assertEqual(
            set(DefinicionEntregable.objects.filter(servicio=self.servicio).values_list("tipo", flat=True)),
            {"TEXTO", "ARCHIVO", "ENLACE", "CONFIRMACION"},
        )

    # --- Publicación ---

    def test_publicacion_servicio_simple_sin_formulario_no_es_publicable(self):
        self._login_admin()
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "publicacion"})
        self.assertContains(respuesta, "Sin formulario con una versión activa")
        self.client.post(reverse("catalogo:studio_publicar", args=[self.servicio.pk]))
        self.servicio.refresh_from_db()
        self.assertFalse(self.servicio.activo)

    def test_publicacion_servicio_simple_con_formulario_activo_se_publica(self):
        self._login_admin()
        self.servicio.formulario = Formulario.objects.create(nombre="Entrada")
        self.servicio.save()
        version = crear_nueva_version(self.servicio.formulario, self.administrador)
        activar_version(self.servicio.formulario, version, self.administrador)
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "publicacion"})
        # Solo el botón de este formulario: el header global (V0) trae controles
        # deshabilitados a propósito (búsqueda/notificaciones "próximamente").
        formulario_publicar = respuesta.content.decode().split(
            reverse("catalogo:studio_publicar", args=[self.servicio.pk])
        )[1].split("</form>")[0]
        self.assertNotIn("disabled", formulario_publicar)
        self.client.post(reverse("catalogo:studio_publicar", args=[self.servicio.pk]))
        self.servicio.refresh_from_db()
        self.assertTrue(self.servicio.activo)

    def test_publicacion_proceso_sin_ejecucion_no_es_publicable_mensaje_traducido(self):
        self._login_admin()
        self.proceso.formulario = Formulario.objects.create(nombre="Entrada proceso")
        self.proceso.save()
        version = crear_nueva_version(self.proceso.formulario, self.administrador)
        activar_version(self.proceso.formulario, version, self.administrador)
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.proceso.pk]), {"tab": "publicacion"})
        self.assertContains(respuesta, "ejecución")
        self.client.post(reverse("catalogo:studio_publicar", args=[self.proceso.pk]))
        self.proceso.refresh_from_db()
        self.assertFalse(self.proceso.activo)

    def test_publicacion_con_ejecucion_incompleta_no_publica_y_no_deja_estado_parcial(self):
        self._login_admin()
        self.servicio.formulario = Formulario.objects.create(nombre="Entrada")
        self.servicio.save()
        version_form = crear_nueva_version(self.servicio.formulario, self.administrador)
        activar_version(self.servicio.formulario, version_form, self.administrador)
        self._preparar_ejecucion_http()  # INICIO->FIN directo, sin bloques: válido y activable en realidad,
        # así que forzamos un bloque incompleto para probar el caso de rollback real.
        from apps.workflows.models import Etapa

        version = self.servicio.workflow.versiones.get()
        self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[self.servicio.pk]),
            {
                "nuevo-tipo": "APROBACION", "nuevo-nombre": "Revisión", "nuevo-descripcion": "",
                "nuevo-config-modo": "SECUENCIAL",
                "nuevo-participantes-TOTAL_FORMS": "1", "nuevo-participantes-INITIAL_FORMS": "0",
                "nuevo-participantes-MIN_NUM_FORMS": "1", "nuevo-participantes-MAX_NUM_FORMS": "1000",
                "nuevo-participantes-0-tipo": "RESPONSABLE_TICKET",
            },
        )
        respuesta = self.client.post(reverse("catalogo:studio_publicar", args=[self.servicio.pk]))
        self.servicio.refresh_from_db()
        self.assertFalse(self.servicio.activo)
        version.refresh_from_db()
        self.assertEqual(version.estado, "BORRADOR")  # sin activar parcialmente

    # --- Compatibilidad ---

    def test_compatibilidad_editor_tecnico_sigue_viendo_los_bloques_del_studio(self):
        self._login_admin()
        version = self._preparar_ejecucion_http()
        self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[self.servicio.pk]),
            {
                "nuevo-tipo": "ACTIVIDAD", "nuevo-nombre": "Visible en editor técnico", "nuevo-descripcion": "",
                "nuevo-config-tipo_actor": "SOLICITANTE",
            },
        )
        respuesta = self.client.get(reverse("workflows:version_detalle", args=[version.pk]))
        self.assertContains(respuesta, "Visible en editor técnico")

    def test_compatibilidad_no_crea_segundo_grafo_ni_segunda_autorizacion(self):
        from apps.workflows.models import Workflow, WorkflowVersion

        self._login_admin()
        self._preparar_ejecucion_http()
        self.assertEqual(Workflow.objects.count(), 1)
        self.assertEqual(WorkflowVersion.objects.count(), 1)


# ---------------------------------------------------------------------------
# Incremento 4.4.1 — Cierre funcional del Studio V1: creación, visibilidad y
# responsables sin Django Admin. Reutiliza Servicio / ServicioVisibilidad /
# ServicioResponsable (nada paralelo).
# ---------------------------------------------------------------------------


class StudioCierreFuncionalTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user("cierre_admin", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.admin, "catalogo.administrar")
        self.sin_permiso = Usuario.objects.create_user("cierre_sin_permiso", password=CLAVE_PRUEBA)
        self.categoria = Categoria.objects.create(nombre="Mercadeo")
        self.servicio = Servicio.objects.create(nombre="Diseño de piezas", categoria=self.categoria, activo=False)
        self.persona = Usuario.objects.create_user("cierre_persona", password=CLAVE_PRUEBA)
        self.area = Area.objects.create(nombre="Mercadeo área")
        self.unidad = UnidadNegocio.objects.create(nombre="Unidad Norte")
        self.equipo = Equipo.objects.create(nombre="Equipo de Mercadeo")

    def _login_admin(self):
        self.client.login(username="cierre_admin", password=CLAVE_PRUEBA)

    def _crear(self, **extra):
        datos = {
            "tipo": "SERVICIO", "nombre": "Nuevo desde Studio", "categoria": self.categoria.pk,
            "categoria_nueva": "", "descripcion": "Desc", "instrucciones": "Instr",
        }
        datos.update(extra)
        return self.client.post(reverse("catalogo:studio_crear"), datos)

    def _conceder(self, **datos):
        return self.client.post(reverse("catalogo:studio_visibilidad_conceder", args=[self.servicio.pk]), datos)

    def _agregar_responsable(self, **datos):
        return self.client.post(reverse("catalogo:studio_responsable_agregar", args=[self.servicio.pk]), datos)

    # --- Creación ---

    def test_crear_servicio_desde_studio_queda_en_borrador_y_redirige_a_general(self):
        from apps.workflows.models import Workflow

        self._login_admin()
        respuesta = self._crear()
        servicio = Servicio.objects.get(nombre="Nuevo desde Studio")
        self.assertRedirects(respuesta, f"{reverse('catalogo:studio', args=[servicio.pk])}?tab=general")
        self.assertEqual(servicio.tipo, Servicio.Tipo.SERVICIO)
        self.assertFalse(servicio.activo)
        self.assertEqual(servicio.alcance_visibilidad, Servicio.AlcanceVisibilidad.RESTRINGIDO)
        self.assertIsNone(servicio.workflow)
        self.assertIsNone(servicio.formulario)
        self.assertEqual(Workflow.objects.count(), 0)
        self.assertEqual(Formulario.objects.count(), 0)
        self.assertEqual(DefinicionEntregable.objects.count(), 0)
        self.assertFalse(servicio.visibilidad.exists())
        self.assertFalse(servicio.responsables.exists())

    def test_crear_proceso_desde_studio(self):
        self._login_admin()
        self._crear(tipo="PROCESO", nombre="Parrilla nueva")
        proceso = Servicio.objects.get(nombre="Parrilla nueva")
        self.assertEqual(proceso.tipo, Servicio.Tipo.PROCESO)
        self.assertFalse(proceso.activo)

    def test_crear_con_categoria_nueva_la_crea_y_audita(self):
        self._login_admin()
        self._crear(nombre="Con categoría nueva", categoria="", categoria_nueva="Comunicaciones")
        categoria = Categoria.objects.get(nombre="Comunicaciones")
        self.assertEqual(Servicio.objects.get(nombre="Con categoría nueva").categoria, categoria)
        self.assertEqual(
            RegistroAuditoria.objects.filter(modelo="catalogo.categoria", object_id=categoria.pk).count(), 1
        )

    def test_crear_exige_categoria_y_rechaza_ambas(self):
        self._login_admin()
        antes = Servicio.objects.count()
        sin = self._crear(nombre="Sin categoría", categoria="", categoria_nueva="")
        ambas = self._crear(nombre="Ambas", categoria_nueva="Otra")
        self.assertEqual(sin.status_code, 200)
        self.assertEqual(ambas.status_code, 200)
        self.assertEqual(Servicio.objects.count(), antes)
        self.assertFalse(Categoria.objects.filter(nombre="Otra").exists())

    def test_crear_categoria_duplicada_no_crea_nada(self):
        self._login_admin()
        antes = Servicio.objects.count()
        respuesta = self._crear(nombre="Duplicada", categoria="", categoria_nueva="mercadeo")
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Ya existe una categoría")
        self.assertEqual(Servicio.objects.count(), antes)
        self.assertEqual(Categoria.objects.filter(nombre__iexact="mercadeo").count(), 1)

    def test_crear_sin_permiso_o_sin_sesion_no_crea(self):
        antes = Servicio.objects.count()
        self.assertEqual(self.client.get(reverse("catalogo:studio_crear")).status_code, 302)
        self.client.login(username="cierre_sin_permiso", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.get(reverse("catalogo:studio_crear")).status_code, 403)
        self.assertEqual(self._crear().status_code, 403)
        self.assertEqual(Servicio.objects.count(), antes)

    def test_crear_audita_una_sola_vez(self):
        self._login_admin()
        self._crear()
        servicio = Servicio.objects.get(nombre="Nuevo desde Studio")
        eventos = RegistroAuditoria.objects.filter(modelo="catalogo.servicio", object_id=servicio.pk)
        self.assertEqual(eventos.count(), 1)
        self.assertEqual(eventos.get().accion, RegistroAuditoria.Accion.CREAR)
        self.assertEqual(eventos.get().usuario, self.admin)

    def test_lista_ofrece_crear_y_configurar(self):
        self._login_admin()
        respuesta = self.client.get(reverse("catalogo:studio_lista"))
        self.assertContains(respuesta, "Crear Servicio o Proceso")
        self.assertContains(respuesta, "Configurar")
        self.assertContains(respuesta, reverse("catalogo:studio_crear"))

    def test_operacion_crear_servicio_exige_permiso(self):
        from django.core.exceptions import PermissionDenied

        from apps.catalogo.operaciones import crear_servicio

        with self.assertRaises(PermissionDenied):
            crear_servicio(
                self.sin_permiso, nombre="X", categoria=self.categoria, tipo=Servicio.Tipo.SERVICIO
            )
        self.assertFalse(Servicio.objects.filter(nombre="X").exists())

    # --- Visibilidad ---

    def test_general_muestra_concesiones_existentes_del_dominio(self):
        ServicioVisibilidad.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.AREA, area=self.area
        )
        self._login_admin()
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "general"})
        self.assertContains(respuesta, "Mercadeo área")
        self.assertContains(respuesta, "Conceder acceso")

    def test_general_publico_interno_no_ofrece_conceder(self):
        self.servicio.alcance_visibilidad = Servicio.AlcanceVisibilidad.PUBLICO_INTERNO
        self.servicio.save()
        self._login_admin()
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "general"})
        self.assertNotContains(respuesta, "Conceder acceso")
        self.assertContains(respuesta, "público interno")

    def test_conceder_visibilidad_por_usuario_area_y_unidad(self):
        self._login_admin()
        self._conceder(tipo_alcance="USUARIO", usuario=self.persona.pk)
        self._conceder(tipo_alcance="AREA", area=self.area.pk)
        self._conceder(tipo_alcance="UNIDAD", unidad_negocio=self.unidad.pk)
        filas = {f.tipo_alcance: f for f in ServicioVisibilidad.objects.filter(servicio=self.servicio, activo=True)}
        self.assertEqual(set(filas), {"USUARIO", "AREA", "UNIDAD"})
        self.assertEqual(filas["USUARIO"].usuario, self.persona)
        self.assertIsNone(filas["USUARIO"].area)
        self.assertEqual(filas["AREA"].area, self.area)
        self.assertEqual(filas["UNIDAD"].unidad_negocio, self.unidad)
        self.assertEqual(RegistroAuditoria.objects.filter(modelo="catalogo.serviciovisibilidad").count(), 3)

    def test_conceder_visibilidad_duplicada_se_rechaza_sin_duplicar_ni_auditar(self):
        self._login_admin()
        self._conceder(tipo_alcance="AREA", area=self.area.pk)
        self._conceder(tipo_alcance="AREA", area=self.area.pk)
        self.assertEqual(ServicioVisibilidad.objects.filter(servicio=self.servicio, activo=True).count(), 1)
        self.assertEqual(RegistroAuditoria.objects.filter(modelo="catalogo.serviciovisibilidad").count(), 1)

    def test_conceder_rechaza_tipo_sin_su_objeto_e_inactivos(self):
        self._login_admin()
        self._conceder(tipo_alcance="USUARIO")
        self._conceder(tipo_alcance="AREA", usuario=self.persona.pk)
        inactivo = Usuario.objects.create_user("cierre_inactivo", password=CLAVE_PRUEBA, is_active=False)
        self._conceder(tipo_alcance="USUARIO", usuario=inactivo.pk)
        self.assertFalse(ServicioVisibilidad.objects.filter(servicio=self.servicio).exists())

    def test_retirar_visibilidad_desactiva_audita_y_permite_reconceder(self):
        self._login_admin()
        self._conceder(tipo_alcance="AREA", area=self.area.pk)
        concesion = ServicioVisibilidad.objects.get(servicio=self.servicio)
        self.client.post(reverse("catalogo:studio_visibilidad_retirar", args=[self.servicio.pk, concesion.pk]))
        concesion.refresh_from_db()
        self.assertFalse(concesion.activo)
        evento = RegistroAuditoria.objects.filter(
            modelo="catalogo.serviciovisibilidad", object_id=concesion.pk, accion=RegistroAuditoria.Accion.ACTUALIZAR
        ).get()
        self.assertTrue(evento.datos_anteriores["activo"])
        self.assertFalse(evento.datos_nuevos["activo"])
        self._conceder(tipo_alcance="AREA", area=self.area.pk)
        self.assertEqual(ServicioVisibilidad.objects.filter(servicio=self.servicio, activo=True).count(), 1)
        self.assertEqual(ServicioVisibilidad.objects.filter(servicio=self.servicio).count(), 2)

    def test_retirar_concesion_de_otro_servicio_es_404(self):
        otro = Servicio.objects.create(nombre="Otro", categoria=self.categoria, activo=False)
        concesion = ServicioVisibilidad.objects.create(
            servicio=otro, tipo_alcance=ServicioVisibilidad.TipoAlcance.AREA, area=self.area
        )
        self._login_admin()
        respuesta = self.client.post(
            reverse("catalogo:studio_visibilidad_retirar", args=[self.servicio.pk, concesion.pk])
        )
        self.assertEqual(respuesta.status_code, 404)
        concesion.refresh_from_db()
        self.assertTrue(concesion.activo)

    def test_visibilidad_sin_permiso_no_escribe(self):
        concesion = ServicioVisibilidad.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.AREA, area=self.area
        )
        self.client.login(username="cierre_sin_permiso", password=CLAVE_PRUEBA)
        self.assertEqual(self._conceder(tipo_alcance="USUARIO", usuario=self.persona.pk).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("catalogo:studio_visibilidad_retirar", args=[self.servicio.pk, concesion.pk])
            ).status_code,
            403,
        )
        concesion.refresh_from_db()
        self.assertTrue(concesion.activo)
        self.assertEqual(ServicioVisibilidad.objects.count(), 1)

    def test_visibilidad_concedida_en_studio_es_la_que_interpreta_el_catalogo(self):
        # Misma fuente de verdad: `servicios_visibles_para` (CU-011) lee las
        # filas que Studio escribió, sin ninguna capa intermedia.
        Servicio.objects.filter(pk=self.servicio.pk).update(activo=True)
        UsuarioArea.objects.create(usuario=self.persona, area=self.area)
        ajeno = Usuario.objects.create_user("cierre_ajeno", password=CLAVE_PRUEBA)
        self.assertNotIn(self.servicio, servicios_visibles_para(self.persona))
        self._login_admin()
        self._conceder(tipo_alcance="AREA", area=self.area.pk)
        self.assertIn(self.servicio, servicios_visibles_para(self.persona))
        self.assertNotIn(self.servicio, servicios_visibles_para(ajeno))
        concesion = ServicioVisibilidad.objects.get(servicio=self.servicio, activo=True)
        self.client.post(reverse("catalogo:studio_visibilidad_retirar", args=[self.servicio.pk, concesion.pk]))
        self.assertNotIn(self.servicio, servicios_visibles_para(self.persona))

    def test_cambiar_alcance_a_publico_en_general_se_audita_y_aplica(self):
        self._login_admin()
        self.client.post(
            reverse("catalogo:studio_general_guardar", args=[self.servicio.pk]),
            {
                "nombre": self.servicio.nombre, "descripcion": "", "categoria": self.categoria.pk,
                "tipo": "SERVICIO", "instrucciones": "", "alcance_visibilidad": "PUBLICO_INTERNO",
            },
        )
        self.servicio.refresh_from_db()
        self.assertEqual(self.servicio.alcance_visibilidad, Servicio.AlcanceVisibilidad.PUBLICO_INTERNO)
        self.assertTrue(
            RegistroAuditoria.objects.filter(
                modelo="catalogo.servicio", object_id=self.servicio.pk, accion=RegistroAuditoria.Accion.ACTUALIZAR
            ).exists()
        )

    # --- Responsables ---

    def test_agregar_responsable_usuario_y_equipo_y_audita(self):
        self._login_admin()
        self._agregar_responsable(tipo_responsable="USUARIO", usuario=self.persona.pk)
        self._agregar_responsable(tipo_responsable="EQUIPO", equipo=self.equipo.pk)
        filas = {r.tipo_responsable: r for r in ServicioResponsable.objects.filter(servicio=self.servicio, activo=True)}
        self.assertEqual(filas["USUARIO"].usuario, self.persona)
        self.assertIsNone(filas["USUARIO"].equipo)
        self.assertEqual(filas["EQUIPO"].equipo, self.equipo)
        self.assertEqual(RegistroAuditoria.objects.filter(modelo="catalogo.servicioresponsable").count(), 2)

    def test_agregar_responsable_duplicado_o_invalido_no_escribe(self):
        self._login_admin()
        self._agregar_responsable(tipo_responsable="EQUIPO", equipo=self.equipo.pk)
        self._agregar_responsable(tipo_responsable="EQUIPO", equipo=self.equipo.pk)
        self._agregar_responsable(tipo_responsable="EQUIPO")
        self._agregar_responsable(tipo_responsable="USUARIO", equipo=self.equipo.pk)
        inactivo = Equipo.objects.create(nombre="Equipo inactivo", activo=False)
        self._agregar_responsable(tipo_responsable="EQUIPO", equipo=inactivo.pk)
        self.assertEqual(ServicioResponsable.objects.filter(servicio=self.servicio).count(), 1)
        self.assertEqual(RegistroAuditoria.objects.filter(modelo="catalogo.servicioresponsable").count(), 1)

    def test_retirar_responsable_desactiva_y_audita(self):
        self._login_admin()
        self._agregar_responsable(tipo_responsable="USUARIO", usuario=self.persona.pk)
        responsable = ServicioResponsable.objects.get(servicio=self.servicio)
        self.client.post(reverse("catalogo:studio_responsable_retirar", args=[self.servicio.pk, responsable.pk]))
        responsable.refresh_from_db()
        self.assertFalse(responsable.activo)
        self.assertTrue(
            RegistroAuditoria.objects.filter(
                modelo="catalogo.servicioresponsable", object_id=responsable.pk,
                accion=RegistroAuditoria.Accion.ACTUALIZAR,
            ).exists()
        )
        self._agregar_responsable(tipo_responsable="USUARIO", usuario=self.persona.pk)
        self.assertEqual(ServicioResponsable.objects.filter(servicio=self.servicio, activo=True).count(), 1)

    def test_responsables_sin_permiso_no_escriben(self):
        responsable = ServicioResponsable.objects.create(
            servicio=self.servicio, tipo_responsable=ServicioResponsable.TipoResponsable.USUARIO, usuario=self.persona
        )
        self.client.login(username="cierre_sin_permiso", password=CLAVE_PRUEBA)
        self.assertEqual(self._agregar_responsable(tipo_responsable="EQUIPO", equipo=self.equipo.pk).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("catalogo:studio_responsable_retirar", args=[self.servicio.pk, responsable.pk])
            ).status_code,
            403,
        )
        responsable.refresh_from_db()
        self.assertTrue(responsable.activo)
        self.assertEqual(ServicioResponsable.objects.count(), 1)

    def test_responsable_configurado_en_studio_es_el_que_lee_tickets(self):
        # ServicioResponsable sigue siendo la única fuente de verdad: la
        # autorización de atención (2.3) reconoce lo que Studio configuró.
        from apps.core.models import MiembroEquipo
        from apps.tickets.autorizacion import usuario_es_responsable_configurado

        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.persona)
        self.assertFalse(usuario_es_responsable_configurado(self.persona, self.servicio))
        self._login_admin()
        self._agregar_responsable(tipo_responsable="EQUIPO", equipo=self.equipo.pk)
        self.assertTrue(usuario_es_responsable_configurado(self.persona, self.servicio))
        responsable = ServicioResponsable.objects.get(servicio=self.servicio)
        self.client.post(reverse("catalogo:studio_responsable_retirar", args=[self.servicio.pk, responsable.pk]))
        self.assertFalse(usuario_es_responsable_configurado(self.persona, self.servicio))

    # --- Publicación ---

    def test_publicacion_muestra_visibilidad_y_responsables_reales_sin_remitir_a_admin(self):
        ServicioVisibilidad.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.UNIDAD, unidad_negocio=self.unidad
        )
        ServicioResponsable.objects.create(
            servicio=self.servicio, tipo_responsable=ServicioResponsable.TipoResponsable.EQUIPO, equipo=self.equipo
        )
        self._login_admin()
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "publicacion"})
        self.assertContains(respuesta, "Unidad Norte")
        self.assertContains(respuesta, "Equipo de Mercadeo")
        self.assertContains(respuesta, "Visibilidad configurada")
        self.assertContains(respuesta, "Responsables configurados")
        self.assertNotContains(respuesta, "Django Admin")

    def test_publicacion_publico_interno_y_avisos_informativos(self):
        self._login_admin()
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "publicacion"})
        self.assertContains(respuesta, "nadie lo encontrará")
        self.assertContains(respuesta, "Sin responsables configurados")
        self.servicio.alcance_visibilidad = Servicio.AlcanceVisibilidad.PUBLICO_INTERNO
        self.servicio.save()
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "publicacion"})
        self.assertContains(respuesta, "Público interno")
        self.assertContains(respuesta, "Visibilidad configurada")

    def test_publicacion_no_exige_responsables_ni_concesiones_regla_de_dominio_intacta(self):
        self.servicio.formulario = Formulario.objects.create(nombre="Entrada cierre")
        self.servicio.save()
        version = crear_nueva_version(self.servicio.formulario, self.admin)
        activar_version(self.servicio.formulario, version, self.admin)
        self._login_admin()
        self.client.post(reverse("catalogo:studio_publicar", args=[self.servicio.pk]))
        self.servicio.refresh_from_db()
        self.assertTrue(self.servicio.activo)
        self.assertFalse(self.servicio.responsables.exists())

    def test_recorrido_completo_sin_admin_crear_configurar_publicar(self):
        from apps.catalogo.operaciones import asociar_formulario_nuevo

        self._login_admin()
        self._crear(nombre="Diseño de piezas gráficas")
        servicio = Servicio.objects.get(nombre="Diseño de piezas gráficas")
        formulario = asociar_formulario_nuevo(servicio, self.admin, nombre="Entrada piezas")
        activar_version(formulario, formulario.versiones.get(), self.admin)
        UsuarioArea.objects.create(usuario=self.persona, area=self.area)
        self.client.post(
            reverse("catalogo:studio_visibilidad_conceder", args=[servicio.pk]),
            {"tipo_alcance": "AREA", "area": self.area.pk},
        )
        self.client.post(
            reverse("catalogo:studio_responsable_agregar", args=[servicio.pk]),
            {"tipo_responsable": "EQUIPO", "equipo": self.equipo.pk},
        )
        self.client.post(reverse("catalogo:studio_publicar", args=[servicio.pk]))
        servicio.refresh_from_db()
        self.assertTrue(servicio.activo)
        self.assertIn(servicio, servicios_visibles_para(self.persona))

    # --- Regresión ---

    def test_cinco_pestanas_siguen_respondiendo_con_visibilidad_y_responsables_cargados(self):
        ServicioVisibilidad.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.USUARIO, usuario=self.persona
        )
        ServicioResponsable.objects.create(
            servicio=self.servicio, tipo_responsable=ServicioResponsable.TipoResponsable.USUARIO, usuario=self.persona
        )
        self._login_admin()
        for tab in ("general", "entrada", "ejecucion", "salida", "publicacion"):
            respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": tab})
            self.assertEqual(respuesta.status_code, 200, tab)


# ---------------------------------------------------------------------------
# Incremento 4.4.2 — Edición continua del flujo en Studio (Ejecución).
# Un BORRADOR nunca queda "cerrado": que exista FIN, un camino completo o una
# validación correcta no bloquea agregar/editar. Solo ACTIVA/HISTORICA son
# inmutables.
# ---------------------------------------------------------------------------


class StudioEdicionContinuaTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user("continua_admin", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.admin, "catalogo.administrar")
        _otorgar_permiso(self.admin, "workflows.administrar")
        self.categoria = Categoria.objects.create(nombre="Continua")
        self.servicio = Servicio.objects.create(nombre="Flujo continuo", categoria=self.categoria, activo=False)
        self.client.login(username="continua_admin", password=CLAVE_PRUEBA)
        self.client.post(reverse("catalogo:studio_ejecucion_configurar", args=[self.servicio.pk]))
        self.servicio.refresh_from_db()
        from apps.workflows.models import WorkflowVersion

        self.version = self.servicio.workflow.versiones.get(estado=WorkflowVersion.Estado.BORRADOR)

    # --- helpers ---

    def _crear_bloque(self, tipo, nombre, despues_de=None):
        datos = {"nuevo-tipo": tipo, "nuevo-nombre": nombre, "nuevo-descripcion": ""}
        if tipo == "ACTIVIDAD":
            datos["nuevo-config-tipo_actor"] = "SOLICITANTE"
        elif tipo == "ESPERA":
            datos.update({
                "nuevo-config-modo": "DURACION", "nuevo-config-duracion_valor": "1",
                "nuevo-config-duracion_unidad": "DIAS",
            })
        elif tipo == "APROBACION":
            datos.update({
                "nuevo-config-modo": "SECUENCIAL",
                "nuevo-participantes-TOTAL_FORMS": "1", "nuevo-participantes-INITIAL_FORMS": "0",
                "nuevo-participantes-MIN_NUM_FORMS": "1", "nuevo-participantes-MAX_NUM_FORMS": "1000",
                "nuevo-participantes-0-tipo": "RESPONSABLE_TICKET",
            })
        if despues_de is not None:
            datos["despues_de"] = despues_de.pk
        return self.client.post(reverse("catalogo:studio_bloque_crear", args=[self.servicio.pk]), datos)

    def _bloque(self, nombre):
        from apps.workflows.models import Etapa

        return Etapa.objects.get(version=self.version, nombre=nombre)

    def _cadena(self):
        """Nombres del camino principal (sigue APROBADA / fallback / única salida)."""
        nombres = []
        etapa = self.version.etapas.get(tipo="INICIO")
        for _ in range(30):
            salientes = list(etapa.transiciones_salientes.all())
            if etapa.tipo == "APROBACION":
                salientes = [t for t in salientes if t.resultado_aprobacion == "APROBADA"]
            elif etapa.tipo == "CONDICION":
                salientes = [t for t in salientes if t.es_fallback]
            if not salientes:
                nombres.append("(sin salida)")
                break
            etapa = salientes[0].etapa_destino
            if etapa.tipo == "FIN":
                nombres.append("FIN")
                break
            nombres.append(etapa.nombre)
        return nombres

    def _pagina_ejecucion(self, **params):
        params["tab"] = "ejecucion"
        return self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), params)

    def _assert_formularios_de_alta_visibles(self):
        respuesta = self._pagina_ejecucion()
        for codigo in ("ACTIVIDAD", "APROBACION", "ESPERA", "DECISION"):
            self.assertContains(respuesta, f'id="panel-nuevo-{codigo}"')
        self.assertNotContains(respuesta, "Configure las rutas del último bloque")

    # --- Edición continua ---

    def test_borrador_vacio_agrega_actividad_y_sigue_editable(self):
        self._crear_bloque("ACTIVIDAD", "A")
        self.assertEqual(self._cadena(), ["A", "FIN"])
        self._assert_formularios_de_alta_visibles()

    def test_actividad_conectada_a_fin_admite_otra_a_b_fin(self):
        from apps.workflows.validacion import validar_estructura

        self._crear_bloque("ACTIVIDAD", "A")
        self._crear_bloque("ACTIVIDAD", "B")
        self.assertEqual(self._cadena(), ["A", "B", "FIN"])
        self.assertEqual(validar_estructura(self.version), [])

    def test_agregar_a_b_c_sucesivamente_no_pierde_editabilidad(self):
        for nombre in ("A", "B", "C"):
            self._crear_bloque("ACTIVIDAD", nombre)
            self._assert_formularios_de_alta_visibles()
        self.assertEqual(self._cadena(), ["A", "B", "C", "FIN"])

    def test_flujo_estructuralmente_valido_en_borrador_sigue_editable(self):
        from apps.workflows.validacion import validar_estructura

        self._crear_bloque("ACTIVIDAD", "A")
        self.assertEqual(validar_estructura(self.version), [])  # válido y completo
        self._assert_formularios_de_alta_visibles()
        self._crear_bloque("ESPERA", "Esperar")
        self.assertEqual(self._cadena(), ["A", "Esperar", "FIN"])

    def test_aprobacion_recien_agregada_no_cierra_el_flujo(self):
        from apps.workflows.models import TransicionEtapa

        self._crear_bloque("ACTIVIDAD", "A")
        self._crear_bloque("APROBACION", "Revisión")
        revision = self._bloque("Revisión")
        # Corrección de 4.4 intacta: una aprobación nueva no recibe salida genérica a FIN.
        self.assertEqual(revision.transiciones_salientes.count(), 0)
        self._assert_formularios_de_alta_visibles()
        self._crear_bloque("ACTIVIDAD", "B")
        self.assertEqual(self._cadena(), ["A", "Revisión", "B", "FIN"])
        for transicion in revision.transiciones_salientes.all():
            self.assertEqual(transicion.resultado_aprobacion, "APROBADA")
            transicion.full_clean()
        self.assertEqual(TransicionEtapa.objects.filter(etapa_origen=revision).count(), 1)

    def test_decision_recien_agregada_no_cierra_el_flujo(self):
        self._crear_bloque("DECISION", "¿Procede?")
        decision = self._bloque("¿Procede?")
        self.assertEqual(decision.transiciones_salientes.count(), 0)
        self._assert_formularios_de_alta_visibles()
        self._crear_bloque("ACTIVIDAD", "B")
        self.assertEqual(self._cadena(), ["¿Procede?", "B", "FIN"])
        salida = decision.transiciones_salientes.get()
        self.assertTrue(salida.es_fallback)
        self.assertEqual((salida.variable, salida.operador, salida.valor), ("", "", ""))
        salida.full_clean()

    def test_configurar_aprobacion_y_seguir_agregando_y_configurando(self):
        self._crear_bloque("APROBACION", "Revisión")
        revision = self._bloque("Revisión")
        fin = self.version.etapas.get(tipo="FIN")
        self.client.post(
            reverse("catalogo:studio_ruta_aprobacion_guardar", args=[self.servicio.pk, revision.pk]),
            {
                f"bloque-{revision.pk}-ruta-destino_aprobada": fin.pk,
                f"bloque-{revision.pk}-ruta-destino_rechazada": fin.pk,
            },
        )
        self._assert_formularios_de_alta_visibles()
        self._crear_bloque("ACTIVIDAD", "Después")
        self.assertEqual(self._cadena(), ["Revisión", "Después", "FIN"])
        # La ruta rechazada no se tocó: sigue yendo a FIN.
        rutas = {t.resultado_aprobacion: t.etapa_destino.tipo for t in revision.transiciones_salientes.all()}
        self.assertEqual(rutas["RECHAZADA"], "FIN")
        # Y todavía se puede reconfigurar un bloque existente.
        actividad = self._bloque("Después")
        self.client.post(
            reverse("catalogo:studio_bloque_editar", args=[self.servicio.pk, actividad.pk]),
            {
                f"bloque-{actividad.pk}-editar-nombre": "Después (editada)", f"bloque-{actividad.pk}-editar-descripcion": "",
                f"bloque-{actividad.pk}-config-tipo_actor": "RESPONSABLE_TICKET",
            },
        )
        actividad.refresh_from_db()
        self.assertEqual(actividad.nombre, "Después (editada)")

    def test_eliminar_el_unico_bloque_no_bloquea_nuevas_altas(self):
        self._crear_bloque("ACTIVIDAD", "A")
        self.client.post(
            reverse("catalogo:studio_bloque_eliminar", args=[self.servicio.pk, self._bloque("A").pk])
        )
        self.assertEqual(self._cadena(), ["FIN"])  # INICIO→FIN restablecido
        self._crear_bloque("ACTIVIDAD", "B")
        self.assertEqual(self._cadena(), ["B", "FIN"])

    def test_eliminar_bloque_intermedio_lineal_reconecta_vecinos(self):
        for nombre in ("A", "B", "C"):
            self._crear_bloque("ACTIVIDAD", nombre)
        self.client.post(
            reverse("catalogo:studio_bloque_eliminar", args=[self.servicio.pk, self._bloque("B").pk])
        )
        self.assertEqual(self._cadena(), ["A", "C", "FIN"])

    def test_eliminar_aprobacion_deja_el_flujo_editable(self):
        self._crear_bloque("ACTIVIDAD", "A")
        self._crear_bloque("APROBACION", "Revisión")
        revision = self._bloque("Revisión")
        fin = self.version.etapas.get(tipo="FIN")
        self.client.post(
            reverse("catalogo:studio_ruta_aprobacion_guardar", args=[self.servicio.pk, revision.pk]),
            {
                f"bloque-{revision.pk}-ruta-destino_aprobada": fin.pk,
                f"bloque-{revision.pk}-ruta-destino_rechazada": fin.pk,
            },
        )
        self.client.post(reverse("catalogo:studio_bloque_eliminar", args=[self.servicio.pk, revision.pk]))
        self.assertEqual(self._cadena(), ["A", "(sin salida)"])
        self._crear_bloque("ACTIVIDAD", "B")
        self.assertEqual(self._cadena(), ["A", "B", "FIN"])

    # --- Inmutabilidad de ACTIVA / HISTORICA ---

    def test_version_activa_es_inmutable_y_se_edita_via_nuevo_borrador(self):
        from apps.workflows import versionamiento as workflows_versionamiento
        from apps.workflows.models import Etapa, WorkflowVersion

        self._crear_bloque("ACTIVIDAD", "A")
        workflows_versionamiento.activar_version(self.version.workflow, self.version, self.admin)
        self._crear_bloque("ACTIVIDAD", "No debería existir")
        self.assertFalse(Etapa.objects.filter(version=self.version, nombre="No debería existir").exists())
        self.assertNotContains(self._pagina_ejecucion(), 'id="panel-nuevo-ACTIVIDAD"')
        # Editar una ACTIVA = nuevo borrador.
        self.client.post(reverse("catalogo:studio_ejecucion_configurar", args=[self.servicio.pk]))
        borrador = self.servicio.workflow.versiones.get(estado=WorkflowVersion.Estado.BORRADOR)
        self.assertNotEqual(borrador.pk, self.version.pk)
        self.version.refresh_from_db()
        self.assertEqual(self.version.estado, "ACTIVA")
        self.version = borrador
        self._crear_bloque("ACTIVIDAD", "B")
        self.assertEqual(self._cadena(), ["A", "B", "FIN"])
        self.assertEqual(Etapa.objects.filter(version_id=self.version.pk).count() - 2, 2)

    def test_version_historica_es_inmutable(self):
        from apps.workflows import versionamiento as workflows_versionamiento
        from apps.workflows.models import WorkflowVersion

        self._crear_bloque("ACTIVIDAD", "A")
        v1 = self.version
        workflows_versionamiento.activar_version(v1.workflow, v1, self.admin)
        self.client.post(reverse("catalogo:studio_ejecucion_configurar", args=[self.servicio.pk]))
        v2 = self.servicio.workflow.versiones.get(estado=WorkflowVersion.Estado.BORRADOR)
        workflows_versionamiento.activar_version(v2.workflow, v2, self.admin)
        v1.refresh_from_db()
        self.assertEqual(v1.estado, "HISTORICA")
        bloque_historico = v1.etapas.get(nombre="A")
        self.client.post(
            reverse("catalogo:studio_bloque_editar", args=[self.servicio.pk, bloque_historico.pk]),
            {
                f"bloque-{bloque_historico.pk}-editar-nombre": "Cambiada", f"bloque-{bloque_historico.pk}-editar-descripcion": "",
                f"bloque-{bloque_historico.pk}-config-tipo_actor": "SOLICITANTE",
            },
        )
        bloque_historico.refresh_from_db()
        self.assertEqual(bloque_historico.nombre, "A")
        self.client.post(reverse("catalogo:studio_bloque_eliminar", args=[self.servicio.pk, bloque_historico.pk]))
        self.assertTrue(v1.etapas.filter(pk=bloque_historico.pk).exists())

    # --- + Agregar después ---

    def test_agregar_despues_inserta_c_entre_a_y_b(self):
        from apps.workflows.validacion import validar_estructura

        self._crear_bloque("ACTIVIDAD", "A")
        self._crear_bloque("ACTIVIDAD", "B")
        self._crear_bloque("ACTIVIDAD", "C", despues_de=self._bloque("A"))
        self.assertEqual(self._cadena(), ["A", "C", "B", "FIN"])
        self.assertEqual(validar_estructura(self.version), [])

    def test_agregar_despues_aprobacion_no_deja_huerfano_el_siguiente_ni_crea_transiciones_invalidas(self):
        self._crear_bloque("ACTIVIDAD", "A")
        self._crear_bloque("ACTIVIDAD", "B")
        self._crear_bloque("APROBACION", "Revisión", despues_de=self._bloque("A"))
        self.assertEqual(self._cadena(), ["A", "Revisión", "B", "FIN"])
        revision = self._bloque("Revisión")
        for transicion in revision.transiciones_salientes.all():
            self.assertEqual(transicion.resultado_aprobacion, "APROBADA")
            transicion.full_clean()
        self._crear_bloque("DECISION", "¿Sigue?", despues_de=self._bloque("B"))
        decision = self._bloque("¿Sigue?")
        # Su destino anterior era FIN: no recibe transición genérica (se configura después).
        self.assertEqual(decision.transiciones_salientes.count(), 0)
        self.assertEqual(self._cadena(), ["A", "Revisión", "B", "¿Sigue?", "(sin salida)"])

    def test_agregar_despues_en_bifurcacion_no_inserta_nada(self):
        from apps.workflows.models import Etapa

        self._crear_bloque("ACTIVIDAD", "A")
        self._crear_bloque("APROBACION", "Revisión")
        revision = self._bloque("Revisión")
        fin = self.version.etapas.get(tipo="FIN")
        self.client.post(
            reverse("catalogo:studio_ruta_aprobacion_guardar", args=[self.servicio.pk, revision.pk]),
            {
                f"bloque-{revision.pk}-ruta-destino_aprobada": fin.pk,
                f"bloque-{revision.pk}-ruta-destino_rechazada": fin.pk,
            },
        )
        antes = Etapa.objects.filter(version=self.version).count()
        transiciones_antes = sorted(
            revision.transiciones_salientes.values_list("resultado_aprobacion", "etapa_destino_id")
        )
        self._crear_bloque("ACTIVIDAD", "No debería existir", despues_de=revision)
        self.assertEqual(Etapa.objects.filter(version=self.version).count(), antes)
        self.assertEqual(
            sorted(revision.transiciones_salientes.values_list("resultado_aprobacion", "etapa_destino_id")),
            transiciones_antes,
        )
        # La UI no ofrece insertar tras la bifurcación, y un ?despues= manipulado se ignora.
        respuesta = self._pagina_ejecucion(despues=revision.pk)
        self.assertNotContains(respuesta, "Agregar después de «Revisión»")
        self.assertContains(respuesta, "Agregar bloque")

    def test_agregar_despues_de_una_decision_tampoco_adivina(self):
        from apps.workflows.models import Etapa

        self._crear_bloque("DECISION", "¿Procede?")
        decision = self._bloque("¿Procede?")
        antes = Etapa.objects.filter(version=self.version).count()
        self._crear_bloque("ACTIVIDAD", "No debería existir", despues_de=decision)
        self.assertEqual(Etapa.objects.filter(version=self.version).count(), antes)

    def test_interfaz_ofrece_agregar_despues_solo_donde_es_seguro(self):
        self._crear_bloque("ACTIVIDAD", "A")
        self._crear_bloque("APROBACION", "Revisión")
        respuesta = self._pagina_ejecucion()
        self.assertContains(respuesta, f"despues={self._bloque('A').pk}")
        self.assertNotContains(respuesta, f"despues={self._bloque('Revisión').pk}")
        respuesta = self._pagina_ejecucion(despues=self._bloque("A").pk)
        self.assertContains(respuesta, "Agregar después de «A»")
        self.assertContains(respuesta, 'name="despues_de"')

    def test_despues_de_un_bloque_de_otra_version_se_rechaza(self):
        from apps.workflows.models import Etapa

        otro = Servicio.objects.create(nombre="Otro flujo", categoria=self.categoria, activo=False)
        self.client.post(reverse("catalogo:studio_ejecucion_configurar", args=[otro.pk]))
        otro.refresh_from_db()
        version_otra = otro.workflow.versiones.get()
        self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[otro.pk]),
            {"nuevo-tipo": "ACTIVIDAD", "nuevo-nombre": "Ajena", "nuevo-descripcion": "", "nuevo-config-tipo_actor": "SOLICITANTE"},
        )
        ajena = Etapa.objects.get(version=version_otra, nombre="Ajena")
        antes = Etapa.objects.filter(version=self.version).count()
        self._crear_bloque("ACTIVIDAD", "Intruso", despues_de=ajena)
        self.assertEqual(Etapa.objects.filter(version=self.version).count(), antes)

    # --- Sin adivinar cuando el final es ambiguo ---

    def test_final_ambiguo_agrega_sin_conectar_y_sigue_editable(self):
        from apps.workflows.models import TransicionEtapa

        self._crear_bloque("ACTIVIDAD", "A")
        self._crear_bloque("APROBACION", "Revisión")
        revision, a = self._bloque("Revisión"), self._bloque("A")
        fin = self.version.etapas.get(tipo="FIN")
        # La ruta principal (aprobada) vuelve a A: ciclo, no hay "final" único.
        self.client.post(
            reverse("catalogo:studio_ruta_aprobacion_guardar", args=[self.servicio.pk, revision.pk]),
            {
                f"bloque-{revision.pk}-ruta-destino_aprobada": a.pk,
                f"bloque-{revision.pk}-ruta-destino_rechazada": fin.pk,
            },
        )
        self._crear_bloque("ACTIVIDAD", "Suelta")
        suelta = self._bloque("Suelta")
        self.assertFalse(
            TransicionEtapa.objects.filter(etapa_origen=suelta).exists()
            or TransicionEtapa.objects.filter(etapa_destino=suelta).exists()
        )
        self._assert_formularios_de_alta_visibles()


# ---------------------------------------------------------------------------
# Sprint 4.5 — Política de entrega configurada desde Studio (Salida).
# ---------------------------------------------------------------------------


class StudioPoliticaEntregaTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user("politica_admin", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.admin, "catalogo.administrar")
        self.sin_permiso = Usuario.objects.create_user("politica_sin_permiso", password=CLAVE_PRUEBA)
        self.categoria = Categoria.objects.create(nombre="Entrega")
        self.servicio = Servicio.objects.create(nombre="Con entrega", categoria=self.categoria, activo=False)

    def _guardar(self, **datos):
        return self.client.post(reverse("catalogo:studio_entrega_guardar", args=[self.servicio.pk]), datos)

    def _auditorias(self):
        return RegistroAuditoria.objects.filter(modelo="catalogo.servicio", object_id=self.servicio.pk)

    def test_servicio_nuevo_no_tiene_politica_y_salida_la_ofrece(self):
        self.assertEqual((self.servicio.politica_entrega, self.servicio.dias_observacion), ("", None))
        self.client.login(username="politica_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "salida"})
        self.assertContains(respuesta, "Entrega al solicitante")
        self.assertContains(respuesta, "Guardar política")
        self.assertContains(respuesta, "sin entrega formal")

    def test_configurar_periodo_de_observaciones(self):
        self.client.login(username="politica_admin", password=CLAVE_PRUEBA)
        self._guardar(politica="PERIODO_OBSERVACIONES", dias_observacion="5")
        self.servicio.refresh_from_db()
        self.assertEqual((self.servicio.politica_entrega, self.servicio.dias_observacion), ("PERIODO_OBSERVACIONES", 5))
        evento = self._auditorias().get(accion=RegistroAuditoria.Accion.ACTUALIZAR)
        self.assertEqual(evento.datos_anteriores, {"politica_entrega": "", "dias_observacion": None})
        self.assertEqual(evento.datos_nuevos, {"politica_entrega": "PERIODO_OBSERVACIONES", "dias_observacion": 5})
        self.assertEqual(evento.usuario, self.admin)

    def test_cierre_directo_descarta_los_dias(self):
        self.client.login(username="politica_admin", password=CLAVE_PRUEBA)
        self._guardar(politica="PERIODO_OBSERVACIONES", dias_observacion="5")
        self._guardar(politica="CIERRE_DIRECTO", dias_observacion="9")
        self.servicio.refresh_from_db()
        self.assertEqual((self.servicio.politica_entrega, self.servicio.dias_observacion), ("CIERRE_DIRECTO", None))

    def test_rechaza_periodo_sin_dias_o_fuera_de_rango_y_politicas_invalidas(self):
        self.client.login(username="politica_admin", password=CLAVE_PRUEBA)
        for datos in (
            {"politica": "PERIODO_OBSERVACIONES"}, {"politica": "PERIODO_OBSERVACIONES", "dias_observacion": "0"},
            {"politica": "PERIODO_OBSERVACIONES", "dias_observacion": "91"}, {"politica": "ESPERAR_SIEMPRE"},
            {"politica": ""},
        ):
            with self.subTest(datos=datos):
                self._guardar(**datos)
        self.servicio.refresh_from_db()
        self.assertEqual((self.servicio.politica_entrega, self.servicio.dias_observacion), ("", None))
        self.assertFalse(self._auditorias().filter(accion=RegistroAuditoria.Accion.ACTUALIZAR).exists())

    def test_guardar_la_misma_politica_no_duplica_auditoria(self):
        self.client.login(username="politica_admin", password=CLAVE_PRUEBA)
        self._guardar(politica="PERIODO_OBSERVACIONES", dias_observacion="3")
        self._guardar(politica="PERIODO_OBSERVACIONES", dias_observacion="3")
        self.assertEqual(self._auditorias().filter(accion=RegistroAuditoria.Accion.ACTUALIZAR).count(), 1)

    def test_sin_permiso_no_configura(self):
        self.client.login(username="politica_sin_permiso", password=CLAVE_PRUEBA)
        self.assertEqual(self._guardar(politica="CIERRE_DIRECTO").status_code, 403)
        self.servicio.refresh_from_db()
        self.assertEqual(self.servicio.politica_entrega, "")

    def test_operacion_exige_permiso_y_la_base_rechaza_estados_incoherentes(self):
        from django.core.exceptions import PermissionDenied

        from apps.catalogo.operaciones import configurar_politica_entrega

        with self.assertRaises(PermissionDenied):
            configurar_politica_entrega(self.servicio, self.sin_permiso, politica="CIERRE_DIRECTO")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Servicio.objects.filter(pk=self.servicio.pk).update(politica_entrega="PERIODO_OBSERVACIONES")

    def test_publicacion_informa_pero_no_exige_politica(self):
        self.client.login(username="politica_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "publicacion"})
        self.assertContains(respuesta, "Sin política de entrega")
        self._guardar(politica="CIERRE_DIRECTO")
        respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "publicacion"})
        self.assertContains(respuesta, "Entrega al solicitante definida")

    def test_las_cinco_pestanas_siguen_respondiendo(self):
        self.client.login(username="politica_admin", password=CLAVE_PRUEBA)
        for tab in ("general", "entrada", "ejecucion", "salida", "publicacion"):
            respuesta = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": tab})
            self.assertEqual(respuesta.status_code, 200, tab)


class StudioWorkflowCompartidoTests(TestCase):
    """4.6 — Un Workflow puede ser reutilizado por varios Servicios/Procesos.

    Studio permite crear ejecución propia o elegir una existente (compartida);
    si es compartida se advierte con quién; el usuario puede cancelar, crear
    copia (desvincula solo a ese elemento) o modificar el compartido (con
    confirmación reforzada antes de publicar). Vincular y modificar son
    permisos independientes. Compartir NO comparte `InstanciaWorkflow` y los
    Tickets iniciados conservan su `WorkflowVersion`."""

    def setUp(self):
        from apps.workflows import versionamiento
        from apps.workflows.models import WorkflowVersion

        self.admin = Usuario.objects.create_user("compartido_admin", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.admin, "catalogo.administrar", nombre_rol="Rol cat admin comp")
        _otorgar_permiso(self.admin, "workflows.administrar", nombre_rol="Rol wf admin comp")
        self.categoria = Categoria.objects.create(nombre="Compartidos")
        self.client.login(username="compartido_admin", password=CLAVE_PRUEBA)

        # Servicio "origen": su flujo (A → B → FIN) se publica y es el compartible.
        self.origen = Servicio.objects.create(nombre="Servicio origen", categoria=self.categoria, activo=False)
        self.client.post(reverse("catalogo:studio_ejecucion_configurar", args=[self.origen.pk]))
        self.origen.refresh_from_db()
        for nombre in ("Diseñar pieza", "Revisar pieza"):
            self._crear_actividad(self.origen, nombre)
        version = self.origen.workflow.versiones.get(estado=WorkflowVersion.Estado.BORRADOR)
        versionamiento.activar_version(self.origen.workflow, version, self.admin)
        self.workflow = self.origen.workflow

        self.destino = Servicio.objects.create(nombre="Servicio destino", categoria=self.categoria, activo=False)

    # --- helpers ---

    def _usuario_con(self, username, *codigos):
        usuario = Usuario.objects.create_user(username, password=CLAVE_PRUEBA)
        for codigo in codigos:
            _otorgar_permiso(usuario, codigo, nombre_rol=f"Rol {codigo} {username}")
        return usuario

    def _crear_actividad(self, servicio, nombre):
        return self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[servicio.pk]),
            {"nuevo-tipo": "ACTIVIDAD", "nuevo-nombre": nombre, "nuevo-descripcion": "",
             "nuevo-config-tipo_actor": "SOLICITANTE"},
        )

    def _pagina(self, servicio=None, tab="ejecucion", **params):
        params["tab"] = tab
        return self.client.get(reverse("catalogo:studio", args=[(servicio or self.destino).pk]), params)

    def _vincular(self, servicio=None, workflow_id=None):
        return self.client.post(
            reverse("catalogo:studio_ejecucion_vincular", args=[(servicio or self.destino).pk]),
            {"plantilla": workflow_id if workflow_id is not None else self.workflow.pk},
        )

    def _copia(self, servicio=None):
        return self.client.post(reverse("catalogo:studio_ejecucion_copia", args=[(servicio or self.destino).pk]))

    def _configurar(self, servicio=None, confirmo=False):
        datos = {"confirmo_compartido": "1"} if confirmo else {}
        return self.client.post(
            reverse("catalogo:studio_ejecucion_configurar", args=[(servicio or self.destino).pk]), datos
        )

    def _publicar(self, servicio=None, **datos):
        return self.client.post(reverse("catalogo:studio_publicar", args=[(servicio or self.destino).pk]), datos)

    def _nombres_etapas(self, version):
        return sorted(version.etapas.values_list("nombre", flat=True))

    def _destino_vinculado(self):
        self._vincular()
        self.destino.refresh_from_db()
        return self.destino

    # --- 2. Crear propia o elegir existente (con vista previa) ---

    def test_sin_ejecucion_se_ofrecen_las_dos_opciones(self):
        respuesta = self._pagina()
        self.assertContains(respuesta, "Empezar desde cero")
        self.assertContains(respuesta, "Usar un flujo existente")
        self.assertContains(respuesta, "Servicio origen")

    def test_solo_se_ofrecen_flujos_con_version_activa(self):
        solo_borrador = Servicio.objects.create(nombre="Solo borrador", categoria=self.categoria, activo=False)
        self.client.post(reverse("catalogo:studio_ejecucion_configurar", args=[solo_borrador.pk]))
        solo_borrador.refresh_from_db()
        ofrecidos = [p.pk for p in self._pagina().context["plantillas"]]
        self.assertIn(self.workflow.pk, ofrecidos)
        self.assertNotIn(solo_borrador.workflow_id, ofrecidos)

    def test_vista_previa_muestra_las_etapas_y_no_vincula_nada(self):
        respuesta = self._pagina(plantilla=self.workflow.pk)
        self.assertContains(respuesta, "Etapas de «")
        self.assertContains(respuesta, "Diseñar pieza")
        self.assertContains(respuesta, "Revisar pieza")
        self.assertContains(respuesta, "Usar este flujo")
        self.destino.refresh_from_db()
        self.assertIsNone(self.destino.workflow_id)

    def test_plantilla_inexistente_en_la_vista_previa_se_ignora(self):
        respuesta = self._pagina(plantilla="999999")
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotContains(respuesta, "Usar este flujo")

    def test_empezar_desde_cero_sigue_creando_un_workflow_propio(self):
        self._configurar()
        self.destino.refresh_from_db()
        self.assertIsNotNone(self.destino.workflow_id)
        self.assertNotEqual(self.destino.workflow_id, self.workflow.pk)

    # --- 1. Reutilización: el Workflow se comparte ---

    def test_vincular_comparte_el_mismo_workflow_sin_duplicarlo(self):
        from apps.workflows.models import Workflow

        total = Workflow.objects.count()
        destino = self._destino_vinculado()
        self.assertEqual(destino.workflow_id, self.workflow.pk)
        self.assertEqual(Workflow.objects.count(), total)
        self.assertEqual(self.workflow.servicios.count(), 2)

    def test_vincular_audita_el_vinculo(self):
        self._destino_vinculado()
        registro = (
            RegistroAuditoria.objects.filter(modelo="catalogo.servicio", object_id=self.destino.pk)
            .order_by("-pk").first()
        )
        self.assertEqual(registro.datos_nuevos["workflow_id"], self.workflow.pk)
        self.assertEqual(registro.datos_nuevos["vinculo"], "EXISTENTE")
        self.assertEqual(registro.datos_nuevos["compartido_con"], [self.origen.pk])

    def test_no_permite_vincular_dos_veces_ni_un_flujo_sin_version_activa(self):
        from apps.catalogo.ejecucion import vincular_ejecucion
        from apps.workflows import versionamiento

        self._vincular()
        otro = Servicio.objects.create(nombre="Otro destino", categoria=self.categoria, activo=False)
        sin_activa = versionamiento.crear_workflow(self.admin, nombre="Nunca publicado")
        with self.assertRaises(ValidationError):
            vincular_ejecucion(otro, self.admin, sin_activa)
        otro.refresh_from_db()
        self.assertIsNone(otro.workflow_id)
        self._vincular(workflow_id=sin_activa.pk)  # ya vinculado: se rechaza sin romper
        self.destino.refresh_from_db()
        self.assertEqual(self.destino.workflow_id, self.workflow.pk)

    def test_entrada_invalida_al_vincular_no_rompe(self):
        for valor in ("abc", "", "999999"):
            self.assertEqual(self._vincular(workflow_id=valor).status_code, 302, valor)
        self.destino.refresh_from_db()
        self.assertIsNone(self.destino.workflow_id)

    # --- 5. Advertencia de servicios vinculados ---

    def test_flujo_compartido_advierte_con_quien_se_comparte(self):
        self._destino_vinculado()
        respuesta = self._pagina()
        self.assertContains(respuesta, "Este flujo se comparte con otros elementos")
        self.assertContains(respuesta, "Servicio origen")
        self.assertEqual([s.pk for s in respuesta.context["compartido_con"]], [self.origen.pk])

    def test_flujo_propio_no_muestra_advertencia(self):
        respuesta = self._pagina(self.origen)
        self.assertNotContains(respuesta, "Este flujo se comparte")
        self.assertEqual(respuesta.context["compartido_con"], [])

    def test_workflows_avanzados_muestra_quienes_lo_usan(self):
        self._destino_vinculado()
        respuesta = self.client.get(reverse("workflows:detalle", args=[self.workflow.pk]))
        self.assertContains(respuesta, "Usado por")
        self.assertContains(respuesta, "Servicio destino")

    # --- 6/8. Modificar compartido: confirmación antes de abrir borrador y de publicar ---

    def test_modificar_compartido_exige_confirmacion_para_abrir_borrador(self):
        from apps.workflows.models import WorkflowVersion

        self._destino_vinculado()
        self._configurar(confirmo=False)
        self.assertFalse(self.workflow.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).exists())
        self._configurar(confirmo=True)
        self.assertTrue(self.workflow.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).exists())

    def test_modificar_un_flujo_propio_no_pide_confirmacion(self):
        from apps.workflows.models import WorkflowVersion

        self._configurar(self.origen, confirmo=False)
        self.assertTrue(self.workflow.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).exists())

    def test_publicar_un_flujo_compartido_exige_confirmacion_reforzada(self):
        from unittest import mock

        from apps.workflows.models import WorkflowVersion

        self._destino_vinculado()
        self._configurar(confirmo=True)
        self.workflow.refresh_from_db()
        activa = self.workflow.version_activa_id
        with mock.patch("apps.catalogo.ejecucion.activar_servicio", side_effect=lambda s, a: s):
            # Sin casilla / sin nombre / nombre incorrecto: no se publica.
            self._publicar()
            self._publicar(confirmo_compartido="1")
            self._publicar(confirmo_compartido="1", confirmacion_nombre="otro nombre")
            self.workflow.refresh_from_db()
            self.assertEqual(self.workflow.version_activa_id, activa)
            self.assertTrue(self.workflow.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).exists())
            # Casilla + nombre exacto del flujo: se publica.
            self._publicar(confirmo_compartido="1", confirmacion_nombre=self.workflow.nombre)
            self.workflow.refresh_from_db()
            self.assertNotEqual(self.workflow.version_activa_id, activa)

    def test_el_dominio_rechaza_publicar_compartido_sin_confirmar(self):
        from apps.catalogo.ejecucion import publicar_ejecucion

        self._destino_vinculado()
        self._configurar(confirmo=True)
        version = self.workflow.versiones.get(estado="BORRADOR")
        with self.assertRaises(ValidationError):
            publicar_ejecucion(self.destino, version, self.admin)

    def test_la_pagina_de_publicacion_pide_confirmacion_solo_si_hay_impacto(self):
        self._destino_vinculado()
        self._configurar(confirmo=True)
        self.assertContains(self._pagina(tab="publicacion"), "Esta publicación afecta a otros elementos")
        # Servicio con flujo propio: sin confirmación reforzada.
        solo = Servicio.objects.create(nombre="Solo propio", categoria=self.categoria, activo=False)
        self._configurar(solo)
        self.assertNotContains(self._pagina(solo, tab="publicacion"), "Esta publicación afecta a otros elementos")

    # --- 6/7. Crear copia: desvincula únicamente al elemento que inició la acción ---

    def test_crear_copia_desvincula_solo_a_este_elemento(self):
        self._destino_vinculado()
        self._copia()
        self.destino.refresh_from_db()
        self.origen.refresh_from_db()
        self.assertNotEqual(self.destino.workflow_id, self.workflow.pk)
        self.assertEqual(self.origen.workflow_id, self.workflow.pk)
        self.assertEqual(list(self.workflow.servicios.values_list("pk", flat=True)), [self.origen.pk])

    def test_la_copia_tiene_la_misma_estructura_ya_activa_y_el_original_no_cambia(self):
        from apps.workflows.models import WorkflowVersion

        self._destino_vinculado()
        activa_original = self.workflow.version_activa_id
        self._copia()
        self.destino.refresh_from_db()
        copia = self.destino.workflow
        self.assertEqual(copia.version_activa.estado, WorkflowVersion.Estado.ACTIVA)
        self.assertEqual(
            self._nombres_etapas(copia.version_activa), self._nombres_etapas(self.workflow.version_activa)
        )
        self.assertFalse(copia.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).exists())
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.version_activa_id, activa_original)
        self.assertEqual(self.workflow.versiones.count(), 1)

    def test_editar_la_copia_no_toca_el_original(self):
        self._destino_vinculado()
        self._copia()
        self._configurar()  # flujo ya propio: sin confirmación
        self._crear_actividad(self.destino, "Paso propio")
        self.destino.refresh_from_db()
        propio = self.destino.workflow.versiones.get(estado="BORRADOR")
        self.assertIn("Paso propio", self._nombres_etapas(propio))
        self.assertNotIn("Paso propio", self._nombres_etapas(self.workflow.version_activa))
        self.assertEqual(self.workflow.versiones.count(), 1)

    def test_la_copia_audita_su_origen(self):
        self._destino_vinculado()
        self._copia()
        registro = (
            RegistroAuditoria.objects.filter(modelo="catalogo.servicio", object_id=self.destino.pk)
            .order_by("-pk").first()
        )
        self.assertEqual(registro.datos_nuevos["vinculo"], "COPIA")
        self.assertEqual(registro.datos_nuevos["copiado_de_workflow_id"], self.workflow.pk)

    def test_no_hay_copia_si_el_flujo_no_se_comparte(self):
        self._copia(self.origen)
        self.origen.refresh_from_db()
        self.assertEqual(self.origen.workflow_id, self.workflow.pk)

    # --- 9/10/12. Permisos independientes, sin asignación automática ---

    def test_vincular_no_concede_modificar(self):
        self._usuario_con("solo_vincula", "catalogo.administrar", "workflows.vincular")
        self.client.login(username="solo_vincula", password=CLAVE_PRUEBA)
        # Puede elegir un flujo existente...
        self.assertContains(self._pagina(), "Usar un flujo existente")
        self.assertEqual(self._vincular().status_code, 302)
        self.destino.refresh_from_db()
        self.assertEqual(self.destino.workflow_id, self.workflow.pk)
        # ...pero no modificarlo, copiarlo ni crear uno propio.
        self.assertEqual(self._configurar(confirmo=True).status_code, 403)
        self.assertEqual(self._copia().status_code, 403)
        otro = Servicio.objects.create(nombre="Otro", categoria=self.categoria, activo=False)
        self.assertEqual(self._configurar(otro).status_code, 403)
        respuesta = self._pagina()
        self.assertNotContains(respuesta, "Editar ejecución")
        self.assertNotContains(respuesta, "Crear copia propia")
        self.assertContains(respuesta, "No tienes autorización para modificar flujos")
        self.assertFalse(self.workflow.versiones.filter(estado="BORRADOR").exists())

    def test_administrar_workflows_implica_poder_vincular(self):
        self.assertEqual(self._vincular().status_code, 302)
        self.destino.refresh_from_db()
        self.assertEqual(self.destino.workflow_id, self.workflow.pk)

    def test_sin_permiso_de_vincular_no_se_ve_ni_se_usa(self):
        self._usuario_con("solo_catalogo_comp", "catalogo.administrar")
        self.client.login(username="solo_catalogo_comp", password=CLAVE_PRUEBA)
        self.assertNotContains(self._pagina(), "Usar un flujo existente")
        self.assertEqual(self._vincular().status_code, 403)
        self.destino.refresh_from_db()
        self.assertIsNone(self.destino.workflow_id)

    def test_solo_workflows_vincular_sin_permiso_de_catalogo_no_entra_a_studio(self):
        self._usuario_con("solo_wf_vincular", "workflows.vincular")
        self.client.login(username="solo_wf_vincular", password=CLAVE_PRUEBA)
        self.assertEqual(self._pagina().status_code, 403)
        self.assertEqual(self._vincular().status_code, 403)

    def test_el_permiso_nuevo_existe_pero_no_esta_asignado_a_nadie(self):
        from apps.core.models import AsignacionRol, Permiso, RolPermiso

        permiso = Permiso.objects.get(codigo="workflows.vincular")
        self.assertFalse(RolPermiso.objects.filter(permiso=permiso).exists())
        self.assertFalse(AsignacionRol.objects.filter(rol__rolpermiso__permiso=permiso).exists())

    def test_poder_vincular_no_abre_la_vista_tecnica_de_flujos(self):
        # Quien configura servicios y puede vincular entra al Diseñador, pero la
        # vista técnica de los flujos exige poder consultarlos.
        self._usuario_con("vincula_sin_avanzado", "catalogo.administrar", "workflows.vincular")
        self.client.login(username="vincula_sin_avanzado", password=CLAVE_PRUEBA)
        self.assertContains(self.client.get(reverse("core:inicio")), "Diseñador")
        self.assertEqual(self.client.get(reverse("workflows:lista")).status_code, 403)

    # --- 3/4/13. Instancias independientes y versión conservada (Memento ya existente) ---

    def test_compartir_no_comparte_instancias_y_los_tickets_conservan_su_version(self):
        from apps.workflows.models import Etapa, InstanciaWorkflow, WorkflowVersion
        from apps.workflows.motor import iniciar_workflow
        from apps.workflows.tests import _crear_workflow_lineal_activo
        from apps.workflows.versionamiento import activar_version, crear_nueva_version

        workflow, _etapas = _crear_workflow_lineal_activo(self.admin)
        a = Servicio.objects.create(nombre="Comparte A", categoria=self.categoria, activo=False, workflow=workflow)
        b = Servicio.objects.create(nombre="Comparte B", categoria=self.categoria, activo=False, workflow=workflow)
        version1 = workflow.version_activa

        # Un Ticket de cada servicio: instancias distintas sobre la misma versión.
        instancia_a = iniciar_workflow(a.workflow, actor=self.admin)
        instancia_b = iniciar_workflow(b.workflow, actor=self.admin)
        self.assertNotEqual(instancia_a.pk, instancia_b.pk)
        self.assertEqual(instancia_a.workflow_version_id, version1.pk)
        self.assertEqual(instancia_b.workflow_version_id, version1.pk)

        # Se modifica y publica el flujo compartido (v2, con un bloque renombrado).
        version2 = crear_nueva_version(workflow, actor=self.admin)
        Etapa.objects.filter(version=version2, tipo=Etapa.Tipo.HITO).update(nombre="Hito nuevo en v2")
        activar_version(workflow, version2, actor=self.admin)

        # Los tickets ya iniciados conservan su versión (ahora histórica) intacta.
        for instancia in (instancia_a, instancia_b):
            instancia.refresh_from_db()
            self.assertEqual(instancia.workflow_version_id, version1.pk)
        version1.refresh_from_db()
        self.assertEqual(version1.estado, WorkflowVersion.Estado.HISTORICA)
        self.assertNotIn("Hito nuevo en v2", self._nombres_etapas(version1))
        with self.assertRaises(ValidationError):
            version1.exigir_editable()

        # Los tickets nuevos de cualquiera de los dos servicios usan v2.
        workflow.refresh_from_db()
        nueva = iniciar_workflow(workflow, actor=self.admin)
        self.assertEqual(nueva.workflow_version_id, version2.pk)
        self.assertEqual(InstanciaWorkflow.objects.filter(workflow_version=version1).count(), 2)


# ---------------------------------------------------------------------------
# Fase D2 — Diseñador › Flujos: un Flujo (Workflow real de la biblioteca) se
# crea, se diseña, se versiona, se publica y se copia SIN pasar por un
# Servicio. Mismas reglas del motor y de Studio; solo cambia el ancla.
# ---------------------------------------------------------------------------


def _permiso_get_or_create(usuario, codigo, nombre_rol):
    from apps.core.models import AsignacionRol, Permiso, RolFuncional, RolPermiso

    permiso, _ = Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": codigo})
    rol = RolFuncional.objects.create(nombre=nombre_rol)
    RolPermiso.objects.create(rol=rol, permiso=permiso)
    return AsignacionRol.objects.create(usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL)


class DisenadorFlujosTests(TestCase):
    def setUp(self):
        from apps.workflows.models import Etapa, TransicionEtapa, Workflow, WorkflowVersion

        self.Etapa, self.Transicion, self.Workflow, self.Version = Etapa, TransicionEtapa, Workflow, WorkflowVersion
        # Diseña flujos SIN poder configurar el catálogo: la independencia es el punto.
        self.diseñador = Usuario.objects.create_user("d2_disenador", password=CLAVE_PRUEBA)
        _permiso_get_or_create(self.diseñador, "workflows.administrar", "Rol D2 administrar flujos")
        self.lector = Usuario.objects.create_user("d2_lector", password=CLAVE_PRUEBA)
        _permiso_get_or_create(self.lector, "workflows.consultar", "Rol D2 consultar flujos")
        self.categoria = Categoria.objects.create(nombre="D2")
        self.client.login(username="d2_disenador", password=CLAVE_PRUEBA)

    # --- helpers ---

    def _crear_flujo(self, nombre="Flujo de prueba"):
        respuesta = self.client.post(reverse("flujos:nuevo"), {"nombre": nombre, "descripcion": "Descripción"})
        self.assertEqual(respuesta.status_code, 302, respuesta.content[:300])
        return self.Workflow.objects.get(nombre=nombre)

    def _bloque(self, workflow, nombre, tipo="ACTIVIDAD"):
        datos = {"nuevo-tipo": tipo, "nuevo-nombre": nombre, "nuevo-descripcion": ""}
        if tipo == "ACTIVIDAD":
            datos["nuevo-config-tipo_actor"] = "SOLICITANTE"
        return self.client.post(reverse("flujos:bloque_crear", args=[workflow.pk]), datos)

    def _flujo_publicado(self, nombre="Flujo publicado", bloques=("Diseñar pieza", "Revisar pieza")):
        workflow = self._crear_flujo(nombre)
        for bloque in bloques:
            self._bloque(workflow, bloque)
        self.client.post(reverse("flujos:publicar", args=[workflow.pk]))
        workflow.refresh_from_db()
        self.assertIsNotNone(workflow.version_activa_id, "el flujo debería haberse publicado")
        return workflow

    def _servicio_usando(self, workflow, nombre="Servicio vinculado"):
        return Servicio.objects.create(nombre=nombre, categoria=self.categoria, activo=False, workflow=workflow)

    def _versiones(self, workflow):
        return {v.numero: v.estado for v in workflow.versiones.all()}

    # --- crear ---

    def test_crear_un_flujo_no_necesita_servicio_ni_permiso_de_catalogo(self):
        servicios_antes = Servicio.objects.count()
        workflow = self._crear_flujo("Atención general")
        self.assertEqual(self._versiones(workflow), {1: self.Version.Estado.BORRADOR})
        version = workflow.versiones.get()
        self.assertEqual(
            sorted(version.etapas.values_list("tipo", flat=True)),
            ["FIN", "INICIO"],
        )
        self.assertEqual(self.Transicion.objects.filter(etapa_origen__version=version).count(), 1)
        self.assertEqual(Servicio.objects.count(), servicios_antes)  # nada se creó ni se vinculó

    def test_el_nuevo_flujo_se_abre_en_su_lienzo(self):
        respuesta = self.client.post(reverse("flujos:nuevo"), {"nombre": "Con lienzo", "descripcion": ""})
        workflow = self.Workflow.objects.get(nombre="Con lienzo")
        self.assertRedirects(respuesta, reverse("flujos:lienzo", args=[workflow.pk]))

    def test_un_flujo_necesita_nombre(self):
        respuesta = self.client.post(reverse("flujos:nuevo"), {"nombre": "   ", "descripcion": ""})
        self.assertEqual(respuesta.status_code, 200)
        self.assertFalse(self.Workflow.objects.exists())

    def test_crear_exige_poder_administrar_flujos(self):
        self.client.login(username="d2_lector", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.get(reverse("flujos:nuevo")).status_code, 403)
        self.assertEqual(self.client.post(reverse("flujos:nuevo"), {"nombre": "No"}).status_code, 403)
        self.assertFalse(self.Workflow.objects.exists())

    def test_la_operacion_de_dominio_tambien_exige_el_permiso(self):
        from django.core.exceptions import PermissionDenied

        from apps.catalogo.ejecucion import crear_flujo

        sin_permiso = Usuario.objects.create_user("d2_nadie", password=CLAVE_PRUEBA)
        with self.assertRaises(PermissionDenied):
            crear_flujo(sin_permiso, nombre="No debería")

    # --- lienzo y permisos de lectura ---

    def test_ver_el_lienzo_exige_consultar_y_no_basta_con_vincular(self):
        workflow = self._crear_flujo()
        self.assertEqual(self.client.get(reverse("flujos:lienzo", args=[workflow.pk])).status_code, 200)
        self.client.login(username="d2_lector", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.get(reverse("flujos:lienzo", args=[workflow.pk])).status_code, 200)
        vincula = Usuario.objects.create_user("d2_vincula", password=CLAVE_PRUEBA)
        _permiso_get_or_create(vincula, "workflows.vincular", "Rol D2 vincular")
        self.client.login(username="d2_vincula", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.get(reverse("flujos:lienzo", args=[workflow.pk])).status_code, 403)

    def test_quien_solo_consulta_no_ve_ni_puede_usar_las_acciones_de_edicion(self):
        workflow = self._crear_flujo()
        self._bloque(workflow, "Paso uno")
        self.client.login(username="d2_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("flujos:lienzo", args=[workflow.pk]))
        self.assertContains(respuesta, "Paso uno")
        self.assertFalse(respuesta.context["editable_ejecucion"])
        for nombre in ("publicar", "preparar", "copia", "datos"):
            self.assertNotContains(respuesta, reverse(f"flujos:{nombre}", args=[workflow.pk]), msg_prefix=nombre)
            self.assertEqual(self.client.post(reverse(f"flujos:{nombre}", args=[workflow.pk])).status_code, 403, nombre)
        self.assertEqual(self._bloque(workflow, "No debería").status_code, 403)

    def test_el_diseñador_resalta_su_navegacion_en_el_lienzo(self):
        workflow = self._crear_flujo()
        respuesta = self.client.get(reverse("flujos:lienzo", args=[workflow.pk]))
        self.assertEqual(respuesta.context["nav_item_activo"], "core:disenador")

    def test_workspace_visual_muestra_fases_conectores_e_inspector(self):
        from apps.workflows.models import FaseWorkflow

        workflow = self._crear_flujo("Flujo visual")
        for nombre in ("Planeacion", "Proceso"):
            self.client.post(
                reverse("flujos:fase_crear", args=[workflow.pk]),
                {"fase-nueva-nombre": nombre, "fase-nueva-descripcion": "", "fase-nueva-orden": ""},
            )
        planeacion, proceso = FaseWorkflow.objects.filter(version__workflow=workflow).order_by("orden")
        self.client.post(
            reverse("flujos:fase_conectar", args=[workflow.pk, planeacion.pk]),
            {
                f"fase-{planeacion.pk}-transicion-fase_destino": proceso.pk,
                f"fase-{planeacion.pk}-transicion-nombre": "",
                f"fase-{planeacion.pk}-transicion-prioridad": "0",
            },
        )

        respuesta = self.client.get(reverse("flujos:lienzo", args=[workflow.pk]))
        self.assertContains(respuesta, "Canvas de fases")
        self.assertContains(respuesta, "Recorrido de fases")
        self.assertContains(respuesta, "Detalles")
        self.assertContains(respuesta, "Nueva fase")
        self.assertContains(respuesta, "Planeacion")
        self.assertContains(respuesta, "Proceso")
        self.assertContains(respuesta, "Conectar fase")
        self.assertContains(respuesta, "Versiones")
        self.assertContains(respuesta, "Usado en")

    # --- diseñar bloques (mismos manejadores que Studio) ---

    def test_los_bloques_se_agregan_conectados_y_sin_servicio(self):
        workflow = self._crear_flujo()
        self._bloque(workflow, "Diseñar pieza")
        self._bloque(workflow, "Revisar pieza")
        respuesta = self.client.get(reverse("flujos:lienzo", args=[workflow.pk]))
        self.assertEqual([f["etapa"].nombre for f in respuesta.context["bloques"]], ["Diseñar pieza", "Revisar pieza"])
        # INICIO → Diseñar → Revisar → FIN
        self.assertEqual(self.Transicion.objects.filter(etapa_origen__version__workflow=workflow).count(), 3)

    def test_eliminar_un_bloque_lineal_deja_el_flujo_continuo(self):
        workflow = self._crear_flujo()
        self._bloque(workflow, "A")
        self._bloque(workflow, "B")
        a = self.Etapa.objects.get(version__workflow=workflow, nombre="A")
        self.client.post(reverse("flujos:bloque_eliminar", args=[workflow.pk, a.pk]))
        respuesta = self.client.get(reverse("flujos:lienzo", args=[workflow.pk]))
        self.assertEqual([f["etapa"].nombre for f in respuesta.context["bloques"]], ["B"])
        self.assertEqual(self.Transicion.objects.filter(etapa_origen__version__workflow=workflow).count(), 2)

    def test_editar_un_bloque_actualiza_su_nombre(self):
        workflow = self._crear_flujo()
        self._bloque(workflow, "Original")
        bloque = self.Etapa.objects.get(version__workflow=workflow, nombre="Original")
        self.client.post(
            reverse("flujos:bloque_editar", args=[workflow.pk, bloque.pk]),
            {f"bloque-{bloque.pk}-editar-nombre": "Renombrado", f"bloque-{bloque.pk}-editar-descripcion": "",
             f"bloque-{bloque.pk}-config-tipo_actor": "SOLICITANTE"},
        )
        bloque.refresh_from_db()
        self.assertEqual(bloque.nombre, "Renombrado")

    def test_un_bloque_de_otro_flujo_no_se_puede_tocar_desde_este(self):
        uno = self._crear_flujo("Uno")
        otro = self._crear_flujo("Otro")
        self._bloque(otro, "Ajeno")
        ajeno = self.Etapa.objects.get(version__workflow=otro, nombre="Ajeno")
        self.client.post(reverse("flujos:bloque_eliminar", args=[uno.pk, ajeno.pk]))
        self.assertTrue(self.Etapa.objects.filter(pk=ajeno.pk).exists())

    def test_una_version_publicada_no_se_edita(self):
        from django.core.exceptions import ValidationError

        from apps.catalogo.ejecucion import agregar_bloque_en_flujo

        workflow = self._flujo_publicado()
        with self.assertRaises(ValidationError):
            agregar_bloque_en_flujo(
                workflow, workflow.version_activa, self.diseñador, tipo="ACTIVIDAD", nombre="Tarde",
                tipo_actor="SOLICITANTE",
            )

    def test_las_operaciones_de_un_flujo_exigen_que_la_version_sea_suya(self):
        from django.core.exceptions import ValidationError

        from apps.catalogo.ejecucion import agregar_bloque_en_flujo

        uno = self._crear_flujo("Uno")
        otro = self._crear_flujo("Otro")
        with self.assertRaises(ValidationError):
            agregar_bloque_en_flujo(
                uno, otro.versiones.get(), self.diseñador, tipo="ACTIVIDAD", nombre="Cruzado", tipo_actor="SOLICITANTE"
            )

    def test_studio_sigue_exigiendo_catalogo_ademas_de_workflows(self):
        servicio = Servicio.objects.create(nombre="Solo Studio", categoria=self.categoria, activo=False)
        respuesta = self.client.post(
            reverse("catalogo:studio_bloque_crear", args=[servicio.pk]),
            {"nuevo-tipo": "ACTIVIDAD", "nuevo-nombre": "X", "nuevo-config-tipo_actor": "SOLICITANTE"},
        )
        self.assertEqual(respuesta.status_code, 403)

    # --- versionar y publicar ---

    def test_publicar_activa_la_version_sin_tocar_ningun_servicio(self):
        workflow = self._crear_flujo("A publicar")
        self._bloque(workflow, "Paso")
        servicio = self._servicio_usando(workflow)
        # Primera publicación: nadie cambia, no exige confirmación reforzada.
        self.client.post(reverse("flujos:publicar", args=[workflow.pk]))
        workflow.refresh_from_db()
        self.assertEqual(self._versiones(workflow), {1: self.Version.Estado.ACTIVA})
        self.assertEqual(workflow.version_activa.numero, 1)
        servicio.refresh_from_db()
        self.assertFalse(servicio.activo)  # publicar un flujo no publica servicios
        self.assertEqual(servicio.workflow_id, workflow.pk)

    def test_un_flujo_incompleto_no_se_publica_y_dice_que_falta(self):
        workflow = self._crear_flujo("Incompleto")
        self._bloque(workflow, "Decide", tipo="DECISION")  # decisión sin condiciones ni salida
        respuesta = self.client.get(reverse("flujos:lienzo", args=[workflow.pk]))
        self.assertFalse(respuesta.context["publicable"])
        self.assertTrue(respuesta.context["errores_publicacion"])
        self.client.post(reverse("flujos:publicar", args=[workflow.pk]))
        workflow.refresh_from_db()
        self.assertIsNone(workflow.version_activa_id)

    def test_modificar_un_flujo_en_uso_pide_confirmacion_y_no_afecta_hasta_publicar(self):
        workflow = self._flujo_publicado()
        servicio = self._servicio_usando(workflow)
        version_activa = workflow.version_activa_id

        sin_confirmar = self.client.post(reverse("flujos:preparar", args=[workflow.pk]))
        self.assertEqual(self._versiones(workflow), {1: self.Version.Estado.ACTIVA})  # no se abrió borrador
        self.assertIn("usan", " ".join(str(m) for m in get_messages_de(sin_confirmar)))

        self.client.post(reverse("flujos:preparar", args=[workflow.pk]), {"confirmo_compartido": "1"})
        self.assertEqual(self._versiones(workflow), {1: self.Version.Estado.ACTIVA, 2: self.Version.Estado.BORRADOR})
        workflow.refresh_from_db()
        self.assertEqual(workflow.version_activa_id, version_activa)  # el borrador no afecta a nadie
        servicio.refresh_from_db()
        self.assertEqual(servicio.workflow_id, workflow.pk)

    def test_publicar_una_version_nueva_de_un_flujo_en_uso_exige_confirmacion_reforzada(self):
        workflow = self._flujo_publicado()
        self._servicio_usando(workflow)
        self.client.post(reverse("flujos:preparar", args=[workflow.pk]), {"confirmo_compartido": "1"})
        url = reverse("flujos:publicar", args=[workflow.pk])

        self.client.post(url)  # sin confirmar
        self.client.post(url, {"confirmo_compartido": "1", "confirmacion_nombre": "otro nombre"})
        self.assertEqual(self._versiones(workflow)[2], self.Version.Estado.BORRADOR)

        self.client.post(url, {"confirmo_compartido": "1", "confirmacion_nombre": workflow.nombre})
        self.assertEqual(
            self._versiones(workflow), {1: self.Version.Estado.HISTORICA, 2: self.Version.Estado.ACTIVA}
        )  # la anterior queda guardada: no es irreversible

    def test_un_flujo_sin_uso_se_modifica_y_publica_sin_confirmaciones(self):
        workflow = self._flujo_publicado()
        self.client.post(reverse("flujos:preparar", args=[workflow.pk]))
        self.assertEqual(self._versiones(workflow)[2], self.Version.Estado.BORRADOR)
        self.client.post(reverse("flujos:publicar", args=[workflow.pk]))
        self.assertEqual(self._versiones(workflow)[2], self.Version.Estado.ACTIVA)

    def test_se_puede_ver_una_version_anterior_en_solo_lectura(self):
        workflow = self._flujo_publicado()
        self.client.post(reverse("flujos:preparar", args=[workflow.pk]))
        v1 = workflow.versiones.get(numero=1)
        respuesta = self.client.get(f"{reverse('flujos:lienzo', args=[workflow.pk])}?version={v1.pk}")
        self.assertFalse(respuesta.context["editable_ejecucion"])
        self.assertTrue(respuesta.context["viendo_otra"])
        por_defecto = self.client.get(reverse("flujos:lienzo", args=[workflow.pk]))
        self.assertTrue(por_defecto.context["editable_ejecucion"])  # el borrador es la versión de trabajo
        self.assertEqual(self.client.get(f"{reverse('flujos:lienzo', args=[workflow.pk])}?version=abc").status_code, 200)

    # --- copiar ---

    def test_crear_copia_es_independiente_y_no_cambia_a_nadie(self):
        original = self._flujo_publicado("Original")
        servicio = self._servicio_usando(original)
        etapas_originales = original.versiones.get().etapas.count()

        respuesta = self.client.post(reverse("flujos:copia", args=[original.pk]), {"nombre": "Mi copia"})
        copia = self.Workflow.objects.get(nombre="Mi copia")
        self.assertRedirects(respuesta, reverse("flujos:lienzo", args=[copia.pk]))
        self.assertNotEqual(copia.pk, original.pk)
        self.assertEqual(self._versiones(copia), {1: self.Version.Estado.BORRADOR})
        self.assertEqual(copia.versiones.get().etapas.count(), etapas_originales)

        # El original y su servicio no cambian; modificar la copia no toca al original.
        servicio.refresh_from_db()
        self.assertEqual(servicio.workflow_id, original.pk)
        self._bloque(copia, "Solo en la copia")
        self.assertEqual(original.versiones.get().etapas.count(), etapas_originales)
        self.assertEqual(self._versiones(original), {1: self.Version.Estado.ACTIVA})

    def test_la_copia_sin_nombre_usa_uno_por_defecto(self):
        original = self._flujo_publicado("Base")
        self.client.post(reverse("flujos:copia", args=[original.pk]), {"nombre": ""})
        self.assertTrue(self.Workflow.objects.filter(nombre="Copia de Base").exists())

    # --- datos y lectura ---

    def test_editar_nombre_y_descripcion(self):
        workflow = self._crear_flujo("Nombre viejo")
        self.client.post(reverse("flujos:datos", args=[workflow.pk]), {"nombre": "Nombre nuevo", "descripcion": "Nueva"})
        workflow.refresh_from_db()
        self.assertEqual((workflow.nombre, workflow.descripcion), ("Nombre nuevo", "Nueva"))
        self.client.post(reverse("flujos:datos", args=[workflow.pk]), {"nombre": "  ", "descripcion": ""})
        workflow.refresh_from_db()
        self.assertEqual(workflow.nombre, "Nombre nuevo")

    def test_el_lienzo_muestra_para_quien_se_usa_y_las_versiones(self):
        workflow = self._flujo_publicado("Compartido")
        self._servicio_usando(workflow, "Servicio uno")
        self._servicio_usando(workflow, "Servicio dos")
        respuesta = self.client.get(reverse("flujos:lienzo", args=[workflow.pk]))
        self.assertEqual(sorted(s.nombre for s in respuesta.context["usado_por"]), ["Servicio dos", "Servicio uno"])
        self.assertContains(respuesta, "Servicio uno")
        self.assertContains(respuesta, "afectará a sus tickets")
        self.assertEqual([v.numero for v in respuesta.context["versiones"]], [1])

    def test_las_acciones_solo_aceptan_post(self):
        workflow = self._crear_flujo()
        for nombre in ("datos", "preparar", "publicar", "copia", "bloque_crear"):
            self.assertEqual(self.client.get(reverse(f"flujos:{nombre}", args=[workflow.pk])).status_code, 405, nombre)

    def test_exigen_autenticacion(self):
        workflow = self._crear_flujo()
        self.client.logout()
        for url in (reverse("flujos:nuevo"), reverse("flujos:lienzo", args=[workflow.pk])):
            self.assertEqual(self.client.get(url).status_code, 302, url)


class DisenadorServiciosD3Tests(TestCase):
    def setUp(self):
        from apps.workflows.models import Etapa, TransicionEtapa, Workflow, WorkflowVersion

        self.Etapa, self.Transicion, self.Workflow, self.Version = Etapa, TransicionEtapa, Workflow, WorkflowVersion
        self.configurador = Usuario.objects.create_user("d3_configurador", password=CLAVE_PRUEBA)
        for codigo in ("catalogo.administrar", "formulario.administrar", "workflows.vincular", "workflows.administrar"):
            _permiso_get_or_create(self.configurador, codigo, f"Rol D3 {codigo}")
        self.solo_vincula = Usuario.objects.create_user("d3_vincula", password=CLAVE_PRUEBA)
        for codigo in ("catalogo.administrar", "formulario.administrar", "workflows.vincular"):
            _permiso_get_or_create(self.solo_vincula, codigo, f"Rol D3 solo {codigo}")
        self.categoria = Categoria.objects.create(nombre="D3")

    def _formulario_activo(self, nombre="Entrada D3"):
        formulario = Formulario.objects.create(nombre=nombre)
        version = crear_nueva_version(formulario, self.configurador)
        activar_version(formulario, version, self.configurador)
        return formulario

    def _servicio(self, nombre="Servicio D3", *, tipo=Servicio.Tipo.SERVICIO, workflow=None):
        return Servicio.objects.create(
            nombre=nombre,
            categoria=self.categoria,
            tipo=tipo,
            formulario=self._formulario_activo(f"Entrada {nombre}"),
            activo=False,
            workflow=workflow,
        )

    def _flujo_publicado(self, nombre="Compra de insumos"):
        from apps.workflows import fases as fases_ops
        from apps.workflows.versionamiento import activar_version as activar_workflow

        workflow = fases_ops.crear_plantilla_fases(
            self.configurador, nombre=nombre, descripcion="Flujo publicado reutilizable"
        )
        version = workflow.versiones.get(numero=1)
        recepcion = fases_ops.agregar_fase(version, self.configurador, nombre="Recepcion")
        revision = fases_ops.agregar_fase(version, self.configurador, nombre="Revision")
        fases_ops.conectar_fases(recepcion, revision, self.configurador)
        activar_workflow(workflow, version, self.configurador)
        workflow.refresh_from_db()
        return workflow

    def _activar_configuracion_operativa(self, servicio):
        from apps.catalogo.configuracion_ejecucion import (
            activar_configuracion_ejecucion,
            agregar_bloque_operativo,
            crear_nueva_version_configuracion,
        )

        version = crear_nueva_version_configuracion(servicio, self.configurador)
        for fase in servicio.workflow.version_activa.fases.order_by("orden", "pk"):
            agregar_bloque_operativo(
                version,
                self.configurador,
                fase=fase,
                tipo=BloqueOperativo.Tipo.ACTIVIDAD,
                nombre=f"Atender {fase.nombre}",
                configuracion={"tipo_actor": "SOLICITANTE"},
            )
        activar_configuracion_ejecucion(servicio, version, self.configurador)
        servicio.refresh_from_db()

    def test_servicio_simple_sin_flujo_es_opcional_en_disenador(self):
        self.client.login(username="d3_configurador", password=CLAVE_PRUEBA)
        servicio = self._servicio("Simple sin flujo")
        respuesta = self.client.get(reverse("catalogo:studio", args=[servicio.pk]), {"tab": "ejecucion"})
        self.assertEqual(respuesta.status_code, 200)
        flujo = next(s for s in respuesta.context["secciones"] if s["clave"] == "ejecucion")
        self.assertEqual(flujo["estado"], "optional")
        self.assertContains(respuesta, "Sin flujo")

    def test_proceso_sin_flujo_requiere_atencion_y_no_publica(self):
        from apps.catalogo.operaciones import validar_publicacion

        self.client.login(username="d3_configurador", password=CLAVE_PRUEBA)
        proceso = self._servicio("Proceso sin flujo", tipo=Servicio.Tipo.PROCESO)
        respuesta = self.client.get(reverse("catalogo:studio", args=[proceso.pk]), {"tab": "ejecucion"})
        flujo = next(s for s in respuesta.context["secciones"] if s["clave"] == "ejecucion")
        self.assertEqual(flujo["estado"], "attention")
        with self.assertRaises(ValidationError):
            validar_publicacion(proceso)

    def test_usuario_que_solo_vincula_ve_tarjetas_y_puede_seleccionar_flujo_publicado(self):
        workflow = self._flujo_publicado()
        servicio = self._servicio("Usa existente")
        self.client.login(username="d3_vincula", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("catalogo:studio", args=[servicio.pk]), {"tab": "ejecucion"})
        self.assertContains(respuesta, "Compra de insumos")
        self.assertContains(respuesta, "Revision")
        self.assertNotContains(respuesta, "Crear nuevo flujo")
        self.assertEqual(
            self.client.post(reverse("catalogo:studio_ejecucion_crear_flujo", args=[servicio.pk]), {"nombre": "No"}).status_code,
            403,
        )

        self.client.post(reverse("catalogo:studio_ejecucion_vincular", args=[servicio.pk]), {"plantilla": workflow.pk})
        servicio.refresh_from_db()
        self.assertEqual(servicio.workflow_id, workflow.pk)

    def test_usuario_que_solo_vincula_puede_publicar_servicio_con_flujo_ya_publicado(self):
        workflow = self._flujo_publicado()
        servicio = self._servicio("Publica con flujo", workflow=workflow)
        self._activar_configuracion_operativa(servicio)
        self.client.login(username="d3_vincula", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("catalogo:studio_publicar", args=[servicio.pk]))
        self.assertEqual(respuesta.status_code, 302)
        servicio.refresh_from_db()
        self.assertTrue(servicio.activo)

    def test_crear_flujo_desde_servicio_crea_workflow_real_y_conserva_retorno(self):
        self.client.login(username="d3_configurador", password=CLAVE_PRUEBA)
        servicio = self._servicio("Servicio crea flujo")
        respuesta = self.client.post(
            reverse("catalogo:studio_ejecucion_crear_flujo", args=[servicio.pk]),
            {"nombre": "Flujo desde servicio"},
        )
        workflow = self.Workflow.objects.get(nombre="Flujo desde servicio")
        self.assertRedirects(respuesta, reverse("flujos:lienzo", args=[workflow.pk]))
        servicio.refresh_from_db()
        self.assertIsNone(servicio.workflow_id)
        lienzo = self.client.get(reverse("flujos:lienzo", args=[workflow.pk]))
        self.assertEqual(lienzo.context["servicio_retorno"], servicio)

    def test_studio_muestra_accion_para_activar_configuracion_operativa(self):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, preparar_configuracion_ejecucion

        workflow = self._flujo_publicado("Flujo operativo D3")
        servicio = self._servicio("Servicio activa config", workflow=workflow)
        version = preparar_configuracion_ejecucion(servicio, self.configurador)
        fase = workflow.version_activa.fases.order_by("orden", "pk").first()
        agregar_bloque_operativo(
            version,
            self.configurador,
            fase=fase,
            tipo=BloqueOperativo.Tipo.ACTIVIDAD,
            nombre="Revisar",
            configuracion={"tipo_actor": "SOLICITANTE"},
        )

        self.client.login(username="d3_configurador", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("catalogo:studio", args=[servicio.pk]), {"tab": "ejecucion"})

        flujo = next(s for s in respuesta.context["secciones"] if s["clave"] == "ejecucion")
        self.assertEqual(flujo["texto"], "Lista para activar")
        self.assertContains(respuesta, "Activar configuracion")

    def test_copia_contextual_solo_cambia_el_vinculo_del_servicio_actual(self):
        workflow = self._flujo_publicado("Compartido D3")
        servicio_a = self._servicio("Servicio A", workflow=workflow)
        servicio_b = self._servicio("Servicio B", workflow=workflow)
        self.client.login(username="d3_configurador", password=CLAVE_PRUEBA)

        self.client.post(reverse("catalogo:studio_ejecucion_copia", args=[servicio_a.pk]))
        servicio_a.refresh_from_db()
        servicio_b.refresh_from_db()
        self.assertNotEqual(servicio_a.workflow_id, workflow.pk)
        self.assertEqual(servicio_b.workflow_id, workflow.pk)
        self.assertEqual(self.Workflow.objects.get(pk=servicio_a.workflow_id).version_activa.estado, self.Version.Estado.ACTIVA)


def get_messages_de(respuesta):
    from django.contrib.messages import get_messages

    return list(get_messages(respuesta.wsgi_request))
