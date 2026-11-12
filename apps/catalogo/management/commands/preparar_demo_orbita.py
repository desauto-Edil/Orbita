"""Bloque 5.6 — prepara, verifica, publica, genera ejemplos y retira los casos de la demostración de Órbita.

Cada fase se ejecuta por separado y de forma explícita (`--fase`). Todo pasa por las operaciones de dominio del
Studio con los permisos del `--actor`; los recursos de la demo se reconocen solo por `catalogo.RecursoDemo`.
`--simular` ejecuta la fase y deshace todo al final. Ver `apps.catalogo.demo` y `docs/demo/ORBITA_DEMO.md`.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalogo.demo import contrato, fases, preparacion
from apps.catalogo.demo.participantes import PERIODOS, Participantes, resolver_area, resolver_usuario
from apps.catalogo.models import RecursoDemo
from apps.core.autorizacion import usuario_tiene_permiso
from apps.workflows.autorizacion import puede_administrar_workflows

FASES = ("preparar", "verificar", "publicar", "ejemplos", "avanzar", "cierre", "retirar", "eliminar")


class _Deshacer(Exception):
    pass


def _mensaje(exc):
    return "; ".join(getattr(exc, "messages", None) or [str(exc) or exc.__class__.__name__])


class Command(BaseCommand):
    help = (
        "Prepara la demostración de Órbita (parrilla, webinar, pieza gráfica, presentaciones) con las operaciones "
        "del dominio. Fases: preparar, verificar, publicar, ejemplos, avanzar (imperandina: lleva su ticket de "
        "prueba a después de la gaceta; desarrollo: el aprobador decide el plan y entra a la gaceta), retirar, eliminar (borrado físico de lo que creó la demo; exige "
        "--confirmar-eliminar)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--fase", required=True, choices=FASES)
        parser.add_argument("--actor", required=True, help="Correo (o su parte local) de la persona que ejecuta y queda en la auditoría.")
        parser.add_argument(
            "--casos", default=",".join(contrato.ALIAS_POR_DEFECTO),
            help=(
                "Casos separados por coma: parrilla, webinar, pieza, presentaciones (por defecto, esos cuatro), "
                "imperandina (personas, equipos y calendario fijos en su contrato) y desarrollo (plan de trabajo de "
                "un desarrollo de software); estos dos solo si se piden."
            ),
        )
        parser.add_argument("--simular", action="store_true", help="Ejecuta la fase y deshace todo al final.")
        parser.add_argument(
            "--confirmar-eliminar", dest="confirmar_eliminar", action="store_true",
            help="Obligatorio con --fase eliminar: borra FÍSICAMENTE lo que creó la demo (no personas ni permisos).",
        )
        parser.add_argument("--categoria", default="Demostración Órbita")
        parser.add_argument("--sufijo", default=" · Demo", help="Se agrega al nombre de cada servicio y plantilla.")
        # Parrilla
        parser.add_argument("--planificador")
        parser.add_argument("--productor")
        parser.add_argument("--aprobador", help="Aprobador interno (idea y pieza); por defecto también del webinar.")
        parser.add_argument("--responsable-publicacion", dest="responsable_publicacion")
        parser.add_argument("--revisores", help="Revisores de la gaceta: usuarios o equipo:<nombre>, separados por coma.")
        parser.add_argument(
            "--dia-creacion", dest="dia_creacion", type=int,
            help="Día (1-28) en que se crea cada ejecución mensual; 20 por defecto. También vale al publicar.",
        )
        parser.add_argument(
            "--periodo", choices=PERIODOS, help="Periodo de cada ejecución; MES_SIGUIENTE por defecto. También al publicar.",
        )
        # Webinar
        parser.add_argument("--coordinador")
        parser.add_argument("--aprobador-webinar", dest="aprobador_webinar")
        parser.add_argument("--equipo-audiovisual", dest="equipo_audiovisual")
        # Servicios
        parser.add_argument("--equipo-diseno", dest="equipo_diseno")
        parser.add_argument("--responsable-presentaciones", dest="responsable_presentaciones")
        parser.add_argument("--area-visibilidad", dest="area_visibilidad")
        # Ejemplos
        parser.add_argument("--solicitante", help="Quien radica los tickets de ejemplo de webinar y servicios.")
        parser.add_argument("--area-ejemplo", dest="area_ejemplo", help="Área para el campo «Área» del webinar.")

    def handle(self, *args, **opciones):
        try:
            actor = resolver_usuario(opciones["actor"], "--actor")
            casos = self._casos(opciones["casos"])
        except ValidationError as exc:
            raise CommandError(_mensaje(exc))
        if opciones["fase"] == "eliminar" and not opciones["confirmar_eliminar"]:
            raise CommandError("La fase eliminar borra físicamente la demo: agrega --confirmar-eliminar.")
        self._exigir_permisos(actor, opciones["fase"])
        fallos = 0
        try:
            with transaction.atomic():
                fallos = self._ejecutar(opciones, actor, casos)
                if opciones["simular"] or opciones["fase"] == "verificar":
                    raise _Deshacer
        except _Deshacer:
            if opciones["simular"]:
                self.stdout.write(self.style.WARNING("Simulación: no se guardó ningún cambio."))
        if fallos:
            raise CommandError(f"{fallos} caso(s) no se completaron. Revisa los mensajes anteriores.")

    def _casos(self, texto):
        casos = []
        for nombre in (parte.strip().lower() for parte in texto.split(",") if parte.strip()):
            if nombre not in contrato.ALIAS:
                raise ValidationError(f"Caso desconocido «{nombre}». Usa: {', '.join(contrato.ALIAS)}.")
            casos.append(contrato.ALIAS[nombre])
        return casos

    def _exigir_permisos(self, actor, fase):
        faltan = []
        if not usuario_tiene_permiso(actor, "catalogo.administrar"):
            faltan.append("catalogo.administrar")
        if fase in ("preparar", "publicar"):
            if not usuario_tiene_permiso(actor, "formulario.administrar"):
                faltan.append("formulario.administrar")
            if not puede_administrar_workflows(actor):
                faltan.append("workflows.administrar")
        if faltan:
            raise CommandError(f"El actor {actor.email} no tiene: {', '.join(faltan)}.")

    def _ejecutar(self, opciones, actor, casos):
        fase = opciones["fase"]
        participantes = solicitante = area = None
        try:
            if fase == "preparar":
                participantes = Participantes.desde_opciones(opciones)
            if fase == "ejemplos":
                solicitante = resolver_usuario(opciones["solicitante"], "--solicitante") if opciones.get("solicitante") else None
                area = resolver_area(opciones["area_ejemplo"], "--area-ejemplo") if opciones.get("area_ejemplo") else None
        except ValidationError as exc:
            raise CommandError(_mensaje(exc))
        fallos = 0
        for codigo in casos:
            caso = contrato.CONTRATOS[codigo]
            self.stdout.write(self.style.MIGRATE_HEADING(f"{caso.nombre} ({fase})"))
            try:
                with transaction.atomic():
                    lineas = self._fase(fase, codigo, actor, opciones, participantes, solicitante, area)
            except (ValidationError, PermissionDenied) as exc:
                fallos += 1
                self.stdout.write(self.style.ERROR(f"  ✗ {_mensaje(exc)}"))
                continue
            for linea in lineas:
                self.stdout.write(f"  {linea}" if not linea.startswith("  ") else linea)
        return fallos

    def _fase(self, fase, codigo, actor, opciones, participantes, solicitante, area):
        if fase == "preparar":
            recurso, creado = preparacion.preparar_caso(
                codigo, actor, participantes, categoria_nombre=opciones["categoria"], sufijo=opciones["sufijo"],
            )
            if not creado:
                return [f"Ya estaba preparado (servicio #{recurso.servicio_id}); no se modificó nada."]
            return [
                f"Preparado: servicio #{recurso.servicio_id} «{recurso.servicio.nombre}», sin publicar.",
                "Siguiente paso: --fase verificar y después --fase publicar.",
            ]
        recurso = RecursoDemo.objects.filter(caso=codigo).select_related("servicio").first()
        if recurso is None:
            return ["No está preparado (usa --fase preparar)."]
        if fase == "verificar":
            return fases.verificar(recurso, actor)
        if fase == "publicar":
            return fases.publicar(recurso, actor, dia_creacion=opciones.get("dia_creacion"), periodo=opciones.get("periodo"))
        if fase == "ejemplos":
            return fases.ejemplos(recurso, actor, solicitante=solicitante, area=area)
        if fase == "avanzar":
            return fases.avanzar(recurso, actor)
        if fase == "cierre":
            return fases.cierre(recurso, actor)
        if fase == "eliminar":
            from apps.catalogo.demo.eliminacion import eliminar

            return eliminar(recurso, actor)
        return fases.retirar(recurso, actor)
