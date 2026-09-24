/* Órbita — comportamiento del Application Shell (Incremento 0.5).
   Sin dependencias externas. Dos responsabilidades: drawer del sidebar en
   móvil, y el dropdown de perfil del topbar. Ninguna operación
   indispensable depende de hover — todo se activa por click/tap y por
   teclado (Escape cierra ambos). */

(function () {
  "use strict";

  function abrirDrawer() {
    document.body.classList.add("drawer-open");
    var boton = document.querySelector('[data-action="toggle-drawer"]');
    if (boton) boton.setAttribute("aria-expanded", "true");
  }

  function cerrarDrawer() {
    document.body.classList.remove("drawer-open");
    var boton = document.querySelector('[data-action="toggle-drawer"]');
    if (boton) boton.setAttribute("aria-expanded", "false");
  }

  function cerrarDropdowns() {
    document.querySelectorAll("[data-dropdown]").forEach(function (dropdown) {
      var menu = dropdown.querySelector(".dropdown__menu");
      var boton = dropdown.querySelector('[data-action="toggle-dropdown"]');
      if (menu) menu.hidden = true;
      if (boton) boton.setAttribute("aria-expanded", "false");
    });
  }

  document.addEventListener("click", function (event) {
    var drawerToggle = event.target.closest('[data-action="toggle-drawer"]');
    if (drawerToggle) {
      if (document.body.classList.contains("drawer-open")) {
        cerrarDrawer();
      } else {
        abrirDrawer();
      }
      return;
    }

    var dropdownToggle = event.target.closest('[data-action="toggle-dropdown"]');
    if (dropdownToggle) {
      var dropdown = dropdownToggle.closest("[data-dropdown]");
      var menu = dropdown.querySelector(".dropdown__menu");
      var estabaAbierto = !menu.hidden;
      cerrarDropdowns();
      menu.hidden = estabaAbierto;
      dropdownToggle.setAttribute("aria-expanded", String(!estabaAbierto));
      return;
    }

    if (!event.target.closest("[data-dropdown]")) {
      cerrarDropdowns();
    }

    if (
      document.body.classList.contains("drawer-open") &&
      !event.target.closest(".sidebar") &&
      !event.target.closest('[data-action="toggle-drawer"]')
    ) {
      cerrarDrawer();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      cerrarDropdowns();
      cerrarDrawer();
    }
  });
})();
