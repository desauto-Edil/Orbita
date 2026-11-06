/* Configuración › General — vista previa del estilo de color (Fase visual).
   Solo presentación: al elegir un estilo, la tarjeta «Así se verá» adopta su `data-tema` (tokens.css §4).
   Nada se aplica a la plataforma hasta guardar; el servidor valida el estilo elegido. */
(function () {
  "use strict";

  var vista = document.querySelector("[data-tema-vista]");
  if (!vista) return;
  var aviso = document.querySelector("[data-tema-aviso]");
  var vigente = document.documentElement.getAttribute("data-tema");

  document.querySelectorAll("[data-tema-opcion]").forEach(function (opcion) {
    opcion.addEventListener("change", function () {
      if (!opcion.checked) return;
      vista.setAttribute("data-tema", opcion.value);
      if (aviso) aviso.hidden = opcion.value === vigente;
    });
  });
})();
