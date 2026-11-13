/* Apariencia (Fase visual): resuelve AUTOMÁTICO con la preferencia del dispositivo ANTES de pintar y la sigue
   si cambia. Se carga en <head> sin `defer` a propósito (sacado de base.html para la CSP, Versión 1). */
(function (d) {
    var preferencia = d.getAttribute("data-apariencia");
    if (preferencia !== "SISTEMA" || !window.matchMedia) return;
    var oscuro = window.matchMedia("(prefers-color-scheme: dark)");
    function aplicar() { d.setAttribute("data-modo", oscuro.matches ? "oscuro" : "claro"); }
    aplicar();
    if (oscuro.addEventListener) oscuro.addEventListener("change", aplicar);
  })(document.documentElement);
