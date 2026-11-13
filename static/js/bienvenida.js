/* Bienvenida, paso «Color» (rediseño 2026-11-13). Solo presentación: al elegir un color, la «Vista previa» adopta
   el `data-tema` de esa opción (tokens.css §4). Guardar sigue siendo el envío normal del formulario; sin JS, la
   vista previa muestra el color guardado. Archivo aparte por la CSP (nada de JS en línea). */
(function () {
  var vista = document.querySelector("[data-bv-vista]");
  if (!vista) return;
  document.querySelectorAll("[data-bv-color]").forEach(function (opcion) {
    opcion.addEventListener("change", function () {
      var contenedor = opcion.closest("[data-tema]");
      if (opcion.checked && contenedor) vista.setAttribute("data-tema", contenedor.getAttribute("data-tema"));
    });
  });
})();
