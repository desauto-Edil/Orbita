/* Órbita — buscador "¿Qué necesitas?" de Inicio (4.D).
   Mejora progresiva: sin JS, el formulario envía a `core:necesidad` (página
   completa). Con JS pide el mismo contenido como fragmento y lo muestra bajo el
   buscador, sin salir de Inicio. El servidor valida, puntúa y decide qué se
   ofrece; aquí solo se pide y se pinta. No guarda el texto escrito en ningún
   lado. Sin dependencias. */

(function () {
  "use strict";

  var formulario = document.querySelector("[data-necesidad-search]");
  var panel = document.getElementById("necesidad-resultados");
  if (!formulario || !panel || typeof fetch !== "function") return;

  var campo = formulario.querySelector("input[name=q]");
  var peticion = 0;

  function cerrar() {
    peticion += 1;
    panel.hidden = true;
    panel.innerHTML = "";
    panel.removeAttribute("aria-busy");
  }

  formulario.addEventListener("submit", function (evento) {
    evento.preventDefault();
    var numero = ++peticion;
    panel.setAttribute("aria-busy", "true");
    fetch(formulario.getAttribute("action") + "?q=" + encodeURIComponent(campo.value), {
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" },
    })
      .then(function (respuesta) {
        if (!respuesta.ok) throw new Error("http " + respuesta.status);
        return respuesta.text();
      })
      .then(function (html) {
        // Una respuesta vieja no pisa a una más reciente.
        if (numero !== peticion) return;
        panel.innerHTML = html;
        panel.hidden = false;
      })
      .catch(function () {
        if (numero !== peticion) return;
        panel.textContent = "No pudimos buscar ahora. Intenta de nuevo.";
        panel.hidden = false;
      })
      .then(function () {
        if (numero === peticion) panel.removeAttribute("aria-busy");
      });
  });

  panel.addEventListener("click", function (evento) {
    if (!evento.target.closest("[data-necesidad-cerrar]")) return;
    evento.preventDefault();
    cerrar();
    campo.focus();
  });
})();
