/* Órbita — comportamiento del Application Shell (2.UI.1 + 2.C).
   Sin dependencias externas. Tres responsabilidades: el popover "Más" del
   dock flotante, cerrar los mensajes globales (Django messages, 2.C), y
   (3.UI.4) mostrar/ocultar paneles inline de edición mediante
   data-action="toggle-panel" + data-target="<id>" — sin lógica de dominio,
   autorización ni validación: el servidor decide qué panel existe
   (renderiza o no el formulario), este script solo alterna `hidden`. */

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

    var togglePanel = event.target.closest('[data-action="toggle-panel"]');
    if (togglePanel) {
      var panel = document.getElementById(togglePanel.getAttribute("data-target"));
      if (panel) panel.hidden = !panel.hidden;
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
