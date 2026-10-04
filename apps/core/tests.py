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

from datetime import timedelta

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.aprobaciones.models import Aprobacion, EsquemaAprobacion
from apps.aprobaciones.operaciones import crear_esquema_aprobacion
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
from apps.tareas.models import Tarea
from apps.tareas.operaciones import crear_tarea

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

    def test_inicio_saluda_por_el_nombre_y_no_trae_datos_organizacionales(self):
        # V1: Inicio es la portada cotidiana. Cargo, área y unidad pertenecen
        # a Mi perfil: no se reserva ninguna tarjeta para ellos (ni su vacío).
        area = Area.objects.create(nombre="Tecnología", codigo="TEC-SHELL")
        unidad = UnidadNegocio.objects.create(nombre="Plataformas", codigo="PLAT-SHELL")
        UsuarioArea.objects.create(usuario=self.usuario, area=area, es_principal=True)
        UsuarioUnidadNegocio.objects.create(
            usuario=self.usuario, unidad_negocio=unidad, es_principal=True
        )
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["saludo"]["nombre"], "Elena")
        self.assertContains(respuesta, "Elena")
        for ausente in ("Tecnología", "Plataformas", "Tu información organizacional", "Aún no tienes área"):
            self.assertNotContains(respuesta, ausente)

    def test_perfil_conserva_la_informacion_organizacional(self):
        area = Area.objects.create(nombre="Tecnología", codigo="TEC-SHELL-P")
        UsuarioArea.objects.create(usuario=self.usuario, area=area, es_principal=True)
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        self.assertContains(self.client.get(reverse("core:perfil")), "Tecnología")

    def test_administracion_visible_en_navegacion_solo_para_is_staff(self):
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertNotContains(respuesta, "Administración")

        self.usuario.is_staff = True
        self.usuario.save()
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertContains(respuesta, "Administración")
        self.assertContains(respuesta, "Configuración")

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

    def test_catalogo_sigue_accesible_para_cualquier_autenticado(self):
        # V0: "Servicios" dejó el dock (solo hay Inicio/Mis tickets/Trabajo/Más),
        # pero el catálogo —único punto de partida para crear un ticket— no
        # puede quedar huérfano: Mis tickets enlaza a él y el buscador de
        # necesidades de Inicio (V1) cae en él cuando no hay JavaScript.
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        self.assertContains(self.client.get(reverse("tickets:mis_tickets")), "Nuevo ticket")
        for vista in ("core:inicio", "tickets:mis_tickets"):
            self.assertContains(self.client.get(reverse(vista)), reverse("catalogo:lista"))

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

    def test_trabajo_con_cola_visible_en_navegacion_solo_con_permiso_tickets_atender(self):
        # 2.3/V0: la Cola de atención vive dentro de "Trabajo", que solo
        # aparece si el usuario tiene `tickets.atender` en algún alcance (o
        # trabajo personal) — no es un destino visible para cualquier
        # autenticado, a diferencia de "Mis tickets".
        self.client.login(username="ecamacho", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertNotContains(respuesta, "Trabajo")

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
        self.assertContains(respuesta, "Trabajo")
        self.assertContains(respuesta, reverse("tickets:cola"))

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


class MiTrabajoTests(TestCase):
    """3.UI.5 — "Mi trabajo" (Tareas + Aprobaciones), sin CU propio: solo
    composición de lectura sobre `apps.tareas.consultas`/`apps.aprobaciones.
    consultas`, ya probadas por su propio dominio. No se duplican aquí las
    pruebas de autorización de objeto de Tareas/Aprobaciones (detalle,
    acciones) — solo composición, deduplicación, orden, pestañas, enlaces
    y navegación, que es lo que este incremento agrega de verdad."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="mtrabajo", password=CLAVE_PRUEBA)

    def _crear_esquema(self, participante):
        return crear_esquema_aprobacion(
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, participante)],
        )

    def test_requiere_autenticacion(self):
        respuesta = self.client.get(reverse("core:mi_trabajo"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse("core:login"), respuesta.url)

    def test_usuario_sin_trabajo_obtiene_empty_state(self):
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "No tienes trabajo pendiente.")

    def test_tarea_asignada_aparece_en_todo_y_en_su_pestana(self):
        crear_tarea(titulo="Revisar contrato", creada_por=self.usuario, usuario_responsable=self.usuario)
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"))
        self.assertContains(respuesta, "Revisar contrato")
        respuesta_tareas = self.client.get(reverse("core:mi_trabajo"), {"tab": "tareas"})
        self.assertContains(respuesta_tareas, "Revisar contrato")

    def test_usuario_con_tareas_gestionar_no_rompe_la_union_de_consultas(self):
        # Regresión: para quien tiene `tareas.gestionar`, "disponibles para
        # tomar" no usa `.distinct()` y `asignadas | disponibles` lanzaba
        # TypeError ("No se puede combinar una consulta única con una no única").
        permiso, _ = Permiso.objects.get_or_create(codigo="tareas.gestionar", defaults={"nombre": "tareas.gestionar"})
        rol = RolFuncional.objects.create(nombre="Gestor de tareas mi trabajo")
        RolPermiso.objects.create(rol=rol, permiso=permiso)
        AsignacionRol.objects.create(usuario=self.usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL)
        otro = Usuario.objects.create_user(username="otro_gestion", password=CLAVE_PRUEBA)
        crear_tarea(titulo="Propia gestor", creada_por=self.usuario, usuario_responsable=self.usuario)
        crear_tarea(titulo="Libre para tomar", creada_por=otro)
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Propia gestor")
        self.assertContains(respuesta, "Libre para tomar")
        self.assertContains(respuesta, "Tareas (2)")

    def test_tarea_de_otro_usuario_no_aparece(self):
        otro = Usuario.objects.create_user(username="otro_mt", password=CLAVE_PRUEBA)
        crear_tarea(titulo="Tarea ajena", creada_por=otro, usuario_responsable=otro)
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"))
        self.assertNotContains(respuesta, "Tarea ajena")

    def test_aprobacion_pendiente_aparece_en_todo_y_en_su_pestana(self):
        esquema = self._crear_esquema(self.usuario)
        aprobacion = esquema.participaciones.get()
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"))
        self.assertContains(respuesta, f"Aprobación #{aprobacion.pk}")
        respuesta_aprob = self.client.get(reverse("core:mi_trabajo"), {"tab": "aprobaciones"})
        self.assertContains(respuesta_aprob, f"Aprobación #{aprobacion.pk}")

    def test_aprobacion_no_asignada_no_aparece(self):
        otro = Usuario.objects.create_user(username="otro_aprob_mt", password=CLAVE_PRUEBA)
        esquema = self._crear_esquema(otro)
        aprobacion_ajena = esquema.participaciones.get()
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"))
        self.assertNotContains(respuesta, f"Aprobación #{aprobacion_ajena.pk}")

    def test_tarea_de_equipo_sin_responsable_no_se_duplica_en_todo(self):
        # Simultáneamente "asignada" (vía membresía de equipo) y
        # "disponible para tomar" (sin usuario_responsable directo) —
        # el caso real de solapamiento entre ambas consultas (punto 5).
        equipo = Equipo.objects.create(nombre="Equipo Mi trabajo")
        MiembroEquipo.objects.create(equipo=equipo, usuario=self.usuario, activo=True)
        crear_tarea(titulo="Tarea de equipo sin tomar", creada_por=self.usuario, equipo_responsable=equipo)
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"))
        self.assertContains(respuesta, "Tarea de equipo sin tomar", count=1)

    def test_tab_invalido_cae_en_default_seguro(self):
        crear_tarea(titulo="Con tab invalido", creada_por=self.usuario, usuario_responsable=self.usuario)
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"), {"tab": "no-existe"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Con tab invalido")
        self.assertContains(respuesta, 'breadcrumb__current">Todo')

    def test_pestana_tareas_no_muestra_aprobaciones(self):
        crear_tarea(titulo="Solo tarea", creada_por=self.usuario, usuario_responsable=self.usuario)
        aprobacion = self._crear_esquema(self.usuario).participaciones.get()
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"), {"tab": "tareas"})
        self.assertContains(respuesta, "Solo tarea")
        self.assertNotContains(respuesta, f"Aprobación #{aprobacion.pk}")

    def test_pestana_aprobaciones_no_muestra_tareas(self):
        crear_tarea(titulo="Tarea oculta en pestaña", creada_por=self.usuario, usuario_responsable=self.usuario)
        aprobacion = self._crear_esquema(self.usuario).participaciones.get()
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"), {"tab": "aprobaciones"})
        self.assertContains(respuesta, f"Aprobación #{aprobacion.pk}")
        self.assertNotContains(respuesta, "Tarea oculta en pestaña")

    def test_enlaces_apuntan_a_detalles_reales(self):
        tarea = crear_tarea(titulo="Con enlace", creada_por=self.usuario, usuario_responsable=self.usuario)
        aprobacion = self._crear_esquema(self.usuario).participaciones.get()
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"))
        self.assertContains(respuesta, reverse("tareas:detalle", args=[tarea.pk]))
        self.assertContains(respuesta, reverse("aprobaciones:detalle", args=[aprobacion.pk]))

    def test_enlaces_ver_todas_las_tareas_y_aprobaciones(self):
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"))
        self.assertContains(respuesta, reverse("tareas:lista"))
        self.assertContains(respuesta, reverse("aprobaciones:lista"))
        self.assertContains(respuesta, "Ver todas las tareas")
        self.assertContains(respuesta, "Ver todas las aprobaciones")

    def test_contadores_reflejan_las_listas_reales(self):
        crear_tarea(titulo="Contador 1", creada_por=self.usuario, usuario_responsable=self.usuario)
        crear_tarea(titulo="Contador 2", creada_por=self.usuario, usuario_responsable=self.usuario)
        self._crear_esquema(self.usuario)
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:mi_trabajo"))
        self.assertContains(respuesta, "Tareas (2)")
        self.assertContains(respuesta, "Aprobaciones (1)")

    def test_trabajo_aparece_en_navegacion_solo_con_trabajo_personal(self):
        # V0: sin rol/permiso especial (nada de rol hardcodeado), pero ya no
        # para cualquier autenticado: "Trabajo" solo se muestra si hay algo
        # que requiera acción del usuario (aquí, una tarea asignada).
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertNotContains(respuesta, reverse("core:mi_trabajo"))
        crear_tarea(titulo="Trabajo visible", creada_por=self.usuario, usuario_responsable=self.usuario)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertContains(respuesta, "Trabajo")
        self.assertContains(respuesta, reverse("core:mi_trabajo"))

    def test_resto_de_navegacion_no_se_altera(self):
        # Regresión mínima: el gate de Trabajo no alcanza a Cola ni a
        # Administración (siguen ausentes por defecto).
        self.client.login(username="mtrabajo", password=CLAVE_PRUEBA)
        crear_tarea(titulo="Solo tarea", creada_por=self.usuario, usuario_responsable=self.usuario)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertNotContains(respuesta, reverse("tickets:cola"))
        self.assertNotContains(respuesta, "Administración")


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


def _otorgar_permiso_nav(usuario, codigo, tipo_alcance=None, area=None):
    """Concede `codigo` al usuario por el mecanismo normal (Permiso → Rol →
    Asignación): sin roles hardcodeados ni sembrado global."""
    permiso, _ = Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": codigo})
    rol = RolFuncional.objects.create(nombre=f"Rol nav {codigo} {usuario.username}")
    RolPermiso.objects.create(rol=rol, permiso=permiso)
    return AsignacionRol.objects.create(
        usuario=usuario, rol=rol, tipo_alcance=tipo_alcance or AsignacionRol.TipoAlcance.GLOBAL, area=area
    )


class NavegacionGlobalTests(TestCase):
    """V0 — Header + Dock adaptativo + panel "Más" + pestañas de Trabajo.

    Solo comportamiento verificable del servidor: qué destinos recibe cada
    usuario según sus capacidades reales (nunca por nombre de rol). Nada
    sobre colores, tamaños ni CSS."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="nav_v0", password=CLAVE_PRUEBA)
        self.client.login(username="nav_v0", password=CLAVE_PRUEBA)

    def _get(self, vista="core:inicio"):
        return self.client.get(reverse(vista))

    @staticmethod
    def _dock(respuesta):
        return [i["etiqueta"] for i in respuesta.context["nav_dock"]]

    @staticmethod
    def _mas(respuesta):
        return {g["etiqueta"]: [i["etiqueta"] for i in g["items"]] for g in respuesta.context["nav_mas_grupos"]}

    def _con_tarea(self):
        return crear_tarea(titulo="Trabajo pendiente", creada_por=self.usuario, usuario_responsable=self.usuario)

    # --- Escenarios del dock (matriz de la fase V0) ---

    def test_escenario_a_solo_solicita(self):
        respuesta = self._get()
        self.assertEqual(self._dock(respuesta), ["Inicio", "Mis tickets"])
        self.assertEqual(self._mas(respuesta), {})
        self.assertNotContains(respuesta, 'id="dock-more"')

    def test_escenario_b_y_e_gestor_que_no_atiende_ve_mas_pero_no_trabajo(self):
        _otorgar_permiso_nav(self.usuario, "catalogo.administrar")
        respuesta = self._get()
        self.assertEqual(self._dock(respuesta), ["Inicio", "Mis tickets"])
        self.assertEqual(self._mas(respuesta), {"Gestión": ["Diseñador"]})
        self.assertContains(respuesta, 'id="dock-more"')

    def test_escenario_c_atiende_tickets_ve_trabajo_sin_mas(self):
        _otorgar_permiso_nav(self.usuario, "tickets.atender")
        respuesta = self._get()
        self.assertEqual(self._dock(respuesta), ["Inicio", "Mis tickets", "Trabajo"])
        self.assertEqual(self._mas(respuesta), {})
        self.assertNotContains(respuesta, 'id="dock-more"')

    def test_escenario_d_atiende_y_configura(self):
        _otorgar_permiso_nav(self.usuario, "tickets.atender")
        _otorgar_permiso_nav(self.usuario, "catalogo.administrar")
        _otorgar_permiso_nav(self.usuario, "workflows.administrar")
        respuesta = self._get()
        self.assertEqual(self._dock(respuesta), ["Inicio", "Mis tickets", "Trabajo"])
        # Studio y Workflows avanzados ya no son destinos separados: un solo Diseñador.
        self.assertEqual(self._mas(respuesta), {"Gestión": ["Diseñador"]})

    # --- Trabajo ---

    def test_trabajo_con_cola_en_alcance_de_area(self):
        # `tickets.atender` solo en un Área también habilita Trabajo (no se
        # exige alcance global).
        area = Area.objects.create(nombre="Área nav", codigo="AREA-NAV-V0")
        _otorgar_permiso_nav(self.usuario, "tickets.atender", AsignacionRol.TipoAlcance.AREA, area)
        respuesta = self._get()
        self.assertIn("Trabajo", self._dock(respuesta))

    def test_trabajo_solo_personal_abre_mi_trabajo(self):
        self._con_tarea()
        respuesta = self._get()
        trabajo = next(i for i in respuesta.context["nav_dock"] if i["etiqueta"] == "Trabajo")
        self.assertEqual(trabajo["url_name"], "core:mi_trabajo")

    def test_aprobacion_pendiente_habilita_trabajo(self):
        crear_esquema_aprobacion(
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.usuario)],
        )
        self.assertIn("Trabajo", self._dock(self._get()))

    def test_tarea_completada_no_habilita_trabajo(self):
        tarea = self._con_tarea()
        Tarea.objects.filter(pk=tarea.pk).update(estado=Tarea.Estado.COMPLETADA)
        self.assertNotIn("Trabajo", self._dock(self._get()))

    def test_trabajo_abre_cola_cuando_hay_acceso_a_ella(self):
        _otorgar_permiso_nav(self.usuario, "tickets.atender")
        self._con_tarea()
        respuesta = self._get()
        trabajo = next(i for i in respuesta.context["nav_dock"] if i["etiqueta"] == "Trabajo")
        self.assertEqual(trabajo["url_name"], "tickets:cola")

    def test_pestanas_de_trabajo_solo_con_cola_y_trabajo_personal(self):
        # Solo Cola: la pestaña no aporta nada → no se muestra.
        _otorgar_permiso_nav(self.usuario, "tickets.atender")
        respuesta = self._get("tickets:cola")
        self.assertEqual(respuesta.context["nav_trabajo_tabs"], [])
        self.assertNotContains(respuesta, "Secciones de Trabajo")

        # Cola + trabajo personal: dos pestañas, la de la vista actual activa.
        self._con_tarea()
        respuesta = self._get("tickets:cola")
        tabs = respuesta.context["nav_trabajo_tabs"]
        self.assertEqual([(t["etiqueta"], t["activa"]) for t in tabs], [("Cola", True), ("Mi trabajo", False)])
        self.assertContains(respuesta, "Secciones de Trabajo")
        respuesta = self._get("core:mi_trabajo")
        tabs = respuesta.context["nav_trabajo_tabs"]
        self.assertEqual([(t["etiqueta"], t["activa"]) for t in tabs], [("Cola", False), ("Mi trabajo", True)])

    def test_sin_cola_no_hay_pestanas_aunque_haya_trabajo_personal(self):
        self._con_tarea()
        respuesta = self._get("core:mi_trabajo")
        self.assertEqual(respuesta.context["nav_trabajo_tabs"], [])

    # --- Más ---

    def test_el_disenador_se_abre_con_cualquiera_de_sus_capacidades(self):
        _otorgar_permiso_nav(self.usuario, "catalogo.administrar")
        self.assertEqual(self._mas(self._get()), {"Gestión": ["Diseñador"]})

        for codigo in ("workflows.consultar", "workflows.administrar"):
            otro = Usuario.objects.create_user(username=f"nav_d1_{codigo}", password=CLAVE_PRUEBA)
            _otorgar_permiso_nav(otro, codigo)
            self.client.login(username=otro.username, password=CLAVE_PRUEBA)
            self.assertEqual(self._mas(self._get()), {"Gestión": ["Diseñador"]}, codigo)

    def test_vincular_por_si_solo_no_abre_el_disenador(self):
        # Elegir un flujo publicado es una acción DENTRO de configurar un servicio:
        # sin `catalogo.administrar` ni poder consultar flujos no hay nada que hacer aquí.
        _otorgar_permiso_nav(self.usuario, "workflows.vincular")
        self.assertEqual(self._mas(self._get()), {})
        self.assertEqual(self.client.get(reverse("core:disenador")).status_code, 403)

    def test_configuracion_solo_para_is_staff(self):
        self.assertEqual(self._mas(self._get()), {})
        self.usuario.is_staff = True
        self.usuario.save()
        self.assertEqual(self._mas(self._get()), {"Administración": ["Configuración"]})

    def test_mas_no_muestra_grupos_vacios(self):
        _otorgar_permiso_nav(self.usuario, "workflows.administrar")
        self.assertEqual(list(self._mas(self._get())), ["Gestión"])

    def test_el_disenador_no_vive_en_trabajo(self):
        _otorgar_permiso_nav(self.usuario, "tickets.atender")
        _otorgar_permiso_nav(self.usuario, "catalogo.administrar")
        _otorgar_permiso_nav(self.usuario, "workflows.administrar")
        respuesta = self._get("tickets:cola")
        self.assertNotIn("Diseñador", self._dock(respuesta))

    # --- Destino activo ---

    def test_trabajo_se_resalta_en_cola_mi_trabajo_y_detalles_de_tareas(self):
        _otorgar_permiso_nav(self.usuario, "tickets.atender")
        self._con_tarea()
        for vista in ("tickets:cola", "core:mi_trabajo", "tareas:lista"):
            self.assertEqual(self._get(vista).context["nav_item_activo"], "tickets:cola", vista)
        self.assertEqual(self._get("tickets:mis_tickets").context["nav_item_activo"], "tickets:mis_tickets")

    def test_el_recorrido_de_solicitud_no_resalta_mis_tickets(self):
        from apps.tickets.operaciones import crear_borrador
        from apps.tickets.tests import _crear_servicio_con_formulario

        servicio, _version, _campos = _crear_servicio_con_formulario(self.usuario, [])
        ticket = crear_borrador(self.usuario, servicio)
        for nombre in ("tickets:borrador", "tickets:revisar"):
            respuesta = self.client.get(reverse(nombre, args=[ticket.pk]))
            self.assertEqual(respuesta.status_code, 200, nombre)
            self.assertIsNone(respuesta.context["nav_item_activo"], nombre)
        self.assertEqual(self._get("tickets:mis_tickets").context["nav_item_activo"], "tickets:mis_tickets")

    def test_el_disenador_resalta_mas_en_todas_sus_partes_pero_el_catalogo_publico_no(self):
        _otorgar_permiso_nav(self.usuario, "catalogo.administrar")
        _otorgar_permiso_nav(self.usuario, "workflows.administrar")
        for vista in ("core:disenador", "core:disenador_flujos", "core:disenador_servicios",
                      "catalogo:studio_lista", "catalogo:studio_crear", "workflows:lista"):
            respuesta = self._get(vista)
            self.assertEqual(respuesta.status_code, 200, vista)
            self.assertEqual(respuesta.context["nav_item_activo"], "core:disenador", vista)
            self.assertTrue(respuesta.context["nav_mas_activo"], vista)
        respuesta = self._get("catalogo:lista")
        self.assertFalse(respuesta.context["nav_mas_activo"])

    # --- Header ---

    def test_menu_de_usuario_ofrece_perfil_y_cerrar_sesion(self):
        respuesta = self._get()
        contenido = respuesta.content.decode()
        menu = contenido.split('id="menu-usuario"')[1].split("</header>")[0]
        self.assertIn(reverse("core:perfil"), menu)
        self.assertIn('method="post"', menu)
        self.assertIn(reverse("core:logout"), menu)
        self.assertIn("Cerrar sesión", menu)

    def test_header_no_contiene_navegacion_de_modulos(self):
        _otorgar_permiso_nav(self.usuario, "tickets.atender")
        contenido = self._get().content.decode()
        header = contenido.split('<header class="app-header">')[1].split("</header>")[0]
        for vista in ("tickets:mis_tickets", "tickets:cola", "catalogo:lista", "core:mi_trabajo"):
            self.assertNotIn(reverse(vista), header)

    def test_hay_un_solo_dock_y_ningun_sidebar(self):
        contenido = self._get().content.decode()
        self.assertEqual(contenido.count('class="dock"'), 1)
        self.assertNotIn('class="sidebar"', contenido)

    # --- Catálogo visual interno ---

    def test_sistema_visual_exige_autenticacion(self):
        self.client.logout()
        respuesta = self.client.get(reverse("core:sistema_visual"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse("core:login"), respuesta.url)

    def test_sistema_visual_prohibido_para_no_staff(self):
        self.assertEqual(self._get("core:sistema_visual").status_code, 403)

    def test_sistema_visual_disponible_para_staff(self):
        self.usuario.is_staff = True
        self.usuario.save()
        respuesta = self._get("core:sistema_visual")
        self.assertEqual(respuesta.status_code, 200)
        for seccion in ("Fundamentos", "Componentes", "Patrones"):
            self.assertContains(respuesta, seccion)

    # --- Compatibilidad del shell ---

    def test_paginas_de_error_siguen_renderizando(self):
        respuesta = self.client.get("/esta-ruta-no-existe/")
        self.assertEqual(respuesta.status_code, 404)
        self.assertContains(respuesta, "Volver al inicio", status_code=404)


class InicioPortalTests(TestCase):
    """V1 — Inicio / Mi Portal. Solo comportamiento verificable del servidor:
    qué datos reales recibe la página (visibilidad del catálogo, frecuentes,
    agenda, Trabajo, tickets recientes), cómo se reorganiza cuando faltan y
    que el explorador respeta la visibilidad. Nada de CSS ni de texto
    decorativo."""

    def setUp(self):
        from apps.catalogo.models import Categoria, Servicio

        self.usuario = Usuario.objects.create_user(
            username="portal_v1", password=CLAVE_PRUEBA, first_name="Yeimi Paola", last_name="Rojas"
        )
        self.otro = Usuario.objects.create_user(username="portal_otro", password=CLAVE_PRUEBA)
        self.categoria = Categoria.objects.create(nombre="Tecnología")
        self.Servicio = Servicio
        self.Categoria = Categoria
        self.client.login(username="portal_v1", password=CLAVE_PRUEBA)

    # --- helpers ---

    def _servicio(self, nombre, *, categoria=None, activo=True, publico=True, **extra):
        return self.Servicio.objects.create(
            nombre=nombre, categoria=categoria or self.categoria, activo=activo,
            alcance_visibilidad=(
                self.Servicio.AlcanceVisibilidad.PUBLICO_INTERNO if publico
                else self.Servicio.AlcanceVisibilidad.RESTRINGIDO
            ),
            **extra,
        )

    def _inicio(self, **params):
        return self.client.get(reverse("core:inicio"), params)

    def _explorar(self, **params):
        return self.client.get(reverse("core:explorar"), params)

    def _servicio_con_formulario(self):
        from apps.tickets.tests import _crear_servicio_con_formulario

        servicio, _version, _campos = _crear_servicio_con_formulario(self.usuario, [])
        return servicio

    def _ticket(self, servicio, solicitante=None):
        from apps.tickets.operaciones import crear_borrador

        return crear_borrador(solicitante or self.usuario, servicio)

    # --- Hero ---

    def test_saludo_cambia_con_la_hora_y_usa_el_nombre_de_pila(self):
        from datetime import datetime

        from apps.core.inicio import saludo

        def a_las(hora):
            return timezone.make_aware(datetime(2026, 10, 2, hora, 0))

        self.assertEqual(saludo(self.usuario, a_las(8))["momento"], "Buenos días")
        self.assertEqual(saludo(self.usuario, a_las(15))["momento"], "Buenas tardes")
        self.assertEqual(saludo(self.usuario, a_las(21))["momento"], "Buenas noches")
        self.assertEqual(saludo(self.usuario, a_las(8))["nombre"], "Yeimi")

    def test_sin_nombre_de_pila_se_usa_el_usuario(self):
        from apps.core.inicio import saludo

        self.assertEqual(saludo(self.otro)["nombre"], "portal_otro")

    def test_hero_incluye_el_buscador_de_necesidades_con_respaldo_sin_js(self):
        respuesta = self._inicio()
        self.assertContains(respuesta, "data-explorer-search")
        self.assertContains(respuesta, f'action="{reverse("catalogo:lista")}"')
        self.assertContains(respuesta, "¿Qué necesitas hoy?")

    # --- Explorar: catálogo según visibilidad ---

    def test_solo_aparecen_servicios_visibles_activos_y_autorizados(self):
        self._servicio("Visible para todos")
        self._servicio("Servicio apagado", activo=False)
        self._servicio("Restringido sin acceso", publico=False)
        respuesta = self._explorar()
        self.assertContains(respuesta, "Visible para todos")
        self.assertNotContains(respuesta, "Servicio apagado")
        self.assertNotContains(respuesta, "Restringido sin acceso")
        self.assertEqual(self._inicio().context["total_servicios"], 1)

    def test_una_concesion_hace_visible_un_servicio_restringido(self):
        from apps.catalogo.models import ServicioVisibilidad

        servicio = self._servicio("Solo con concesión", publico=False)
        self.assertNotContains(self._explorar(), "Solo con concesión")
        ServicioVisibilidad.objects.create(
            servicio=servicio, tipo_alcance=ServicioVisibilidad.TipoAlcance.USUARIO, usuario=self.usuario
        )
        self.assertContains(self._explorar(), "Solo con concesión")
        # …y sigue oculto para quien no tiene la concesión.
        self.client.login(username="portal_otro", password=CLAVE_PRUEBA)
        self.assertNotContains(self._explorar(), "Solo con concesión")

    def test_el_explorador_busca_por_nombre_descripcion_o_categoria(self):
        self._servicio("Soporte de equipos", descripcion="Reparación de laptops y monitores")
        self._servicio("Compra de papelería", categoria=self.Categoria.objects.create(nombre="Compras"))
        self.assertContains(self._explorar(q="laptops"), "Soporte de equipos")
        self.assertContains(self._explorar(q="soporte"), "Soporte de equipos")
        respuesta = self._explorar(q="compras")  # por nombre de categoría
        self.assertContains(respuesta, "Compra de papelería")
        self.assertNotContains(respuesta, "Soporte de equipos")
        self.assertContains(self._explorar(q="zzz-nada"), "No encontramos nada")

    def test_el_explorador_filtra_por_categoria_y_tolera_entradas_invalidas(self):
        otra = self.Categoria.objects.create(nombre="Compras")
        self._servicio("Servicio de tecnología")
        self._servicio("Servicio de compras", categoria=otra)
        respuesta = self._explorar(categoria=otra.pk)
        self.assertContains(respuesta, "Servicio de compras")
        self.assertNotContains(respuesta, "Servicio de tecnología")
        for basura in ("abc", "", "0", "99999"):
            self.assertEqual(self._explorar(categoria=basura).status_code, 200, basura)

    def test_el_explorador_exige_autenticacion(self):
        self.client.logout()
        respuesta = self._explorar()
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse("core:login"), respuesta.url)

    def test_el_explorador_acota_los_resultados(self):
        from apps.core.inicio import LIMITE_RESULTADOS_EXPLORADOR

        for i in range(LIMITE_RESULTADOS_EXPLORADOR + 2):
            self._servicio(f"Servicio masivo {i:03d}")
        respuesta = self._explorar()
        self.assertContains(respuesta, 'class="svc-card"', count=LIMITE_RESULTADOS_EXPLORADOR)
        self.assertContains(respuesta, f"Mostrando los primeros {LIMITE_RESULTADOS_EXPLORADOR}")

    def test_las_tarjetas_entran_directo_a_la_solicitud_sin_ficha_intermedia(self):
        servicio = self._servicio("Soporte de equipos")
        respuesta = self._explorar()
        self.assertContains(respuesta, reverse("tickets:solicitar", args=[servicio.pk]))
        self.assertNotContains(respuesta, reverse("catalogo:detalle", args=[servicio.pk]))

    def test_el_explorador_solo_acepta_get(self):
        self.assertEqual(self.client.post(reverse("core:explorar")).status_code, 405)

    def test_inicio_muestra_pocas_categorias_y_ver_todo(self):
        from apps.core.inicio import LIMITE_CATEGORIAS_TILES

        for i in range(LIMITE_CATEGORIAS_TILES + 2):
            self._servicio(f"Servicio {i}", categoria=self.Categoria.objects.create(nombre=f"Categoría {i}"))
        respuesta = self._inicio()
        self.assertEqual(len(respuesta.context["categorias_tiles"]), LIMITE_CATEGORIAS_TILES)
        self.assertEqual(len(respuesta.context["categorias"]), LIMITE_CATEGORIAS_TILES + 2)
        self.assertContains(respuesta, 'id="explorador"')
        self.assertContains(respuesta, "Ver todo")

    def test_inicio_no_lista_servicios_ni_consulta_por_tarjeta(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._servicio("Primero")
        self._inicio()  # calentamiento (cachés de contenido/sesión)
        with CaptureQueriesContext(connection) as pocas:
            self._inicio()
        for i in range(15):
            self._servicio(f"Otro {i}")
        with CaptureQueriesContext(connection) as muchas:
            respuesta = self._inicio()
        self.assertEqual(len(pocas), len(muchas))
        # El catálogo completo no viaja en la portada: se pide al explorador.
        self.assertNotContains(respuesta, "Otro 7")

    def test_sin_servicios_disponibles_hay_estado_vacio_y_no_explorador(self):
        respuesta = self._inicio()
        self.assertEqual(respuesta.context["total_servicios"], 0)
        # El vacío forma parte de la composición (estado visual propio)...
        self.assertContains(respuesta, 'class="portal-empty"')
        self.assertContains(respuesta, "Aún no hay nada para solicitar")
        self.assertNotContains(respuesta, 'id="explorador"')
        self.assertNotContains(respuesta, "Ver todo")

    def test_con_servicios_el_estado_vacio_desaparece(self):
        self._servicio("Algo disponible")
        respuesta = self._inicio()
        self.assertNotContains(respuesta, "portal-empty")
        self.assertContains(respuesta, "cat-chip")

    # --- Frecuentes y recientes ---

    def test_sin_historial_no_hay_seccion_de_usados(self):
        self._servicio("Algo disponible")
        respuesta = self._inicio()
        self.assertFalse(respuesta.context["tiene_usados"])
        self.assertNotContains(respuesta, "Tus habituales")
        self.assertNotContains(respuesta, "Usados recientemente")

    def test_un_uso_es_reciente_y_dos_son_frecuentes(self):
        servicio = self._servicio_con_formulario()
        self._ticket(servicio)
        respuesta = self._inicio()
        self.assertEqual(respuesta.context["frecuentes"], [])
        self.assertEqual(respuesta.context["recientes"], [servicio])
        self.assertContains(respuesta, "Usados recientemente")

        self._ticket(servicio)
        respuesta = self._inicio()
        self.assertEqual(respuesta.context["frecuentes"], [servicio])
        self.assertEqual(respuesta.context["recientes"], [])
        self.assertContains(respuesta, "Tus habituales")

    def test_los_usados_son_solo_los_propios_y_siguen_la_visibilidad(self):
        servicio = self._servicio_con_formulario()
        self._ticket(servicio, solicitante=self.otro)
        self.assertFalse(self._inicio().context["tiene_usados"])
        self._ticket(servicio)
        self.assertTrue(self._inicio().context["tiene_usados"])
        # Un servicio que dejó de estar disponible ya no se ofrece.
        self.Servicio.objects.filter(pk=servicio.pk).update(activo=False)
        self.assertFalse(self._inicio().context["tiene_usados"])

    # --- Tickets recientes ---

    def test_tickets_recientes_son_los_propios_con_limite_y_sin_cancelados(self):
        from apps.core.inicio import LIMITE_TICKETS_RECIENTES
        from apps.tickets.models import Ticket

        servicio = self._servicio_con_formulario()
        propios = [self._ticket(servicio) for _ in range(LIMITE_TICKETS_RECIENTES + 2)]
        ajeno = self._ticket(servicio, solicitante=self.otro)
        cancelado = propios[-1]
        Ticket.objects.filter(pk=cancelado.pk).update(estado=Ticket.Estado.CANCELADO)
        respuesta = self._inicio()
        ids = [t.pk for t in respuesta.context["tickets_recientes"]]
        self.assertEqual(len(ids), LIMITE_TICKETS_RECIENTES)
        self.assertNotIn(ajeno.pk, ids)
        self.assertNotIn(cancelado.pk, ids)
        self.assertContains(respuesta, "Tus tickets recientes")
        self.assertContains(respuesta, reverse("tickets:mis_tickets"))

    def test_un_borrador_enlaza_a_su_formulario(self):
        servicio = self._servicio_con_formulario()
        ticket = self._ticket(servicio)
        self.assertContains(self._inicio(), reverse("tickets:borrador", args=[ticket.pk]))

    def test_sin_tickets_no_hay_tarjeta_vacia(self):
        respuesta = self._inicio()
        self.assertEqual(respuesta.context["tickets_recientes"], [])
        self.assertNotContains(respuesta, "Tus tickets recientes")

    # --- Tu trabajo ---

    def test_sin_capacidad_de_trabajo_no_hay_resumen(self):
        respuesta = self._inicio()
        self.assertIsNone(respuesta.context["trabajo"])
        self.assertNotContains(respuesta, "Tu trabajo")
        self.assertNotContains(respuesta, "Todo al día")

    def test_resumen_cuenta_pendientes_reales_y_apunta_a_mi_trabajo(self):
        crear_tarea(titulo="Mi tarea", creada_por=self.usuario, usuario_responsable=self.usuario)
        hecha = crear_tarea(titulo="Hecha", creada_por=self.usuario, usuario_responsable=self.usuario)
        Tarea.objects.filter(pk=hecha.pk).update(estado=Tarea.Estado.COMPLETADA)
        crear_esquema_aprobacion(
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.usuario)],
        )
        respuesta = self._inicio()
        trabajo = respuesta.context["trabajo"]
        self.assertEqual((trabajo["tareas"], trabajo["aprobaciones"]), (1, 1))
        self.assertFalse(trabajo["acceso_cola"])
        self.assertEqual(trabajo["url"], reverse("core:mi_trabajo"))
        self.assertContains(respuesta, "Tu trabajo")
        self.assertNotContains(respuesta, reverse("tickets:cola"))

    def test_con_capacidad_pero_sin_pendientes_solo_hay_una_senal_discreta(self):
        # Sin pendientes a su nombre no se pinta la franja de Trabajo: solo
        # "Todo al día" con el acceso. La Cola no se cuenta.
        _otorgar_permiso_nav(self.usuario, "tickets.atender")
        respuesta = self._inicio()
        trabajo = respuesta.context["trabajo"]
        self.assertTrue(trabajo["acceso_cola"])
        self.assertFalse(trabajo["hay_pendientes"])
        self.assertEqual(trabajo["url"], reverse("tickets:cola"))
        self.assertContains(respuesta, "Todo al día")
        self.assertNotContains(respuesta, "Tu trabajo")
        self.assertNotContains(respuesta, "work-pills")
        self.assertNotContains(respuesta, "Ir a Trabajo")  # el dock ya navega a Trabajo

    def test_con_pendientes_y_cola_el_resumen_incluye_la_cola_sin_contarla(self):
        _otorgar_permiso_nav(self.usuario, "tickets.atender")
        crear_tarea(titulo="Pendiente", creada_por=self.usuario, usuario_responsable=self.usuario)
        respuesta = self._inicio()
        trabajo = respuesta.context["trabajo"]
        self.assertTrue(trabajo["hay_pendientes"])
        self.assertEqual(trabajo["url"], reverse("tickets:cola"))
        self.assertContains(respuesta, "Tu trabajo")
        self.assertContains(respuesta, "Cola de atención")
        self.assertContains(respuesta, "Ir a Trabajo")
        self.assertNotContains(respuesta, "Todo al día")

    def test_ritmo_de_la_pagina_hero_cuerpo_trabajo_recientes(self):
        # Retícula 1 → 2 → 1 → 1 columnas. El orden del DOM es también el
        # orden móvil: hero, explorar, tu día, trabajo, recientes.
        self._servicio("Algo disponible")
        self._ticket(self._servicio_con_formulario())
        crear_tarea(titulo="Pendiente", creada_por=self.usuario, usuario_responsable=self.usuario)
        html = self._inicio().content.decode()
        marcas = [
            'class="portal-hero"', 'class="portal-body"', 'id="t-explorar"', 'id="t-agenda"',
            'class="portal-trabajo"', 'class="portal-recientes"',
        ]
        posiciones = [html.index(m) for m in marcas]
        self.assertEqual(posiciones, sorted(posiciones))
        # Tu día pertenece al cuerpo de dos columnas, no al hero.
        hero = html[html.index('class="portal-hero"'):html.index('class="portal-body"')]
        self.assertNotIn("Tu día", hero)
        self.assertNotIn("portal-agenda", hero)

    def test_sin_pendientes_ni_tickets_no_hay_bandas(self):
        html = self._inicio().content.decode()
        self.assertNotIn('class="portal-trabajo"', html)
        self.assertNotIn('class="portal-recientes"', html)

    def test_inicio_es_una_sola_pagina_sin_paneles_administrativos(self):
        # Gestionar el catálogo no agrega nada a Inicio (vive en Más › Studio).
        _otorgar_permiso_nav(self.usuario, "catalogo.administrar")
        respuesta = self._inicio()
        self.assertIsNone(respuesta.context["trabajo"])
        self.assertFalse(respuesta.context["agenda"]["hay_eventos"])

    # --- Tu día / agenda ---

    def _a_las(self, dia, hora=12, mes=10):
        from datetime import datetime

        return timezone.make_aware(datetime(2026, mes, dia, hora, 0))

    def test_el_calendario_es_permanente_pero_sin_datos_ficticios(self):
        # Tu día siempre muestra el mes; marcadores y lista salen solo de
        # datos reales (aquí no hay ninguna fecha).
        crear_tarea(titulo="Sin fecha", creada_por=self.usuario, usuario_responsable=self.usuario)
        respuesta = self._inicio()
        agenda_ctx = respuesta.context["agenda"]
        self.assertFalse(agenda_ctx["hay_eventos"])
        self.assertEqual(agenda_ctx["proximos"], [])
        self.assertTrue(agenda_ctx["semanas"])
        self.assertContains(respuesta, "Tu día")
        self.assertContains(respuesta, 'aria-current="date"')
        self.assertContains(respuesta, "Sin fechas pendientes.")
        self.assertNotContains(respuesta, "cal__marca")

    def test_la_fecha_vive_en_tu_dia_y_no_en_el_hero(self):
        respuesta = self._inicio()
        saludo_ctx = respuesta.context["saludo"]
        self.assertEqual(
            set(saludo_ctx), {"momento", "nombre", "dia", "dia_semana", "mes_anio"}
        )
        self.assertContains(respuesta, 'class="agenda-today__num"')
        self.assertNotContains(respuesta, "portal-hero__date")

    def test_agenda_usa_fechas_limite_de_tareas_propias_pendientes(self):
        from apps.core.inicio import agenda

        ahora = self._a_las(2)
        crear_tarea(
            titulo="Entregar informe", creada_por=self.usuario, usuario_responsable=self.usuario,
            fecha_limite=self._a_las(5, 10),
        )
        hecha = crear_tarea(
            titulo="Ya hecha", creada_por=self.usuario, usuario_responsable=self.usuario,
            fecha_limite=self._a_las(6),
        )
        Tarea.objects.filter(pk=hecha.pk).update(estado=Tarea.Estado.COMPLETADA)
        crear_tarea(
            titulo="De otra persona", creada_por=self.otro, usuario_responsable=self.otro,
            fecha_limite=self._a_las(7),
        )
        resultado = agenda(self.usuario, ahora=ahora)
        self.assertEqual([e["titulo"] for e in resultado["proximos"]], ["Entregar informe"])
        self.assertEqual(resultado["proximos"][0]["tipo"], "tarea")
        # El día 5 queda marcado en el mes; los demás no.
        marcados = [d["dia"] for semana in resultado["semanas"] for d in semana if d["n"] and d["en_mes"]]
        self.assertEqual(marcados, [5])

    def test_el_calendario_es_un_mes_con_semanas_desde_el_lunes(self):
        from apps.core.inicio import agenda

        crear_tarea(
            titulo="Algo", creada_por=self.usuario, usuario_responsable=self.usuario, fecha_limite=self._a_las(20)
        )
        resultado = agenda(self.usuario, ahora=self._a_las(2))
        self.assertEqual(resultado["titulo_mes"], "Octubre 2026")
        primera = resultado["semanas"][0]
        self.assertEqual(len(primera), 7)
        self.assertFalse(primera[0]["en_mes"])  # lunes 28 de septiembre
        self.assertEqual((primera[3]["dia"], primera[3]["en_mes"]), (1, True))  # jueves 1 de octubre
        self.assertEqual([d["dia"] for s in resultado["semanas"] for d in s if d["hoy"]], [2])
        self.assertEqual((resultado["mes_anterior"], resultado["mes_siguiente"]), ("2026-09", "2026-11"))

    def test_tareas_vencidas_se_cuentan_aparte(self):
        from apps.core.inicio import agenda

        crear_tarea(
            titulo="Atrasada", creada_por=self.usuario, usuario_responsable=self.usuario,
            fecha_limite=self._a_las(1, 8),
        )
        resultado = agenda(self.usuario, ahora=self._a_las(2))
        self.assertEqual(resultado["vencidas"], 1)
        self.assertEqual(resultado["proximos"], [])

    def test_agenda_incluye_el_plazo_de_responder_una_entrega(self):
        from apps.core.inicio import agenda
        from apps.tickets.models import EntregaTicket

        ticket = self._ticket(self._servicio_con_formulario())
        ajeno = self._ticket(self._servicio_con_formulario(), solicitante=self.otro)
        ahora = timezone.now()
        for t, estado in ((ticket, EntregaTicket.Estado.PENDIENTE), (ajeno, EntregaTicket.Estado.PENDIENTE)):
            EntregaTicket.objects.create(
                ticket=t, numero=1, entregada_por=self.otro, entregada_en=ahora,
                politica="PERIODO_OBSERVACIONES", dias_observacion=3, vence_en=ahora + timedelta(days=2),
                estado=estado,
            )
        resultado = agenda(self.usuario, ahora=ahora)
        self.assertEqual([e["tipo"] for e in resultado["proximos"]], ["entrega"])
        self.assertEqual(resultado["proximos"][0]["url"], reverse("tickets:detalle", args=[ticket.pk]))

    def test_navegar_a_otro_mes_conserva_la_agenda_aunque_este_vacia(self):
        crear_tarea(
            titulo="Hoy mismo", creada_por=self.usuario, usuario_responsable=self.usuario,
            fecha_limite=timezone.now() + timedelta(hours=1),
        )
        respuesta = self._inicio(mes="2031-01")
        self.assertEqual(respuesta.context["agenda"]["titulo_mes"], "Enero 2031")
        self.assertFalse(respuesta.context["agenda"]["es_mes_actual"])
        # Valores inválidos o fuera de rango caen en el mes actual.
        for basura in ("xx", "2026-13", "1900-01", "2026"):
            self.assertTrue(self._inicio(mes=basura).context["agenda"]["es_mes_actual"], basura)

    def test_con_fechas_reales_el_calendario_es_accesible_y_lista_lo_proximo(self):
        crear_tarea(
            titulo="Con fecha", creada_por=self.usuario, usuario_responsable=self.usuario,
            fecha_limite=timezone.now() + timedelta(hours=1),
        )
        respuesta = self._inicio()
        self.assertContains(respuesta, 'aria-current="date"')
        self.assertContains(respuesta, 'scope="col"')
        self.assertContains(respuesta, "Con fecha")
        self.assertTrue(respuesta.context["agenda"]["hay_eventos"])

    # --- Rutas y compatibilidad ---

    def test_rutas_de_inicio_resuelven(self):
        self.assertEqual(self._inicio().status_code, 200)
        self.assertEqual(reverse("core:explorar"), "/explorar/")


class DisenadorTests(TestCase):
    """D1 — Diseñador: entrada unificada (Flujos + Servicios). Solo
    comportamiento verificable del servidor: quién entra, qué sección y qué
    acciones ve según capacidades reales (nunca por nombre de rol), qué datos
    reales recibe cada pantalla y que las pantallas heredadas conservan su
    camino de vuelta. Nada de estilos."""

    def setUp(self):
        from apps.catalogo.models import Categoria, Servicio
        from apps.workflows.models import Workflow, WorkflowVersion

        self.usuario = Usuario.objects.create_user(username="dis_d1", password=CLAVE_PRUEBA)
        self.client.login(username="dis_d1", password=CLAVE_PRUEBA)
        self.categoria = Categoria.objects.create(nombre="Comunicaciones")
        self.Servicio, self.Workflow, self.WorkflowVersion = Servicio, Workflow, WorkflowVersion

    # --- helpers ---

    def _flujo(self, nombre, *, publicado=True, en_diseno=False):
        flujo = self.Workflow.objects.create(nombre=nombre)
        numero = 1
        if publicado:
            version = self.WorkflowVersion.objects.create(
                workflow=flujo, numero=numero, estado=self.WorkflowVersion.Estado.ACTIVA
            )
            flujo.version_activa = version
            flujo.save()
            numero += 1
        if en_diseno or not publicado:
            self.WorkflowVersion.objects.create(
                workflow=flujo, numero=numero, estado=self.WorkflowVersion.Estado.BORRADOR
            )
        return flujo

    def _servicio(self, nombre, *, activo=True, flujo=None, tipo=None):
        return self.Servicio.objects.create(
            nombre=nombre, categoria=self.categoria, activo=activo, workflow=flujo,
            tipo=tipo or self.Servicio.Tipo.SERVICIO,
            alcance_visibilidad=self.Servicio.AlcanceVisibilidad.PUBLICO_INTERNO,
        )

    def _con(self, *codigos):
        for codigo in codigos:
            _otorgar_permiso_nav(self.usuario, codigo)

    def _get(self, nombre):
        return self.client.get(reverse(f"core:{nombre}"))

    @staticmethod
    def _claves(respuesta):
        return [t["clave"] for t in respuesta.context["disenador_tabs"]]

    # --- quién entra y qué sección ve ---

    def test_sin_capacidades_no_entra_a_ninguna_pantalla(self):
        for nombre in ("disenador", "disenador_flujos", "disenador_servicios"):
            self.assertEqual(self._get(nombre).status_code, 403, nombre)

    def test_exige_autenticacion(self):
        self.client.logout()
        respuesta = self._get("disenador")
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse("core:login"), respuesta.url)

    def test_catalogo_administrar_ve_solo_servicios(self):
        self._con("catalogo.administrar")
        respuesta = self._get("disenador")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self._claves(respuesta), ["inicio", "servicios"])
        self.assertContains(respuesta, reverse("core:disenador_servicio_crear"))
        self.assertNotContains(respuesta, reverse("flujos:nuevo"))
        self.assertEqual(self._get("disenador_servicios").status_code, 200)
        self.assertEqual(self._get("disenador_flujos").status_code, 403)

    def test_consultar_flujos_ve_solo_flujos_y_en_solo_lectura(self):
        self._con("workflows.consultar")
        flujo = self._flujo("Atención estándar")
        respuesta = self._get("disenador_flujos")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self._claves(respuesta), ["inicio", "flujos"])
        self.assertContains(respuesta, "Atención estándar")
        self.assertContains(respuesta, reverse("flujos:lienzo", args=[flujo.pk]))  # abrir el flujo
        self.assertNotContains(respuesta, reverse("workflows:detalle", args=[flujo.pk]))  # sin vista tecnica
        self.assertNotContains(respuesta, reverse("flujos:nuevo"))  # sin "Nuevo flujo"
        self.assertEqual(self._get("disenador_servicios").status_code, 403)
        self.assertNotContains(self._get("disenador"), reverse("core:disenador_servicio_crear"))

    def test_administrar_flujos_ve_nuevo_flujo_y_workspace_visual(self):
        self._con("workflows.administrar")
        self._flujo("Atención estándar")
        for nombre in ("disenador", "disenador_flujos"):
            self.assertContains(self._get(nombre), reverse("flujos:nuevo"), msg_prefix=nombre)

    def test_vincular_sin_consultar_solo_ve_flujos_publicados_y_sin_acceso_a_su_definicion(self):
        self._con("catalogo.administrar", "workflows.vincular")
        publicado = self._flujo("Flujo publicado")
        self._flujo("Flujo en borrador", publicado=False)
        respuesta = self._get("disenador_flujos")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual([f.nombre for f in respuesta.context["flujos"]], ["Flujo publicado"])
        self.assertNotContains(respuesta, "Flujo en borrador")
        self.assertNotContains(respuesta, reverse("workflows:detalle", args=[publicado.pk]))
        self.assertNotContains(respuesta, reverse("flujos:lienzo", args=[publicado.pk]))
        self.assertNotContains(respuesta, reverse("flujos:nuevo"))
        self.assertNotContains(respuesta, "En diseño")

    def test_quien_administra_servicios_y_flujos_ve_ambas_secciones(self):
        self._con("catalogo.administrar", "workflows.administrar")
        self.assertEqual(self._claves(self._get("disenador")), ["inicio", "flujos", "servicios"])

    # --- datos reales ---

    def test_la_biblioteca_muestra_estado_version_y_uso(self):
        self._con("workflows.consultar")
        compartido = self._flujo("Compartido", en_diseno=True)
        self._servicio("Servicio uno", flujo=compartido)
        self._servicio("Servicio dos", flujo=compartido)
        self._flujo("Sin publicar", publicado=False)
        respuesta = self._get("disenador_flujos")
        por_nombre = {f.nombre: f for f in respuesta.context["flujos"]}
        self.assertEqual(por_nombre["Compartido"].n_servicios, 2)
        self.assertEqual(sorted(s.nombre for s in por_nombre["Compartido"].usado_por), ["Servicio dos", "Servicio uno"])
        self.assertTrue(por_nombre["Compartido"].tiene_borrador)
        self.assertEqual(por_nombre["Sin publicar"].n_servicios, 0)
        self.assertContains(respuesta, "Usado por 2 servicios o procesos")
        self.assertContains(respuesta, "Sin usar todavía")
        self.assertContains(respuesta, "Publicado · v1")
        self.assertContains(respuesta, "Sin publicar")
        self.assertContains(respuesta, "Borrador en diseño")
        self.assertContains(respuesta, "Estructuras reutilizables para organizar cómo se realiza el trabajo.")
        self.assertContains(respuesta, "Abrir")

    def test_la_lista_de_servicios_muestra_tipo_estado_y_flujo(self):
        self._con("catalogo.administrar")
        flujo = self._flujo("Flujo de compras")
        con_flujo = self._servicio("Compra de papelería", flujo=flujo)
        self._servicio("Soporte simple", activo=False)
        self._servicio("Proceso sin flujo", activo=False, tipo=self.Servicio.Tipo.PROCESO)
        respuesta = self._get("disenador_servicios")
        self.assertContains(respuesta, "Flujo: Flujo de compras")
        self.assertContains(respuesta, "Sin flujo")
        self.assertContains(respuesta, "Un proceso necesita un flujo para poder publicarse.")
        self.assertContains(respuesta, reverse("core:disenador_servicio", args=[con_flujo.pk]))
        self.assertContains(respuesta, "Borrador")
        self.assertContains(respuesta, "Publicado")

    def test_el_inicio_resume_cifras_y_lo_que_esta_en_curso(self):
        from apps.core.disenador import LIMITE_RECIENTES

        self._con("catalogo.administrar", "workflows.administrar")
        self._flujo("Publicado")
        self._flujo("En curso", publicado=False)
        self._servicio("Publicado uno")
        for i in range(LIMITE_RECIENTES + 2):
            self._servicio(f"Borrador {i}", activo=False)
        respuesta = self._get("disenador")
        self.assertEqual(
            (respuesta.context["flujos"]["publicados"], respuesta.context["flujos"]["en_diseno"]), (1, 1)
        )
        self.assertEqual(
            (respuesta.context["servicios"]["publicados"], respuesta.context["servicios"]["borradores"]),
            (1, LIMITE_RECIENTES + 2),
        )
        self.assertEqual(len(respuesta.context["servicios"]["recientes"]), LIMITE_RECIENTES)
        self.assertEqual([f.nombre for f in respuesta.context["flujos"]["recientes"]], ["En curso"])
        self.assertContains(respuesta, "Continúa donde lo dejaste")

    def test_estados_vacios_segun_la_capacidad(self):
        self._con("catalogo.administrar", "workflows.administrar")
        respuesta = self._get("disenador")
        self.assertContains(respuesta, "Todavía no hay flujos. Crea el primero")
        self.assertContains(respuesta, "Todavía no hay servicios ni procesos")
        self.assertNotContains(respuesta, "Continúa donde lo dejaste")
        self.assertContains(self._get("disenador_flujos"), "Crea el primer flujo para empezar")
        self.assertContains(self._get("disenador_servicios"), "Aún no hay servicios ni procesos")

    def test_sin_flujos_publicados_quien_solo_consulta_no_recibe_invitacion_a_crear(self):
        self._con("workflows.consultar")
        respuesta = self._get("disenador_flujos")
        self.assertContains(respuesta, "Todavía no hay flujos creados.")
        self.assertNotContains(respuesta, "Crea el primero")

    def test_el_lenguaje_visible_no_expone_terminos_tecnicos(self):
        self._con("catalogo.administrar", "workflows.administrar")
        self._flujo("Flujo")
        for nombre in ("disenador", "disenador_flujos", "disenador_servicios"):
            html = self._get(nombre).content.decode()
            for tecnico in ("WorkflowVersion", "Etapa", "Transición", "InstanciaWorkflow"):
                self.assertNotIn(tecnico, html, (nombre, tecnico))

    # --- pantallas heredadas ---

    def test_las_pantallas_heredadas_vuelven_al_disenador(self):
        self._con("catalogo.administrar", "workflows.administrar")
        flujo = self._flujo("Flujo")
        servicio = self._servicio("Servicio")
        destinos = [
            reverse("catalogo:studio_lista"),
            reverse("catalogo:studio_crear"),
            reverse("catalogo:studio", args=[servicio.pk]),
            reverse("workflows:lista"),
            reverse("workflows:crear"),
            reverse("workflows:detalle", args=[flujo.pk]),
            reverse("flujos:nuevo"),
            reverse("flujos:lienzo", args=[flujo.pk]),
        ]
        for url in destinos:
            respuesta = self.client.get(url)
            self.assertEqual(respuesta.status_code, 200, url)
            self.assertContains(respuesta, f'href="{reverse("core:disenador")}"', msg_prefix=url)
