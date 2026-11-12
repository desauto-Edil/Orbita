"""G.0 — prueba REAL de la Conversación en Google Chat con delegación de dominio (plan
`.claude/plans/conversacion_google_chat.md`). No toca tickets: crea un espacio de prueba como la cuenta de Órbita,
agrega a una persona, publica como la cuenta y como esa persona, lee los mensajes y deja el espacio en solo lectura.
Si algo falla, dice en qué paso (delegación, permisos del espacio, miembros, mensajes).

El espacio queda en Chat (Órbita no borra espacios): bórralo a mano cuando termines."""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core import google_chat as gc
from apps.core.models import Usuario


class Command(BaseCommand):
    help = "Prueba la Conversación en Google Chat: crea un espacio de prueba con --con <usuario o correo>."

    def add_arguments(self, parser):
        parser.add_argument("--con", required=True, help="Correo de Workspace (o su parte local) de la persona de prueba.")

    def _paso(self, texto):
        self.stdout.write(f"· {texto}")

    def handle(self, *args, **options):
        referencia = options["con"].strip()
        usuario = Usuario.objects.por_referencia(referencia).first()
        correo = (usuario.email if usuario is not None else referencia).strip()
        if "@" not in correo:
            raise CommandError(f"«{referencia}» no tiene correo de Workspace.")
        if not gc.configurado():
            raise CommandError("Falta configurar en .env: " + "; ".join(gc.faltantes()) + ".")
        if correo.lower() == gc.cuenta():
            raise CommandError("La persona de prueba debe ser distinta de GOOGLE_CHAT_CUENTA.")
        marca = timezone.now().strftime("%Y%m%d%H%M%S")
        paso = "crear el espacio como la cuenta de Órbita"
        try:
            self._paso(f"Creando un espacio de prueba como {gc.cuenta()}…")
            espacio = gc.crear_espacio(f"Prueba de Órbita {marca}", f"orbita-prueba-{marca}")
            nombre = espacio["name"]
            paso = "restringir quién agrega miembros"
            gc.restringir_miembros(nombre)
            paso = f"agregar a {correo}"
            self._paso(f"Agregando a {correo}…")
            gc.agregar_miembro(nombre, correo)
            paso = "publicar como la cuenta de Órbita"
            gc.publicar(nombre, "Prueba de Órbita: mensaje de la cuenta de Órbita.", gc.id_cliente(f"orbita-prueba-{marca}-a"))
            paso = f"publicar como {correo} (delegación)"
            self._paso(f"Publicando como {correo}…")
            gc.publicar(nombre, "Prueba de Órbita: mensaje publicado en nombre de la persona.",
                        gc.id_cliente(f"orbita-prueba-{marca}-b"), como=correo)
            paso = "leer los mensajes del espacio"
            leidos = gc.listar_mensajes(nombre, timezone.now() - timedelta(hours=1))
            paso = "dejar el espacio en solo lectura"
            gc.solo_lectura(nombre, True)
        except gc.ErrorChat as error:
            raise CommandError(f"Falló al {paso}: {error}") from None
        self.stdout.write(self.style.SUCCESS(
            f"Google Chat funciona con delegación: espacio {nombre} con {len(leidos)} mensaje(s), ya en solo lectura.\n"
            f"Ábrelo en {espacio.get('spaceUri') or 'Google Chat'} y bórralo cuando termines."
        ))
