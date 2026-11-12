"""Acceso Google: identidad validada, vinculación local y errores de proveedor."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.test import RequestFactory, TestCase, override_settings

from apps.core import google_auth


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="cliente-prueba",
    GOOGLE_OAUTH_CLIENT_SECRET="secreto-prueba",
    GOOGLE_OAUTH_REDIRECT_URI="http://127.0.0.1:8000/auth/google/callback/",
    GOOGLE_OAUTH_ALLOWED_DOMAINS=["edilandina.com", "gammaingenieros.com"],
    LOGIN_URL="/login/", LOGIN_REDIRECT_URL="/",
)
class GoogleTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(
            "persona@edilandina.com",
        )
        self.claims = {
            "sub": "google-123", "email": self.usuario.email,
            "email_verified": True, "hd": "edilandina.com",
            "picture": "https://lh3.googleusercontent.com/foto",
        }

    def request(self, method="get", data=None):
        from django.contrib.auth.models import AnonymousUser
        request = getattr(RequestFactory(), method)("/auth/google/", data or {})
        SessionMiddleware(lambda r: None).process_request(request)
        request._messages = FallbackStorage(request)
        request.user = AnonymousUser()
        return request

    def test_vincula_sin_crear_cuentas(self):
        clave_sesion = self.usuario.clave_sesion
        usuario = google_auth.resolver_usuario(self.claims)
        self.assertEqual(usuario.pk, self.usuario.pk)
        self.assertEqual(usuario.google_sub, "google-123")
        self.assertEqual(usuario.google_foto_url, self.claims["picture"])
        self.assertEqual(usuario.clave_sesion, clave_sesion)  # vincular no cierra las sesiones
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_admite_segundo_dominio_y_correo_sin_distinguir_mayusculas(self):
        self.usuario.email = "Persona@gammaingenieros.com"
        self.usuario.save()
        self.claims.update(email="persona@gammaingenieros.com", hd="gammaingenieros.com")
        self.assertEqual(google_auth.resolver_usuario(self.claims).pk, self.usuario.pk)

    def test_rechaza_identidad_no_verificada_o_dominio_ajeno(self):
        for cambios in ({"email_verified": False}, {"hd": "otro.com"},
                        {"email": "persona@otro.com"}, {"sub": ""}, {"hd": ""}):
            with self.subTest(cambios=cambios), self.assertRaises(PermissionDenied):
                google_auth.resolver_usuario({**self.claims, **cambios})

    def test_rechaza_no_registrado(self):
        self.claims["email"] = "ausente@edilandina.com"
        with self.assertRaises(PermissionDenied):
            google_auth.resolver_usuario(self.claims)

    def test_rechaza_inactivo(self):
        self.usuario.is_active = False
        self.usuario.save()
        with self.assertRaises(PermissionDenied):
            google_auth.resolver_usuario(self.claims)

    def test_un_correo_no_puede_repetirse_ni_con_otras_mayusculas(self):
        # Versión 1: el correo es la identidad; la base de datos impide una segunda cuenta con el mismo correo.
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            get_user_model().objects.create_user("PERSONA@edilandina.com", is_active=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            get_user_model().objects.filter(pk=self.usuario.pk).update(email="x@edilandina.com")
            get_user_model().objects.create(email="X@EDILANDINA.COM")  # se guarda en minúsculas: choca

    def test_no_reemplaza_identidad_vinculada(self):
        self.usuario.google_sub = "otra-identidad"
        self.usuario.save()
        with self.assertRaises(PermissionDenied):
            google_auth.resolver_usuario(self.claims)

    def test_no_reasigna_sub_a_otra_cuenta(self):
        get_user_model().objects.create_user("otra@pruebas.orbita", google_sub="google-123")
        with self.assertRaises(PermissionDenied):
            google_auth.resolver_usuario(self.claims)

    def test_foto_ausente_o_insegura_usa_iniciales(self):
        for foto in ("", "http://sitio/foto", "javascript:alert(1)", None):
            with self.subTest(foto=foto):
                usuario = google_auth.resolver_usuario({**self.claims, "picture": foto})
                self.assertEqual(usuario.google_foto_url, "")

    @patch("apps.core.google_auth.cliente")
    def test_inicio_solo_por_post_y_redirige_al_proveedor(self, cliente):
        self.assertEqual(google_auth.iniciar(self.request()).status_code, 405)
        cliente.return_value.authorize_redirect.return_value = HttpResponseRedirect("https://accounts.google.com/")
        request = self.request("post")
        self.assertEqual(google_auth.iniciar(request).status_code, 302)
        cliente.return_value.authorize_redirect.assert_called_once_with(
            request, "http://127.0.0.1:8000/auth/google/callback/", prompt="select_account"
        )

    @patch("apps.core.google_auth.cliente")
    def test_callback_inicia_sesion_sin_guardar_tokens(self, cliente):
        cliente.return_value.authorize_access_token.return_value = {
            "userinfo": self.claims, "access_token": "no-guardar"
        }
        request = self.request()
        self.assertEqual(google_auth.callback(request).url, "/")
        self.assertEqual(request.session["_auth_user_id"], str(self.usuario.pk))
        self.assertNotIn("no-guardar", str(dict(request.session)))

    @patch("apps.core.google_auth.cliente")
    def test_el_destino_interno_viaja_por_la_sesion_y_se_usa_al_volver(self, cliente):
        cliente.return_value.authorize_redirect.return_value = HttpResponseRedirect("https://accounts.google.com/")
        ida = self.request("post", {"next": "/tickets/7/"})
        google_auth.iniciar(ida)
        self.assertEqual(ida.session[google_auth.SESION_DESTINO], "/tickets/7/")
        cliente.return_value.authorize_access_token.return_value = {"userinfo": self.claims}
        vuelta = self.request()
        vuelta.session[google_auth.SESION_DESTINO] = "/tickets/7/"
        self.assertEqual(google_auth.callback(vuelta).url, "/tickets/7/")
        self.assertNotIn(google_auth.SESION_DESTINO, vuelta.session)

    @patch("apps.core.google_auth.cliente")
    def test_un_destino_externo_se_descarta(self, cliente):
        cliente.return_value.authorize_redirect.return_value = HttpResponseRedirect("https://accounts.google.com/")
        for externo in ("https://otro.com/", "//otro.com/x", "javascript:alert(1)"):
            with self.subTest(externo=externo):
                ida = self.request("post", {"next": externo})
                google_auth.iniciar(ida)
                self.assertNotIn(google_auth.SESION_DESTINO, ida.session)
        cliente.return_value.authorize_access_token.return_value = {"userinfo": self.claims}
        vuelta = self.request()
        vuelta.session[google_auth.SESION_DESTINO] = "https://otro.com/"
        self.assertEqual(google_auth.callback(vuelta).url, "/")

    @patch("apps.core.google_auth.cliente")
    def test_una_cuenta_no_registrada_vuelve_al_login_con_su_destino(self, cliente):
        cliente.return_value.authorize_access_token.return_value = {
            "userinfo": {**self.claims, "email": "ausente@edilandina.com"}
        }
        request = self.request()
        request.session[google_auth.SESION_DESTINO] = "/tickets/7/"
        self.assertEqual(google_auth.callback(request).url, "/login/?next=%2Ftickets%2F7%2F")
        self.assertNotIn("_auth_user_id", request.session)

    @patch("apps.core.google_auth.cliente")
    def test_confirmar_identidad_con_la_sesion_abierta_registra_un_acceso_con_google(self, cliente):
        # Etapa 4: la administración avanzada pide confirmar con Google sin cerrar la sesión.
        from apps.core.models import AccesoUsuario

        cliente.return_value.authorize_redirect.return_value = HttpResponseRedirect("https://accounts.google.com/")
        ida = self.request("post", {"next": "/admin/", "reautenticar": "1"})
        ida.user = self.usuario
        self.assertEqual(google_auth.iniciar(ida).status_code, 302)
        cliente.return_value.authorize_redirect.assert_called_once()
        cliente.return_value.authorize_access_token.return_value = {"userinfo": self.claims}
        vuelta = self.request()
        vuelta.user = self.usuario
        vuelta.session[google_auth.SESION_REAUTENTICAR] = self.usuario.pk
        vuelta.session[google_auth.SESION_DESTINO] = "/admin/"
        self.assertEqual(google_auth.callback(vuelta).url, "/admin/")
        self.assertTrue(AccesoUsuario.objects.filter(usuario=self.usuario, metodo=AccesoUsuario.Metodo.GOOGLE).exists())

    @patch("apps.core.google_auth.cliente")
    def test_confirmar_identidad_con_otra_cuenta_no_cambia_de_persona(self, cliente):
        from apps.core.models import AccesoUsuario

        otra = get_user_model().objects.create_user("otra@edilandina.com")
        cliente.return_value.authorize_access_token.return_value = {
            "userinfo": {**self.claims, "sub": "google-otra", "email": otra.email}
        }
        vuelta = self.request()
        vuelta.user = self.usuario
        vuelta.session[google_auth.SESION_REAUTENTICAR] = self.usuario.pk
        google_auth.callback(vuelta)
        self.assertFalse(AccesoUsuario.objects.filter(metodo=AccesoUsuario.Metodo.GOOGLE).exists())
        self.assertNotIn("_auth_user_id", vuelta.session)

    @patch("apps.core.google_auth.cliente")
    def test_sin_pedir_confirmacion_una_sesion_abierta_no_vuelve_a_google(self, cliente):
        ida = self.request("post", {"next": "/tickets/7/"})
        ida.user = self.usuario
        self.assertEqual(google_auth.iniciar(ida).url, "/tickets/7/")
        cliente.assert_not_called()

    @patch("apps.core.google_auth.cliente")
    def test_error_de_validacion_no_inicia_sesion(self, cliente):
        cliente.return_value.authorize_access_token.side_effect = ValueError("token inválido")
        request = self.request()
        self.assertEqual(google_auth.callback(request).url, "/login/")
        self.assertNotIn("_auth_user_id", request.session)

    @override_settings(GOOGLE_OAUTH_CLIENT_SECRET="")
    @patch("apps.core.google_auth.cliente")
    def test_sin_credenciales_no_contacta_google(self, cliente):
        self.assertEqual(google_auth.iniciar(self.request("post")).url, "/login/")
        cliente.assert_not_called()
