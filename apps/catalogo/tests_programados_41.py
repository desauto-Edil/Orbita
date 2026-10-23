"""Bloque 4.1 — bloqueos funcionales del Proceso programado en el Studio.

H1: con un EQUIPO como responsable inicial, publicar exige que alguien pueda TOMAR sus ejecuciones (`tickets.atender`
GLOBAL, o un miembro activo del equipo con ese permiso); una PERSONA las inicia ella misma. H2: el responsable
inicial de una programación activa no se retira desde Responsables. Reutilizan el escenario de
`tests_coherencia`. Se entregan SIN ejecutar.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.catalogo import estado_basico
from apps.catalogo.models import ProgramacionProceso, ServicioResponsable
from apps.catalogo.operaciones import (
    MENSAJE_SIN_ATENCION_PROGRAMADO,
    activar_servicio,
    retirar_responsable,
    validar_publicacion,
)
from apps.catalogo.programacion import asegurar_responsable_inicial, configurar_programacion, desactivar_programacion
from apps.catalogo.tests_coherencia import _BaseCoherencia
from apps.catalogo.tests_fase4a import CLAVE, Usuario
from apps.core.models import (
    Area,
    AsignacionRol,
    Equipo,
    MiembroEquipo,
    Permiso,
    RolFuncional,
    RolPermiso,
)


class _BaseProgramado(_BaseCoherencia):
    def _preparar_programado(self):
        self._preparar()
        self.proceso = self._proceso("Programado 4.1")
        self.miembro = Usuario.objects.create_user("b41_miembro", password=CLAVE)
        self.equipo = Equipo.objects.create(nombre="Equipo 4.1")
        self.membresia = MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.miembro)

    def _sin_atencion_global(self):
        AsignacionRol.objects.filter(usuario=self.atencion_global).update(activo=False)

    def _dar_atender_en_area(self, usuario):
        area = Area.objects.create(nombre=f"Área {usuario.username}")
        permiso, _ = Permiso.objects.get_or_create(codigo="tickets.atender", defaults={"nombre": "Atender"})
        rol = RolFuncional.objects.create(nombre=f"Rol {usuario.username}")
        RolPermiso.objects.create(rol=rol, permiso=permiso)
        AsignacionRol.objects.create(usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.AREA, area=area)

    def _programar(self, tipo, referencia):
        responsable = asegurar_responsable_inicial(self.proceso, self.admin, (tipo, referencia))
        configurar_programacion(
            self.proceso, self.admin, dia_creacion=1, periodo=ProgramacionProceso.Periodo.MES_ACTUAL,
            responsable_inicial=responsable,
        )
        self.proceso.refresh_from_db()
        return responsable

    def _inicio(self):
        return {a.clave: a for a in estado_basico.apartados(self.proceso)}["inicio"]


class EquipoResponsableInicialTests(_BaseProgramado, TestCase):
    def setUp(self):
        self._preparar_programado()

    def test_un_equipo_sin_nadie_que_pueda_tomar_impide_publicar(self):
        self._sin_atencion_global()
        self._programar(ServicioResponsable.TipoResponsable.EQUIPO, self.equipo)
        _estados, ev = self._estados(self.proceso)
        self.assertIn(MENSAJE_SIN_ATENCION_PROGRAMADO, ev.bloqueos)
        self.assertIn("general", ev.por_seccion)  # se corrige en Básico
        self.assertEqual(self._inicio().estado, estado_basico.ATENCION)
        with self.assertRaises(ValidationError):
            activar_servicio(self.proceso, self.admin)

    def test_un_miembro_con_permiso_en_algun_alcance_basta(self):
        self._sin_atencion_global()
        self._dar_atender_en_area(self.miembro)
        self._programar(ServicioResponsable.TipoResponsable.EQUIPO, self.equipo)
        _estados, ev = self._estados(self.proceso)
        self.assertNotIn(MENSAJE_SIN_ATENCION_PROGRAMADO, ev.bloqueos)
        self.assertEqual(self._inicio().estado, estado_basico.COMPLETO)

    def test_un_miembro_retirado_del_equipo_no_cuenta(self):
        self._sin_atencion_global()
        self._dar_atender_en_area(self.miembro)
        MiembroEquipo.objects.filter(pk=self.membresia.pk).update(activo=False)
        self._programar(ServicioResponsable.TipoResponsable.EQUIPO, self.equipo)
        _estados, ev = self._estados(self.proceso)
        self.assertIn(MENSAJE_SIN_ATENCION_PROGRAMADO, ev.bloqueos)

    def test_la_atencion_global_basta(self):
        self._programar(ServicioResponsable.TipoResponsable.EQUIPO, self.equipo)  # `atencion_global` sigue activo
        _estados, ev = self._estados(self.proceso)
        self.assertNotIn(MENSAJE_SIN_ATENCION_PROGRAMADO, ev.bloqueos)

    def test_una_persona_inicia_sus_ejecuciones_sin_otro_permiso(self):
        self._sin_atencion_global()
        self._programar(ServicioResponsable.TipoResponsable.USUARIO, self.persona)
        _estados, ev = self._estados(self.proceso)
        self.assertNotIn(MENSAJE_SIN_ATENCION_PROGRAMADO, ev.bloqueos)

    def test_lo_ya_publicado_y_la_generacion_no_cambian(self):
        # Es requisito de PUBLICAR (`errores_de_acceso`), no de `validar_publicacion`, que usa la generación.
        self._sin_atencion_global()
        self._programar(ServicioResponsable.TipoResponsable.EQUIPO, self.equipo)
        try:
            validar_publicacion(self.proceso)
        except ValidationError as exc:
            self.assertNotIn(MENSAJE_SIN_ATENCION_PROGRAMADO, exc.messages)


class RetirarResponsableInicialTests(_BaseProgramado, TestCase):
    def setUp(self):
        self._preparar_programado()
        self.inicial = self._programar(ServicioResponsable.TipoResponsable.USUARIO, self.persona)
        self.otro = ServicioResponsable.objects.create(
            servicio=self.proceso, tipo_responsable=ServicioResponsable.TipoResponsable.EQUIPO, equipo=self.equipo
        )

    def test_el_responsable_inicial_no_se_retira(self):
        with self.assertRaises(ValidationError):
            retirar_responsable(self.inicial, self.admin)
        self.inicial.refresh_from_db()
        self.assertTrue(self.inicial.activo)
        self.assertEqual(self._inicio().estado, estado_basico.COMPLETO)

    def test_otro_responsable_si_se_retira(self):
        retirar_responsable(self.otro, self.admin)
        self.otro.refresh_from_db()
        self.assertFalse(self.otro.activo)

    def test_con_la_programacion_pausada_si_se_retira(self):
        desactivar_programacion(self.proceso, self.admin)
        retirar_responsable(self.inicial, self.admin)
        self.inicial.refresh_from_db()
        self.assertFalse(self.inicial.activo)

    def test_al_cambiar_el_responsable_inicial_el_anterior_se_puede_retirar(self):
        configurar_programacion(
            self.proceso, self.admin, dia_creacion=1, periodo=ProgramacionProceso.Periodo.MES_ACTUAL,
            responsable_inicial=self.otro,
        )
        retirar_responsable(self.inicial, self.admin)
        self.inicial.refresh_from_db()
        self.assertFalse(self.inicial.activo)

    def test_basico_lo_marca_y_la_vista_lo_rechaza(self):
        self.client.force_login(self.admin)
        url = reverse("catalogo:studio", args=[self.proceso.pk]) + "?tab=general"
        pagina = self.client.get(url)
        self.assertContains(pagina, "Para retirarlo, cambia el responsable inicial en Inicio")
        self.assertNotContains(
            pagina, reverse("catalogo:studio_responsable_retirar", args=[self.proceso.pk, self.inicial.pk])
        )
        self.client.post(
            reverse("catalogo:studio_responsable_retirar", args=[self.proceso.pk, self.inicial.pk]), {"tab": "general"}
        )
        self.inicial.refresh_from_db()
        self.assertTrue(self.inicial.activo)
