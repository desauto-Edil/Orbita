"""Pruebas de `apps.core` — incrementos 0.2 (Identidad y Organización), 0.3
(Roles, Permisos y Alcances), 0.4 (Auditoría y trazabilidad) y 0.5 (Base
visual y Application Shell).

No se ejecutan como parte de la implementación (instrucción explícita de
todos los incrementos): se entregan junto con los comandos exactos para
correrlas manualmente vía Docker. Organizadas en clases por CU, en un
único archivo (decisión de arquitectura: sin paquete `tests/`). Correr
`apps.core` completo corre 0.2, 0.3, 0.4 y 0.5 juntas, comprobando
regresiones.
"""

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.admin import RegistroAuditoriaAdmin
from apps.core.auditoria import registrar_evento, serializar
from apps.core.autorizacion import alcances_autorizados, usuario_tiene_permiso
from apps.core.models import (
    Area,
    AreaUnidadNegocio,
    AsignacionRol,
    Equipo,
    EquipoArea,
    EquipoUnidadNegocio,
    MiembroEquipo,
    PerfilOrganizacional,
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


class IdentidadTests(TestCase):
    """CU-001 Autenticarse, CU-002 Cerrar sesión (RQF-001/002/004, RN-001)."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(
            username="jperez", password=CLAVE_PRUEBA, email="jperez@edilandina.com"
        )

    def test_login_valido_establece_sesion(self):
        # RQF-001, CU-001
        respuesta = self.client.post(
            reverse("core:login"), {"username": "jperez", "password": CLAVE_PRUEBA}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_invalido_no_autentica(self):
        # RQF-001
        respuesta = self.client.post(
            reverse("core:login"), {"username": "jperez", "password": "incorrecta"}
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout_invalida_sesion(self):
        # RQF-002, RN-001, CU-002
        self.client.login(username="jperez", password=CLAVE_PRUEBA)
        self.client.post(reverse("core:logout"))
        respuesta = self.client.get(reverse("core:perfil"))
        self.assertEqual(respuesta.status_code, 302)

    def test_acceso_protegido_sin_sesion_redirige_a_login(self):
        # RQF-004, RN-001, CU-001
        respuesta = self.client.get(reverse("core:perfil"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse("core:login"), respuesta.url)


class PerfilTests(TestCase):
    """CU-003 Consultar perfil (RQF-003/016/017, RN-004)."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(
            username="mgomez", password=CLAVE_PRUEBA, email="mgomez@edilandina.com"
        )
        PerfilOrganizacional.objects.create(usuario=self.usuario, cargo="Analista")
        self.area1 = Area.objects.create(nombre="TIC", codigo="TIC-P")
        self.area2 = Area.objects.create(nombre="Compras", codigo="COMP-P")
        self.unidad1 = UnidadNegocio.objects.create(nombre="Infraestructura", codigo="INFRA-P")
        self.unidad2 = UnidadNegocio.objects.create(nombre="Logística", codigo="LOG-P")
        UsuarioArea.objects.create(usuario=self.usuario, area=self.area1, es_principal=True)
        UsuarioArea.objects.create(usuario=self.usuario, area=self.area2)
        UsuarioUnidadNegocio.objects.create(
            usuario=self.usuario, unidad_negocio=self.unidad1, es_principal=True
        )
        UsuarioUnidadNegocio.objects.create(usuario=self.usuario, unidad_negocio=self.unidad2)

    def test_consultar_perfil_muestra_datos_propios_y_areas_unidades(self):
        # RQF-003, RQF-016, RQF-017, RN-004, CU-003
        self.client.login(username="mgomez", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:perfil"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["areas"].count(), 2)
        self.assertEqual(respuesta.context["unidades_negocio"].count(), 2)
        self.assertEqual(respuesta.context["area_principal"].area, self.area1)
        self.assertEqual(respuesta.context["unidad_principal"].unidad_negocio, self.unidad1)


class OrganizacionTests(TestCase):
    """CU-004 Gestionar estructura organizacional, CU-005 Gestionar usuarios y
    pertenencia organizacional (RQF-009..017, RN-002/003/004/036/037)."""

    def test_crear_area(self):
        # RQF-010
        area = Area.objects.create(nombre="Talento Humano", codigo="TH")
        self.assertTrue(area.activo)

    def test_editar_area_actualiza_metadata_de_registro(self):
        # RQF-011 (parcial: solo timestamps de RegistroBase; el historial de
        # valores previos/nuevos por cambio es responsabilidad de 0.4)
        area = Area.objects.create(nombre="Finanzas", codigo="FIN")
        creado_en_original = area.creado_en
        area.nombre = "Finanzas Corporativas"
        area.save()
        area.refresh_from_db()
        self.assertEqual(area.nombre, "Finanzas Corporativas")
        self.assertEqual(area.creado_en, creado_en_original)
        self.assertGreater(area.actualizado_en, creado_en_original)

    def test_desactivar_area_no_elimina_registro(self):
        # RQF-009, RQF-012, RN-002
        area = Area.objects.create(nombre="Mercadeo", codigo="MKT")
        area.activo = False
        area.save()
        self.assertTrue(Area.objects.filter(pk=area.pk, activo=False).exists())

    def test_unidad_negocio_es_catalogo_independiente(self):
        # RQF-013, RN-003
        campos = {campo.name for campo in UnidadNegocio._meta.get_fields()}
        self.assertNotIn("area", campos)
        unidad = UnidadNegocio.objects.create(nombre="Operaciones", codigo="OPS")
        self.assertIsNotNone(unidad.pk)

    def test_area_unidad_relacion_transversal_multiple(self):
        # RQF-014, RN-003
        area = Area.objects.create(nombre="TIC", codigo="TIC2")
        unidad1 = UnidadNegocio.objects.create(nombre="Infraestructura", codigo="INFRA2")
        unidad2 = UnidadNegocio.objects.create(nombre="Desarrollo", codigo="DEV2")
        AreaUnidadNegocio.objects.create(area=area, unidad_negocio=unidad1)
        AreaUnidadNegocio.objects.create(area=area, unidad_negocio=unidad2)
        self.assertEqual(area.unidades_relacionadas.count(), 2)

    def test_perfil_organizacional_separado_de_autenticacion(self):
        # RQF-015
        usuario = Usuario.objects.create_user(username="lcastro", password=CLAVE_PRUEBA)
        perfil = PerfilOrganizacional.objects.create(usuario=usuario, cargo="Coordinador")
        self.assertNotEqual(type(usuario), type(perfil))
        self.assertEqual(usuario.perfil_organizacional, perfil)

    def test_usuario_pertenece_a_varias_areas_y_unidades_simultaneamente(self):
        # RQF-016, RQF-017, RN-004
        usuario = Usuario.objects.create_user(username="afranco", password=CLAVE_PRUEBA)
        area1 = Area.objects.create(nombre="Legal", codigo="LEG")
        area2 = Area.objects.create(nombre="Riesgo", codigo="RIE")
        unidad1 = UnidadNegocio.objects.create(nombre="Cumplimiento", codigo="CUMP")
        unidad2 = UnidadNegocio.objects.create(nombre="Auditoría Interna", codigo="AUDI")
        UsuarioArea.objects.create(usuario=usuario, area=area1)
        UsuarioArea.objects.create(usuario=usuario, area=area2)
        UsuarioUnidadNegocio.objects.create(usuario=usuario, unidad_negocio=unidad1)
        UsuarioUnidadNegocio.objects.create(usuario=usuario, unidad_negocio=unidad2)
        self.assertEqual(usuario.areas.count(), 2)
        self.assertEqual(usuario.unidades_negocio.count(), 2)

    def test_rn036_solo_una_area_principal_activa_por_usuario(self):
        # RN-036
        usuario = Usuario.objects.create_user(username="rvargas", password=CLAVE_PRUEBA)
        area1 = Area.objects.create(nombre="Contabilidad", codigo="CONT")
        area2 = Area.objects.create(nombre="Tesorería", codigo="TES")
        UsuarioArea.objects.create(usuario=usuario, area=area1, es_principal=True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UsuarioArea.objects.create(usuario=usuario, area=area2, es_principal=True)

    def test_rn037_solo_una_unidad_principal_activa_por_usuario(self):
        # RN-037
        usuario = Usuario.objects.create_user(username="dpineda", password=CLAVE_PRUEBA)
        unidad1 = UnidadNegocio.objects.create(nombre="Compras", codigo="COMPU")
        unidad2 = UnidadNegocio.objects.create(nombre="Inventarios", codigo="INV")
        UsuarioUnidadNegocio.objects.create(
            usuario=usuario, unidad_negocio=unidad1, es_principal=True
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UsuarioUnidadNegocio.objects.create(
                    usuario=usuario, unidad_negocio=unidad2, es_principal=True
                )


class EquiposTests(TestCase):
    """CU-006 Gestionar equipos de trabajo (RQF-018/019/020, RN-005)."""

    def test_crear_equipo_con_miembros(self):
        # RQF-018
        equipo = Equipo.objects.create(nombre="Soporte N1")
        usuario = Usuario.objects.create_user(username="hgomez", password=CLAVE_PRUEBA)
        MiembroEquipo.objects.create(equipo=equipo, usuario=usuario)
        self.assertEqual(equipo.miembros.count(), 1)

    def test_retirar_miembro_conserva_fila_con_fecha_fin(self):
        # RQF-019
        equipo = Equipo.objects.create(nombre="Soporte N2")
        usuario = Usuario.objects.create_user(username="ncruz", password=CLAVE_PRUEBA)
        membresia = MiembroEquipo.objects.create(equipo=equipo, usuario=usuario)

        membresia.activo = False
        membresia.fecha_fin = timezone.now().date()
        membresia.save()

        self.assertTrue(MiembroEquipo.objects.filter(pk=membresia.pk, activo=False).exists())
        self.assertIsNotNone(MiembroEquipo.objects.get(pk=membresia.pk).fecha_fin)

    def test_equipo_transversal_a_varias_areas_y_unidades(self):
        # RQF-020, RN-005
        equipo = Equipo.objects.create(nombre="Equipo Transversal")
        area1 = Area.objects.create(nombre="TIC", codigo="TIC3")
        area2 = Area.objects.create(nombre="Operaciones", codigo="OPS3")
        unidad1 = UnidadNegocio.objects.create(nombre="Infraestructura", codigo="INFRA3")
        EquipoArea.objects.create(equipo=equipo, area=area1)
        EquipoArea.objects.create(equipo=equipo, area=area2)
        EquipoUnidadNegocio.objects.create(equipo=equipo, unidad_negocio=unidad1)
        self.assertEqual(equipo.areas.count(), 2)
        self.assertEqual(equipo.unidades_negocio.count(), 1)


class RolesPermisosTests(TestCase):
    """CU-007 Gestionar roles y permisos (RQF-021/022, RN-007)."""

    def test_crear_rol_funcional(self):
        # RQF-021
        rol = RolFuncional.objects.create(nombre="Gestor de Servicios")
        self.assertTrue(rol.activo)

    def test_crear_permiso_y_asociarlo_a_rol(self):
        # RQF-022
        permiso = Permiso.objects.create(codigo="organizacion.area.administrar", nombre="Administrar áreas")
        rol = RolFuncional.objects.create(nombre="Administrador de Área")
        RolPermiso.objects.create(rol=rol, permiso=permiso)
        self.assertEqual(rol.rolpermiso.count(), 1)

    def test_permiso_puede_asociarse_a_varios_roles(self):
        # RQF-022
        permiso = Permiso.objects.create(codigo="organizacion.area.consultar", nombre="Consultar áreas")
        rol1 = RolFuncional.objects.create(nombre="Analista")
        rol2 = RolFuncional.objects.create(nombre="Auditor")
        RolPermiso.objects.create(rol=rol1, permiso=permiso)
        RolPermiso.objects.create(rol=rol2, permiso=permiso)
        self.assertEqual(permiso.rolpermiso.count(), 2)

    def test_rn007_autorizacion_no_depende_del_nombre_del_rol(self):
        # RN-007: la decisión se resuelve por permisos efectivos, nunca por
        # el nombre del rol — renombrar el rol no debe alterar la decisión.
        permiso = Permiso.objects.create(codigo="equipos.gestionar", nombre="Gestionar equipos")
        rol = RolFuncional.objects.create(nombre="Cualquiera")
        RolPermiso.objects.create(rol=rol, permiso=permiso)
        usuario = Usuario.objects.create_user(username="rfranco", password=CLAVE_PRUEBA)
        AsignacionRol.objects.create(
            usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
        )

        self.assertTrue(usuario_tiene_permiso(usuario, "equipos.gestionar"))

        rol.nombre = "Nombre Completamente Distinto"
        rol.save()

        self.assertTrue(usuario_tiene_permiso(usuario, "equipos.gestionar"))


class AsignacionRolTests(TestCase):
    """CU-008 Asignar roles y alcances (RQF-023/024/025, RN-006/008) e
    integridad de `AsignacionRol`."""

    def setUp(self):
        self.permiso = Permiso.objects.create(codigo="areas.administrar", nombre="Administrar áreas")
        self.rol = RolFuncional.objects.create(nombre="Administrador de Área")
        RolPermiso.objects.create(rol=self.rol, permiso=self.permiso)
        self.usuario = Usuario.objects.create_user(username="cmora", password=CLAVE_PRUEBA)
        self.area = Area.objects.create(nombre="TIC", codigo="TIC-AR")
        self.unidad = UnidadNegocio.objects.create(nombre="Infraestructura", codigo="INFRA-AR")

    def test_asignar_rol_a_usuario_con_alcance_y_vigencia(self):
        # RQF-023
        asignacion = AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.AREA,
            area=self.area,
        )
        self.assertTrue(asignacion.activo)
        self.assertIsNotNone(asignacion.fecha_inicio)

    def test_retirar_asignacion_conserva_fila_historica(self):
        # RQF-024
        asignacion = AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL,
        )
        asignacion.activo = False
        asignacion.fecha_fin = timezone.now().date()
        asignacion.save()
        self.assertTrue(AsignacionRol.objects.filter(pk=asignacion.pk, activo=False).exists())

    def test_constraint_global_no_permite_area_ni_unidad(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AsignacionRol.objects.create(
                    usuario=self.usuario,
                    rol=self.rol,
                    tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL,
                    area=self.area,
                )

    def test_constraint_area_exige_area_y_rechaza_unidad(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AsignacionRol.objects.create(
                    usuario=self.usuario,
                    rol=self.rol,
                    tipo_alcance=AsignacionRol.TipoAlcance.AREA,
                    area=self.area,
                    unidad_negocio=self.unidad,
                )

    def test_constraint_unidad_exige_unidad_y_rechaza_area(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AsignacionRol.objects.create(
                    usuario=self.usuario,
                    rol=self.rol,
                    tipo_alcance=AsignacionRol.TipoAlcance.UNIDAD,
                    unidad_negocio=self.unidad,
                    area=self.area,
                )

    def test_no_permite_duplicar_asignacion_global_activa(self):
        AsignacionRol.objects.create(
            usuario=self.usuario, rol=self.rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AsignacionRol.objects.create(
                    usuario=self.usuario,
                    rol=self.rol,
                    tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL,
                )

    def test_no_permite_duplicar_asignacion_area_activa(self):
        AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.AREA,
            area=self.area,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AsignacionRol.objects.create(
                    usuario=self.usuario,
                    rol=self.rol,
                    tipo_alcance=AsignacionRol.TipoAlcance.AREA,
                    area=self.area,
                )

    def test_no_permite_duplicar_asignacion_unidad_activa(self):
        # Deuda técnica de 0.3: análoga a test_no_permite_duplicar_asignacion_area_activa,
        # faltaba su equivalente explícito para alcance UNIDAD.
        AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.UNIDAD,
            unidad_negocio=self.unidad,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AsignacionRol.objects.create(
                    usuario=self.usuario,
                    rol=self.rol,
                    tipo_alcance=AsignacionRol.TipoAlcance.UNIDAD,
                    unidad_negocio=self.unidad,
                )

    def test_permite_nueva_asignacion_tras_desactivar_la_anterior(self):
        anterior = AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.AREA,
            area=self.area,
        )
        anterior.activo = False
        anterior.fecha_fin = timezone.now().date()
        anterior.save()

        nueva = AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.AREA,
            area=self.area,
        )

        self.assertTrue(AsignacionRol.objects.filter(pk=anterior.pk, activo=False).exists())
        self.assertTrue(AsignacionRol.objects.filter(pk=nueva.pk, activo=True).exists())


class AutorizacionTests(TestCase):
    """CU-009 Validar autorización y visibilidad (RQF-026/028, RN-006/008/009)."""

    def setUp(self):
        self.permiso = Permiso.objects.create(codigo="areas.administrar", nombre="Administrar áreas")
        self.rol = RolFuncional.objects.create(nombre="Administrador de Área")
        RolPermiso.objects.create(rol=self.rol, permiso=self.permiso)
        self.usuario = Usuario.objects.create_user(username="jsalas", password=CLAVE_PRUEBA)
        self.area = Area.objects.create(nombre="TIC", codigo="TIC-AZ")
        self.otra_area = Area.objects.create(nombre="Compras", codigo="COMP-AZ")
        self.unidad = UnidadNegocio.objects.create(nombre="Infraestructura", codigo="INFRA-AZ")

    def test_usuario_tiene_permiso_global_aplica_en_cualquier_area_o_unidad(self):
        # RQF-025, RN-008: GLOBAL aplica también al consultar un contexto concreto
        AsignacionRol.objects.create(
            usuario=self.usuario, rol=self.rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
        )
        self.assertTrue(usuario_tiene_permiso(self.usuario, "areas.administrar"))
        self.assertTrue(usuario_tiene_permiso(self.usuario, "areas.administrar", area=self.area))
        self.assertTrue(
            usuario_tiene_permiso(self.usuario, "areas.administrar", unidad_negocio=self.unidad)
        )

    def test_usuario_tiene_permiso_sin_contexto_solo_considera_global(self):
        # Sin area/unidad_negocio la consulta es explícitamente por GLOBAL,
        # no "en cualquier alcance" — una asignación de AREA no debe bastar.
        AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.AREA,
            area=self.area,
        )
        self.assertFalse(usuario_tiene_permiso(self.usuario, "areas.administrar"))

    def test_usuario_tiene_permiso_area_no_aplica_fuera_de_esa_area(self):
        # RQF-026, RN-006
        AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.AREA,
            area=self.area,
        )
        self.assertTrue(usuario_tiene_permiso(self.usuario, "areas.administrar", area=self.area))
        self.assertFalse(
            usuario_tiene_permiso(self.usuario, "areas.administrar", area=self.otra_area)
        )

    def test_usuario_tiene_permiso_unidad_no_aplica_fuera_de_esa_unidad(self):
        otra_unidad = UnidadNegocio.objects.create(nombre="Logística", codigo="LOG-AZ")
        AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.UNIDAD,
            unidad_negocio=self.unidad,
        )
        self.assertTrue(
            usuario_tiene_permiso(self.usuario, "areas.administrar", unidad_negocio=self.unidad)
        )
        self.assertFalse(
            usuario_tiene_permiso(self.usuario, "areas.administrar", unidad_negocio=otra_unidad)
        )

    def test_usuario_tiene_permiso_respeta_fecha_inicio_futura(self):
        AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL,
            fecha_inicio=timezone.now().date() + timezone.timedelta(days=5),
        )
        self.assertFalse(usuario_tiene_permiso(self.usuario, "areas.administrar"))

    def test_usuario_tiene_permiso_respeta_fecha_fin_vencida(self):
        AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL,
            fecha_inicio=timezone.now().date() - timezone.timedelta(days=10),
            fecha_fin=timezone.now().date() - timezone.timedelta(days=1),
        )
        self.assertFalse(usuario_tiene_permiso(self.usuario, "areas.administrar"))

    def test_usuario_tiene_permiso_respeta_asignacion_inactiva(self):
        AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL,
            activo=False,
        )
        self.assertFalse(usuario_tiene_permiso(self.usuario, "areas.administrar"))

    def test_usuario_tiene_permiso_respeta_rol_inactivo(self):
        self.rol.activo = False
        self.rol.save()
        AsignacionRol.objects.create(
            usuario=self.usuario, rol=self.rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
        )
        self.assertFalse(usuario_tiene_permiso(self.usuario, "areas.administrar"))

    def test_usuario_tiene_permiso_respeta_rolpermiso_inactivo(self):
        rp = RolPermiso.objects.get(rol=self.rol, permiso=self.permiso)
        rp.activo = False
        rp.save()
        AsignacionRol.objects.create(
            usuario=self.usuario, rol=self.rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
        )
        self.assertFalse(usuario_tiene_permiso(self.usuario, "areas.administrar"))

    def test_usuario_tiene_permiso_respeta_permiso_inactivo(self):
        self.permiso.activo = False
        self.permiso.save()
        AsignacionRol.objects.create(
            usuario=self.usuario, rol=self.rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
        )
        self.assertFalse(usuario_tiene_permiso(self.usuario, "areas.administrar"))

    def test_pertenencia_organizacional_no_otorga_permiso(self):
        # RQF-028, RN-009: pertenecer a un área no otorga automáticamente permiso
        UsuarioArea.objects.create(usuario=self.usuario, area=self.area)
        self.assertFalse(usuario_tiene_permiso(self.usuario, "areas.administrar", area=self.area))

    def test_alcances_autorizados_devuelve_areas_y_unidades_reales(self):
        # CU-009 ("conjunto visible"), probado contra Area/UnidadNegocio reales
        AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.AREA,
            area=self.area,
        )
        AsignacionRol.objects.create(
            usuario=self.usuario,
            rol=self.rol,
            tipo_alcance=AsignacionRol.TipoAlcance.UNIDAD,
            unidad_negocio=self.unidad,
        )
        alcances = alcances_autorizados(self.usuario, "areas.administrar")
        self.assertFalse(alcances["global"])
        self.assertEqual(alcances["areas"], [self.area.id])
        self.assertEqual(alcances["unidades_negocio"], [self.unidad.id])

        areas_visibles = Area.objects.filter(id__in=alcances["areas"])
        self.assertEqual(list(areas_visibles), [self.area])

    def test_alcances_autorizados_vacio_equivale_a_sin_autorizacion(self):
        alcances = alcances_autorizados(self.usuario, "areas.administrar")
        self.assertFalse(alcances["global"])
        self.assertEqual(alcances["areas"], [])
        self.assertEqual(alcances["unidades_negocio"], [])


class AuditoriaTests(TestCase):
    """CU-040 Consultar auditoría y trazabilidad global (RQF-008/116/117/120,
    RN-033/034). Cierra, para los modelos de 0.2/0.3, las postcondiciones
    "...auditada"/"...cambios auditados" de CU-004, CU-005, CU-007 y CU-008."""

    def setUp(self):
        self.staff = Usuario.objects.create_user(
            username="auditor_admin", password=CLAVE_PRUEBA, is_staff=True, is_superuser=True
        )

    # --- registrar_evento / serializar: captura explícita, sin señales ---

    def test_registrar_evento_crear_no_tiene_datos_anteriores(self):
        # RQF-116
        area = Area.objects.create(nombre="Servicios Generales", codigo="SERVGEN-U")
        evento = registrar_evento(
            accion=RegistroAuditoria.Accion.CREAR,
            instancia=area,
            origen=RegistroAuditoria.Origen.USUARIO,
            usuario=self.staff,
            datos_nuevos=serializar(area),
        )
        self.assertIsNone(evento.datos_anteriores)
        self.assertEqual(evento.datos_nuevos["codigo"], "SERVGEN-U")
        self.assertEqual(evento.modelo, "core.area")
        self.assertEqual(evento.objeto_repr, "Servicios Generales")

    def test_registrar_evento_origen_usuario_exige_usuario(self):
        # RN-034
        area = Area.objects.create(nombre="Requiere Actor", codigo="ACTOR1")
        with self.assertRaises(ValueError):
            registrar_evento(
                accion=RegistroAuditoria.Accion.ACTUALIZAR,
                instancia=area,
                origen=RegistroAuditoria.Origen.USUARIO,
                usuario=None,
            )

    def test_registrar_evento_origen_sistema_no_admite_usuario(self):
        # RN-034
        area = Area.objects.create(nombre="Proceso Automático", codigo="AUTO1")
        with self.assertRaises(ValueError):
            registrar_evento(
                accion=RegistroAuditoria.Accion.ACTUALIZAR,
                instancia=area,
                origen=RegistroAuditoria.Origen.SISTEMA,
                usuario=self.staff,
            )

    def test_registrar_evento_origen_sistema_queda_sin_usuario(self):
        # RQF-008, RN-034: una acción automática se identifica como del sistema
        area = Area.objects.create(nombre="Tarea Programada", codigo="AUTO2")
        evento = registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR,
            instancia=area,
            origen=RegistroAuditoria.Origen.SISTEMA,
            datos_anteriores={"nombre": "x"},
            datos_nuevos={"nombre": "Tarea Programada"},
        )
        self.assertIsNone(evento.usuario)
        self.assertEqual(evento.origen, RegistroAuditoria.Origen.SISTEMA)

    def test_password_no_se_captura_en_serializacion(self):
        # Sección F: dato sensible excluido del snapshot
        usuario = Usuario.objects.create_user(username="conclave", password=CLAVE_PRUEBA)
        datos = serializar(usuario)
        self.assertNotIn("password", datos)

    # --- coherencia origen/usuario a nivel de base de datos ---

    def test_constraint_origen_usuario_exige_usuario_no_nulo(self):
        content_type = ContentType.objects.get_for_model(Area)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RegistroAuditoria.objects.create(
                    content_type=content_type,
                    object_id=1,
                    modelo="core.area",
                    objeto_repr="x",
                    accion=RegistroAuditoria.Accion.CREAR,
                    origen=RegistroAuditoria.Origen.USUARIO,
                    usuario=None,
                )

    def test_constraint_origen_sistema_rechaza_usuario(self):
        content_type = ContentType.objects.get_for_model(Area)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RegistroAuditoria.objects.create(
                    content_type=content_type,
                    object_id=1,
                    modelo="core.area",
                    objeto_repr="x",
                    accion=RegistroAuditoria.Accion.CREAR,
                    origen=RegistroAuditoria.Origen.SISTEMA,
                    usuario=self.staff,
                )

    # --- inmutabilidad (RN-033) ---

    def test_registro_auditoria_no_permite_actualizarse(self):
        area = Area.objects.create(nombre="Inmutable", codigo="INMUT1")
        evento = registrar_evento(
            accion=RegistroAuditoria.Accion.CREAR,
            instancia=area,
            origen=RegistroAuditoria.Origen.USUARIO,
            usuario=self.staff,
            datos_nuevos=serializar(area),
        )
        evento.objeto_repr = "Modificado"
        with self.assertRaises(ValueError):
            evento.save()

    def test_registro_auditoria_no_permite_eliminarse(self):
        area = Area.objects.create(nombre="Inmutable2", codigo="INMUT2")
        evento = registrar_evento(
            accion=RegistroAuditoria.Accion.CREAR,
            instancia=area,
            origen=RegistroAuditoria.Origen.USUARIO,
            usuario=self.staff,
            datos_nuevos=serializar(area),
        )
        with self.assertRaises(ValueError):
            evento.delete()

    # --- identidad histórica ---

    def test_identidad_historica_sobrevive_a_la_eliminacion_del_objeto(self):
        area = Area.objects.create(nombre="Temporal", codigo="TEMP1")
        evento = registrar_evento(
            accion=RegistroAuditoria.Accion.CREAR,
            instancia=area,
            origen=RegistroAuditoria.Origen.USUARIO,
            usuario=self.staff,
            datos_nuevos=serializar(area),
        )
        area_id = area.pk
        Area.objects.filter(pk=area_id).delete()
        evento.refresh_from_db()
        self.assertEqual(evento.modelo, "core.area")
        self.assertEqual(evento.objeto_repr, "Temporal")
        self.assertEqual(evento.object_id, area_id)
        self.assertIsNone(evento.objeto)

    # --- AdminAuditableMixin: captura explícita vía Django Admin ---

    def test_crear_area_via_admin_registra_evento_crear(self):
        self.client.login(username="auditor_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("admin:core_area_add"),
            {"nombre": "Servicios Generales", "codigo": "SERVGEN", "descripcion": "", "activo": "on"},
        )
        self.assertEqual(respuesta.status_code, 302)
        area = Area.objects.get(codigo="SERVGEN")
        evento = RegistroAuditoria.objects.get(modelo="core.area", object_id=area.pk)
        self.assertEqual(evento.accion, RegistroAuditoria.Accion.CREAR)
        self.assertIsNone(evento.datos_anteriores)
        self.assertEqual(evento.datos_nuevos["codigo"], "SERVGEN")
        self.assertEqual(evento.origen, RegistroAuditoria.Origen.USUARIO)
        self.assertEqual(evento.usuario_id, self.staff.pk)

    def test_editar_area_via_admin_registra_datos_anteriores_reales(self):
        # Caso crítico: datos_anteriores debe reflejar el valor previo al
        # cambio, no el nuevo.
        area = Area.objects.create(nombre="Finanzas", codigo="FINAUD")
        self.client.login(username="auditor_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("admin:core_area_change", args=[area.pk]),
            {"nombre": "Finanzas Corporativas", "codigo": "FINAUD", "descripcion": "", "activo": "on"},
        )
        self.assertEqual(respuesta.status_code, 302)
        evento = RegistroAuditoria.objects.filter(
            modelo="core.area", object_id=area.pk, accion=RegistroAuditoria.Accion.ACTUALIZAR
        ).latest("creado_en")
        self.assertEqual(evento.datos_anteriores["nombre"], "Finanzas")
        self.assertEqual(evento.datos_nuevos["nombre"], "Finanzas Corporativas")

    def test_eliminar_area_via_admin_registra_evento_eliminar(self):
        area = Area.objects.create(nombre="Descartable", codigo="DESC1")
        self.client.login(username="auditor_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("admin:core_area_delete", args=[area.pk]), {"post": "yes"})
        self.assertEqual(respuesta.status_code, 302)
        self.assertFalse(Area.objects.filter(pk=area.pk).exists())
        evento = RegistroAuditoria.objects.get(
            modelo="core.area", object_id=area.pk, accion=RegistroAuditoria.Accion.ELIMINAR
        )
        self.assertEqual(evento.datos_anteriores["codigo"], "DESC1")
        self.assertIsNone(evento.datos_nuevos)

    def test_asociar_permiso_a_rol_via_inline_registra_evento_crear(self):
        # RolPermiso solo se gestiona vía el inline de RolFuncionalAdmin —
        # sin el override de save_formset quedaría sin auditar (CU-007/RQF-120).
        rol = RolFuncional.objects.create(nombre="Rol Para Inline")
        permiso = Permiso.objects.create(codigo="inline.probar", nombre="Probar inline")
        self.client.login(username="auditor_admin", password=CLAVE_PRUEBA)
        datos = {
            "nombre": rol.nombre,
            "descripcion": "",
            "activo": "on",
            "rolpermiso-TOTAL_FORMS": "1",
            "rolpermiso-INITIAL_FORMS": "0",
            "rolpermiso-MIN_NUM_FORMS": "0",
            "rolpermiso-MAX_NUM_FORMS": "1000",
            "rolpermiso-0-id": "",
            "rolpermiso-0-permiso": str(permiso.pk),
            "rolpermiso-0-activo": "on",
        }
        respuesta = self.client.post(reverse("admin:core_rolfuncional_change", args=[rol.pk]), datos)
        self.assertEqual(respuesta.status_code, 302)
        rp = RolPermiso.objects.get(rol=rol, permiso=permiso)
        evento = RegistroAuditoria.objects.get(modelo="core.rolpermiso", object_id=rp.pk)
        self.assertEqual(evento.accion, RegistroAuditoria.Accion.CREAR)
        self.assertEqual(evento.datos_nuevos["permiso"], permiso.pk)

    def test_eliminar_permiso_de_rol_via_inline_registra_evento_eliminar(self):
        rol = RolFuncional.objects.create(nombre="Rol Para Quitar")
        permiso = Permiso.objects.create(codigo="inline.quitar", nombre="Quitar inline")
        rp = RolPermiso.objects.create(rol=rol, permiso=permiso)
        self.client.login(username="auditor_admin", password=CLAVE_PRUEBA)
        datos = {
            "nombre": rol.nombre,
            "descripcion": "",
            "activo": "on",
            "rolpermiso-TOTAL_FORMS": "1",
            "rolpermiso-INITIAL_FORMS": "1",
            "rolpermiso-MIN_NUM_FORMS": "0",
            "rolpermiso-MAX_NUM_FORMS": "1000",
            "rolpermiso-0-id": str(rp.pk),
            "rolpermiso-0-permiso": str(permiso.pk),
            "rolpermiso-0-activo": "on",
            "rolpermiso-0-DELETE": "on",
        }
        respuesta = self.client.post(reverse("admin:core_rolfuncional_change", args=[rol.pk]), datos)
        self.assertEqual(respuesta.status_code, 302)
        self.assertFalse(RolPermiso.objects.filter(pk=rp.pk).exists())
        evento = RegistroAuditoria.objects.get(
            modelo="core.rolpermiso", object_id=rp.pk, accion=RegistroAuditoria.Accion.ELIMINAR
        )
        self.assertEqual(evento.datos_anteriores["permiso"], permiso.pk)

    # --- autorización funcional de la consulta (no django.contrib.auth.Permission) ---

    def test_admin_auditoria_exige_permiso_funcional_no_is_staff(self):
        # RQF-005 (parcial), RQF-117: gateado por usuario_tiene_permiso(),
        # no por is_staff ni por nombre de rol. Reutiliza el Permiso ya
        # sembrado por la migración 0005 (no lo duplica: el código es único).
        permiso = Permiso.objects.get(codigo="auditoria.consultar")
        rol = RolFuncional.objects.create(nombre="Cualquiera")
        RolPermiso.objects.create(rol=rol, permiso=permiso)
        usuario_sin_permiso = Usuario.objects.create_user(
            username="staff_sin_permiso", password=CLAVE_PRUEBA, is_staff=True
        )
        usuario_con_permiso = Usuario.objects.create_user(
            username="staff_con_permiso", password=CLAVE_PRUEBA, is_staff=True
        )
        AsignacionRol.objects.create(
            usuario=usuario_con_permiso, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
        )
        admin_instance = RegistroAuditoriaAdmin(RegistroAuditoria, django_admin.site)
        request = RequestFactory().get("/admin/core/registroauditoria/")

        request.user = usuario_sin_permiso
        self.assertFalse(admin_instance.has_view_permission(request))

        request.user = usuario_con_permiso
        self.assertTrue(admin_instance.has_view_permission(request))

    def test_admin_auditoria_superuser_sin_permiso_funcional_tambien_es_denegado(self):
        # Decisión deliberada: is_superuser no otorga un bypass implícito;
        # la autorización pasa siempre por el sistema funcional de Órbita.
        superusuario = Usuario.objects.create_user(
            username="superuser_sin_permiso", password=CLAVE_PRUEBA, is_staff=True, is_superuser=True
        )
        admin_instance = RegistroAuditoriaAdmin(RegistroAuditoria, django_admin.site)
        request = RequestFactory().get("/admin/core/registroauditoria/")
        request.user = superusuario
        self.assertFalse(admin_instance.has_view_permission(request))

    def test_admin_auditoria_es_de_solo_lectura(self):
        # CU-040 postcondición: "Consulta sin modificación del log."
        admin_instance = RegistroAuditoriaAdmin(RegistroAuditoria, django_admin.site)
        request = RequestFactory().get("/admin/core/registroauditoria/")
        request.user = self.staff
        self.assertFalse(admin_instance.has_add_permission(request))
        self.assertFalse(admin_instance.has_change_permission(request))
        self.assertFalse(admin_instance.has_delete_permission(request))


class ApplicationShellTests(TestCase):
    """Application Shell autenticado (incremento 0.5; rediseño 2.UI.1:
    topbar + dock flotante, sin sidebar) — sin CU propio.

    Comportamiento real únicamente: autenticación requerida, información
    organizacional real renderizada, y navegación condicionada a is_staff /
    alcances de `tickets.atender`. Nada sobre colores/tamaños/CSS — eso no
    es competencia de estas pruebas.
    """

    def setUp(self):
        self.usuario = Usuario.objects.create_user(
            username="ecamacho",
            password=CLAVE_PRUEBA,
            first_name="Elena",
            last_name="Camacho",
        )

    def test_inicio_requiere_autenticacion(self):
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse("core:login"), respuesta.url)

    def test_inicio_renderiza_informacion_real_del_usuario(self):
        area = Area.objects.create(nombre="Tecnología", codigo="TEC-SHELL")
        unidad = UnidadNegocio.objects.create(nombre="Plataformas", codigo="PLAT-SHELL")
        UsuarioArea.objects.create(usuario=self.usuario, area=area, es_principal=True)
        UsuarioUnidadNegocio.objects.create(
            usuario=self.usuario, unidad_negocio=unidad, es_principal=True
        )
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Elena Camacho")
        self.assertContains(respuesta, "Tecnología")
        self.assertContains(respuesta, "Plataformas")

    def test_inicio_sin_area_ni_unidad_muestra_empty_state_real(self):
        # "No existe el dominio" no equivale a "consulta con cero resultados"
        # inventada: aquí sí hay un dominio real (Área/Unidad) consultado de
        # verdad, solo que el usuario no tiene ninguna asignada todavía.
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertContains(respuesta, "Aún no tienes área ni unidad de negocio asignadas.")

    def test_administracion_visible_en_navegacion_solo_para_is_staff(self):
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertNotContains(respuesta, "Administración")

        self.usuario.is_staff = True
        self.usuario.save()
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertContains(respuesta, "Administración")

    def test_perfil_muestra_badge_principal_solo_en_la_relacion_marcada(self):
        area_principal = Area.objects.create(nombre="Finanzas", codigo="FIN-SHELL")
        area_secundaria = Area.objects.create(nombre="Compras", codigo="COMP-SHELL")
        UsuarioArea.objects.create(usuario=self.usuario, area=area_principal, es_principal=True)
        UsuarioArea.objects.create(usuario=self.usuario, area=area_secundaria)
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:perfil"))
        self.assertContains(respuesta, "Finanzas")
        self.assertContains(respuesta, "Compras")
        self.assertContains(respuesta, "Principal", count=1)

    def test_login_sigue_respondiendo_correctamente_con_base_html(self):
        respuesta = self.client.get(reverse("core:login"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Entrar")

    def test_servicios_visible_en_navegacion_para_cualquier_autenticado(self):
        # Regresión por el cambio en core.navegacion (Incremento 1.1):
        # "Servicios" es el primer módulo funcional real agregado al dock.
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertContains(respuesta, "Servicios")

    def test_shell_autenticado_contiene_dock(self):
        # 2.UI.1: el shell autenticado se navega desde el dock flotante,
        # no desde un sidebar — regresión de estructura, no de estilo.
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertContains(respuesta, 'class="dock"')
        self.assertContains(respuesta, "dock__list")

    def test_shell_ya_no_contiene_sidebar(self):
        # 2.UI.1: el sidebar permanente desaparece del shell (eliminado,
        # no solo ocultado) — sustituido por topbar + dock.
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertNotContains(respuesta, 'class="sidebar"')

    def test_shell_no_autenticado_no_renderiza_dock(self):
        # Login es la única página servida sin sesión — no debe traer el
        # dock (que depende de navegación autenticada).
        respuesta = self.client.get(reverse("core:login"))
        self.assertNotContains(respuesta, 'class="dock"')

    def test_cola_atencion_visible_en_navegacion_solo_con_permiso_tickets_atender(self):
        # 2.3/2.UI.1: "Cola de atención" solo aparece en el dock si el
        # usuario tiene `tickets.atender` en algún alcance — no es un
        # destino visible para cualquier autenticado, a diferencia de
        # "Mis tickets" o "Servicios".
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertNotContains(respuesta, "Cola de atención")

        permiso, _ = Permiso.objects.get_or_create(
            codigo="tickets.atender",
            defaults={"nombre": "Atender tickets (tomar, asignar, reasignar)"},
        )
        rol = RolFuncional.objects.create(nombre="Rol atender dock")
        RolPermiso.objects.create(rol=rol, permiso=permiso)
        AsignacionRol.objects.create(
            usuario=self.usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
        )
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertContains(respuesta, "Cola de atención")

    def test_rutas_principales_del_dock_siguen_resolviendo(self):
        # 2.UI.1 es exclusivamente visual/navegación: no debe romper ninguna
        # URL existente de los módulos ya implementados.
        for nombre in (
            "core:inicio",
            "core:perfil",
            "core:logout",
            "catalogo:lista",
            "tickets:mis_tickets",
            "tickets:cola",
            "admin:index",
        ):
            reverse(nombre)


class MensajesGlobalesTests(TestCase):
    """2.C, punto 4 — el shell venía generando `messages.success`/
    `messages.error` desde 2.1 sin renderizarlos nunca (ningún template
    incluía `{% for message in messages %}`). Corregido en
    `templates/base.html`: verifica que el shell autenticado los muestre,
    con la clase `alert--<tag>` correcta (ERROR se remapea a "danger" vía
    `MESSAGE_TAGS`, ver `config/settings.py`)."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="msgshell24c", password=CLAVE_PRUEBA)

    def _renderizar_inicio_con_mensaje(self, nivel, texto):
        from django.contrib.messages import add_message
        from django.contrib.messages.storage.fallback import FallbackStorage

        from apps.core.views import inicio_view

        request = RequestFactory().get(reverse("core:inicio"))
        request.user = self.usuario
        request.session = self.client.session
        storage = FallbackStorage(request)
        request._messages = storage
        add_message(request, nivel, texto)
        return inicio_view(request)

    def test_shell_autenticado_renderiza_messages(self):
        from django.contrib.messages import constants

        respuesta = self._renderizar_inicio_con_mensaje(constants.INFO, "Mensaje informativo de prueba.")
        contenido = respuesta.content.decode()
        self.assertIn("Mensaje informativo de prueba.", contenido)
        self.assertIn('class="messages"', contenido)

    def test_mensaje_success_es_visible_y_cerrable(self):
        from django.contrib.messages import constants

        respuesta = self._renderizar_inicio_con_mensaje(constants.SUCCESS, "Operación exitosa.")
        contenido = respuesta.content.decode()
        self.assertIn("Operación exitosa.", contenido)
        self.assertIn("alert--success", contenido)
        self.assertIn('data-action="cerrar-mensaje"', contenido)

    def test_mensaje_error_es_visible_como_danger(self):
        # RQF/convención del proyecto: ERROR se remapea a "danger" para
        # reutilizar los tokens/clases ya existentes (--danger), no un
        # nombre de clase "error" aislado.
        from django.contrib.messages import constants

        respuesta = self._renderizar_inicio_con_mensaje(constants.ERROR, "Algo salió mal.")
        contenido = respuesta.content.decode()
        self.assertIn("Algo salió mal.", contenido)
        self.assertIn("alert--danger", contenido)

    def test_mensaje_warning_es_visible(self):
        from django.contrib.messages import constants

        respuesta = self._renderizar_inicio_con_mensaje(constants.WARNING, "Advertencia de prueba.")
        contenido = respuesta.content.decode()
        self.assertIn("Advertencia de prueba.", contenido)
        self.assertIn("alert--warning", contenido)

    def test_sin_mensajes_no_renderiza_el_contenedor(self):
        respuesta = self.client.get(reverse("core:login"))
        self.assertNotContains(respuesta, 'class="messages"')
