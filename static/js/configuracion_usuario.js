/* Configuración › Usuario: muestra solo el selector del alcance elegido (sacado de la plantilla para la CSP, Versión 1). */
  /* Mejora progresiva: muestra solo el selector (Área o Unidad) que
     corresponde al alcance elegido. La validación real la hace el servidor. */
  document.querySelectorAll("[data-alcance]").forEach(function (formulario) {
    var alcance = formulario.querySelector('select[name$="tipo_alcance"]');
    if (!alcance) return;
    function aplicar() {
      formulario.querySelectorAll("[data-alcance-campo]").forEach(function (bloque) {
        bloque.hidden = bloque.getAttribute("data-alcance-campo") !== alcance.value;
      });
    }
    alcance.addEventListener("change", aplicar);
    aplicar();
  });
