from django.core.management.base import BaseCommand, CommandError

from apps.core.correo import ErrorCorreo, autorizar, enviar_prueba


class Command(BaseCommand):
    help = "Valida la autorización Gmail sin enviar, o envía un único correo si se indica --destinatario."

    def add_arguments(self, parser):
        parser.add_argument("--destinatario", help="Correo autorizado para recibir una única prueba.")

    def handle(self, *args, **options):
        try:
            if options["destinatario"]:
                identificador = enviar_prueba(options["destinatario"])
                self.stdout.write(self.style.SUCCESS(
                    f"Gmail aceptó el correo de prueba. ID: {identificador}. Verifica su recepción."
                ))
            else:
                autorizar()
                self.stdout.write(self.style.SUCCESS("Autorización Gmail válida. No se envió ningún correo."))
        except ErrorCorreo as exc:
            raise CommandError(str(exc)) from None
