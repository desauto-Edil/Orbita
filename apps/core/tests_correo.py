import base64
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from google.auth.exceptions import RefreshError
import requests

from apps.core.correo import ErrorCorreo, autorizar, credenciales, enviar_prueba


@override_settings(
    GMAIL_AUTH_MODE="service_account",
    GMAIL_SERVICE_ACCOUNT_EMAIL="cuenta@proyecto.iam.gserviceaccount.com",
    GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY="clave-de-prueba",
    GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY_ID="",
    GMAIL_SENDER_EMAIL="remitente@edilandina.com",
    GMAIL_SCOPES=("https://www.googleapis.com/auth/gmail.send",),
)
class CorreoTests(SimpleTestCase):
    @override_settings(
        GMAIL_AUTH_MODE="oauth", GOOGLE_OAUTH_CLIENT_ID="id-prueba",
        GOOGLE_OAUTH_CLIENT_SECRET="secreto-prueba", GMAIL_REFRESH_TOKEN="renovacion-prueba",
    )
    def test_oauth_no_requiere_cuenta_de_servicio(self):
        with override_settings(GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY=""):
            credentials = credenciales()
        self.assertEqual(credentials.refresh_token, "renovacion-prueba")
        self.assertEqual(credentials.scopes, ("https://www.googleapis.com/auth/gmail.send",))

    @override_settings(GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY="")
    def test_faltantes_se_reportan_sin_valores(self):
        with self.assertRaisesMessage(ErrorCorreo, "GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY"):
            credenciales()

    @patch("apps.core.correo.service_account.Credentials.from_service_account_info")
    def test_delega_solo_el_permiso_de_envio_al_buzon(self, constructor):
        credenciales()
        args, kwargs = constructor.call_args
        self.assertEqual(args[0]["client_email"], "cuenta@proyecto.iam.gserviceaccount.com")
        self.assertEqual(kwargs["subject"], "remitente@edilandina.com")
        self.assertEqual(kwargs["scopes"], ("https://www.googleapis.com/auth/gmail.send",))

    @patch("apps.core.correo.credenciales")
    def test_autorizacion_no_envia_y_oculta_respuesta_sensible(self, cred):
        cred.return_value.refresh.side_effect = RefreshError("unauthorized_client contenido-sensible")
        with patch("apps.core.correo.requests.post") as post:
            with self.assertRaises(ErrorCorreo) as error:
                autorizar()
            self.assertIn("delegación", str(error.exception))
            self.assertNotIn("contenido-sensible", str(error.exception))
            post.assert_not_called()

    @patch("apps.core.correo.autorizar")
    @patch("apps.core.correo.requests.post")
    def test_mime_y_envio_unico(self, post, auth):
        auth.return_value.token = "token-prueba"
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"id": "mensaje-123"}
        self.assertEqual(enviar_prueba("destino@edilandina.com"), "mensaje-123")
        post.assert_called_once()
        kwargs = post.call_args.kwargs
        mensaje = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(kwargs["json"]["raw"])
        )
        self.assertEqual(mensaje["To"], "destino@edilandina.com")
        self.assertEqual(mensaje["From"], "remitente@edilandina.com")
        self.assertIn("prueba", mensaje.get_content())
        self.assertFalse(kwargs["allow_redirects"])

    @patch("apps.core.correo.autorizar")
    def test_rechaza_destinatario_invalido_antes_de_autorizar(self, auth):
        with self.assertRaises(ErrorCorreo):
            enviar_prueba("incorrecto\nBcc: tercero@dominio.com")
        auth.assert_not_called()

    @patch("apps.core.correo.autorizar")
    @patch("apps.core.correo.requests.post")
    def test_timeout_no_reintenta(self, post, auth):
        post.side_effect = requests.Timeout()
        with self.assertRaisesMessage(ErrorCorreo, "Revisa Enviados"):
            enviar_prueba("destino@edilandina.com")
        post.assert_called_once()

    @patch("apps.core.correo.autorizar")
    @patch("apps.core.correo.requests.post")
    def test_403_no_expone_respuesta(self, post, auth):
        post.return_value = Mock(status_code=403, text="respuesta privada")
        with self.assertRaises(ErrorCorreo) as error:
            enviar_prueba("destino@edilandina.com")
        self.assertIn("HTTP 403", str(error.exception))
        self.assertNotIn("respuesta privada", str(error.exception))
