from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.core.correo import ErrorCorreo
from apps.core.gmail_auth import callback, conectar, guardar_token


@override_settings(GMAIL_SENDER_EMAIL="remitente@edilandina.com")
class GmailConexionTests(SimpleTestCase):
    def test_guardado_conserva_variables_existentes(self):
        with TemporaryDirectory() as directory, override_settings(
            BASE_DIR=Path(directory), GMAIL_AUTH_MODE="service_account", GMAIL_REFRESH_TOKEN=""
        ):
            path = Path(directory) / ".env"
            path.write_text("# conservar\nOTRA_VARIABLE=valor\nGMAIL_AUTH_MODE=service_account\n")
            guardar_token("token-prueba_123")
            result = path.read_text()
            self.assertIn("# conservar\nOTRA_VARIABLE=valor\n", result)
            self.assertIn("GMAIL_AUTH_MODE=oauth\n", result)
            self.assertIn("GMAIL_REFRESH_TOKEN=token-prueba_123\n", result)
            self.assertEqual(result.count("GMAIL_AUTH_MODE="), 1)

    def test_rechaza_token_ausente_o_inyeccion_en_env(self):
        for token in (None, "", "valor\nOTRA=valor", "valor$EXPANSION"):
            with self.subTest(token=token), self.assertRaises(ErrorCorreo):
                guardar_token(token)

    def test_rechaza_duplicados_sin_modificar_archivo(self):
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)):
            path = Path(directory) / ".env"
            original = "GMAIL_AUTH_MODE=oauth\nGMAIL_AUTH_MODE=service_account\n"
            path.write_text(original)
            with self.assertRaises(ErrorCorreo):
                guardar_token("token-prueba")
            self.assertEqual(path.read_text(), original)

    def request(self):
        request = RequestFactory().get("/auth/gmail/callback/")
        request.user = Mock(is_authenticated=True, is_active=True, is_superuser=True)
        return request

    def test_usuario_sin_privilegio_no_puede_conectar(self):
        request = self.request()
        request.user.is_superuser = False
        with self.assertRaises(PermissionDenied):
            conectar(request)

    @patch("apps.core.gmail_auth.redirect")
    @patch("apps.core.gmail_auth.messages")
    @patch("apps.core.gmail_auth.guardar_token")
    @patch("apps.core.gmail_auth.cliente")
    def test_no_guarda_si_se_autoriza_otro_buzon(self, client, save, messages, redirect):
        client.return_value.authorize_access_token.return_value = {
            "userinfo": {"email": "otra@edilandina.com", "email_verified": True},
            "refresh_token": "no-guardar", "scope": "https://www.googleapis.com/auth/gmail.send",
        }
        redirect.return_value = HttpResponseRedirect("/auth/gmail/")
        callback(self.request())
        save.assert_not_called()
        messages.error.assert_called_once()

    @patch("apps.core.gmail_auth.redirect")
    @patch("apps.core.gmail_auth.messages")
    @patch("apps.core.gmail_auth.guardar_token")
    @patch("apps.core.gmail_auth.cliente")
    def test_no_guarda_si_no_se_acepta_envio(self, client, save, messages, redirect):
        client.return_value.authorize_access_token.return_value = {
            "userinfo": {"email": "remitente@edilandina.com", "email_verified": True},
            "refresh_token": "no-guardar", "scope": "openid email",
        }
        redirect.return_value = HttpResponseRedirect("/auth/gmail/")
        callback(self.request())
        save.assert_not_called()
        messages.error.assert_called_once()

    @patch("apps.core.gmail_auth.redirect")
    @patch("apps.core.gmail_auth.messages")
    @patch("apps.core.gmail_auth.guardar_token")
    @patch("apps.core.gmail_auth.cliente")
    def test_guarda_solo_despues_de_validar_buzon_y_permiso(self, client, save, messages, redirect):
        client.return_value.authorize_access_token.return_value = {
            "userinfo": {"email": "remitente@edilandina.com", "email_verified": True},
            "refresh_token": "token-prueba", "scope": "openid email https://www.googleapis.com/auth/gmail.send",
        }
        redirect.return_value = HttpResponseRedirect("/auth/gmail/")
        callback(self.request())
        save.assert_called_once_with("token-prueba")
        messages.success.assert_called_once()
