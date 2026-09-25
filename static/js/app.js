/* Órbita — comportamiento del Application Shell (2.UI.1 + 2.C).
   Sin dependencias externas. Dos responsabilidades: el popover "Más" del
   dock flotante, y cerrar los mensajes globales (Django messages, 2.C).
   Ambas se activan por click/tap; el popover también por teclado (Escape
   cierra) y clic fuera. La autorización de qué aparece en el dock sigue
   resolviéndose en el servidor (apps/core/navegacion.py), este script solo
   abre/cierra lo que el servidor ya decidió renderizar — igual que un
   mensaje: el servidor decide si existe, este script solo lo oculta. */

(function () {
  "use strict";

  function boton() {
    return document.querySelector('[data-action="toggle-more"]');
  }

  function popover() {
    return document.getElementById("dock-more");
  }

  function abrirMas() {
    var menu = popover();
    var trigger = boton();
    if (menu) menu.hidden = false;
    if (trigger) trigger.setAttribute("aria-expanded", "true");
  }

  function cerrarMas() {
    var menu = popover();
    var trigger = boton();
    if (menu) menu.hidden = true;
    if (trigger) trigger.setAttribute("aria-expanded", "false");
  }

  document.addEventListener("click", function (event) {
    var cerrarMensaje = event.target.closest('[data-action="cerrar-mensaje"]');
    if (cerrarMensaje) {
      var alerta = cerrarMensaje.closest(".alert");
      if (alerta) alerta.remove();
      return;
    }

    var toggle = event.target.closest('[data-action="toggle-more"]');
    if (toggle) {
      var menu = popover();
      var estabaAbierto = menu && !menu.hidden;
      if (estabaAbierto) {
        cerrarMas();
      } else {
        abrirMas();
      }
      return;
    }

    if (!event.target.closest(".dock__item-wrap")) {
      cerrarMas();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      cerrarMas();
    }
  });
})();
