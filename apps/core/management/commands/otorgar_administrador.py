"""Crea (o habilita) el PRIMER administrador de Órbita. Solo lo corre quien tiene acceso al servidor:

    docker compose exec web python manage.py otorgar_administrador --correo persona@edilandina.com --nombre "Nombre Apellido"

- El correo debe ser de un dominio permitido (`GOOGLE_OAUTH_ALLOWED_DOMAINS`): es con lo que la persona ingresará.
- Si la cuenta no existe la crea (sin contraseña: el único ingreso es Google); si existe, la reutiliza.
- Le asigna, en alcance GLOBAL, el rol «Administrador de Plataforma» con `plataforma.administracion_avanzada` y los
  permisos de Configuración (`apps.core.plataforma.PERMISOS_DEL_ADMINISTRADOR`). El nombre del rol solo sirve para
  encontrarlo o crearlo aquí; la autorización sigue siendo por permisos (RN-007).
- Si ya hay otro administrador vigente se niega, salvo `--forzar`: los siguientes se otorgan desde Configuración.
- Idempotente y auditado (origen Sistema). No hay ninguna ruta web que haga esto.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core import plataforma
from apps.core.auditoria import registrar_evento, serializar
from apps.core.autorizacion import existe_asignacion_vigente
from apps.core.models import AsignacionRol, Permiso, RegistroAuditoria, RolFuncional, RolPermiso, Usuario

SISTEMA = RegistroAuditoria.Origen.SISTEMA


def _auditar(instancia, anterior=None):
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR if anterior is None else RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=instancia, origen=SISTEMA, datos_anteriores=anterior, datos_nuevos=serializar(instancia),
    )


class Command(BaseCommand):
    help = "Crea o habilita el primer administrador de Órbita (solo desde el servidor)."

    def add_arguments(self, parser):
        parser.add_argument("--correo", required=True, help="Correo corporativo con el que ingresará con Google.")
        parser.add_argument("--nombre", default="", help="Nombre y apellido (solo si la cuenta no existe).")
        parser.add_argument(
            "--forzar", action="store_true", help="Otorgarlo aunque ya exista otro administrador vigente."
        )

    @transaction.atomic
    def handle(self, *args, correo, nombre, forzar, **options):
        correo = correo.strip().lower()
        permitidos = {d.lower() for d in settings.GOOGLE_OAUTH_ALLOWED_DOMAINS}
        if "@" not in correo or correo.rpartition("@")[2] not in permitidos:
            raise CommandError(
                f"«{correo}» no es de un dominio permitido ({', '.join(sorted(permitidos))}): no podría ingresar."
            )

        cuentas = list(Usuario.objects.select_for_update().filter(email__iexact=correo)[:2])
        if len(cuentas) > 1:
            raise CommandError(f"Hay más de una cuenta con «{correo}». Corrige los correos antes de continuar.")
        if cuentas and not cuentas[0].is_active:
            raise CommandError(f"La cuenta de «{correo}» está inactiva. Actívala antes de otorgarle el acceso.")
        usuario = cuentas[0] if cuentas else None

        otros = Usuario.objects.filter(is_active=True)
        if usuario is not None:
            otros = otros.exclude(pk=usuario.pk)
        hay_otro = existe_asignacion_vigente(
            plataforma.PERMISO_ADMINISTRACION_AVANZADA, usuarios_ids=otros.values_list("pk", flat=True),
            solo_global=True,
        )
        if hay_otro and not forzar:
            raise CommandError(
                "Ya hay un administrador vigente. Otorga los siguientes desde Configuración › Roles y permisos, "
                "o repite el comando con --forzar si de verdad necesitas recuperar el acceso."
            )

        faltan = set(plataforma.PERMISOS_DEL_ADMINISTRADOR) - set(
            Permiso.objects.filter(codigo__in=plataforma.PERMISOS_DEL_ADMINISTRADOR).values_list("codigo", flat=True)
        )
        if faltan:
            raise CommandError(f"Faltan permisos en la base de datos ({', '.join(sorted(faltan))}): corre migrate.")

        if usuario is None:
            nombres = nombre.strip().split(" ", 1)
            usuario = Usuario.objects.create_user(
                correo, first_name=nombres[0][:150], last_name=(nombres[1] if len(nombres) > 1 else "")[:150],
            )
            _auditar(usuario)
            self.stdout.write(f"Cuenta creada para {correo}.")

        rol = RolFuncional.objects.filter(nombre=plataforma.NOMBRE_ROL_ADMINISTRADOR).order_by("pk").first()
        if rol is None:
            rol = RolFuncional.objects.create(
                nombre=plataforma.NOMBRE_ROL_ADMINISTRADOR,
                descripcion="Administra la plataforma: Configuración y la administración avanzada.",
            )
            _auditar(rol)
        elif not rol.activo:
            anterior = serializar(rol)
            rol.activo = True
            rol.save(update_fields=["activo", "actualizado_en"])
            _auditar(rol, anterior)

        for permiso in Permiso.objects.filter(codigo__in=plataforma.PERMISOS_DEL_ADMINISTRADOR):
            if not permiso.activo:
                anterior = serializar(permiso)
                permiso.activo = True
                permiso.save(update_fields=["activo", "actualizado_en"])
                _auditar(permiso, anterior)
            vinculo, creado = RolPermiso.objects.get_or_create(rol=rol, permiso=permiso)
            if creado:
                _auditar(vinculo)
            elif not vinculo.activo:
                anterior = serializar(vinculo)
                vinculo.activo = True
                vinculo.save(update_fields=["activo", "actualizado_en"])
                _auditar(vinculo, anterior)

        asignacion = AsignacionRol.objects.filter(
            usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL, activo=True
        ).first()
        if asignacion is None:
            asignacion = AsignacionRol.objects.create(
                usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
            )
            _auditar(asignacion)
        elif asignacion.fecha_fin is not None:
            anterior = serializar(asignacion)
            asignacion.fecha_fin = None
            asignacion.save(update_fields=["fecha_fin", "actualizado_en"])
            _auditar(asignacion, anterior)

        self.stdout.write(self.style.SUCCESS(
            f"{correo} ya es administrador de Órbita. Que ingrese con Google y continúe en Configuración."
        ))
