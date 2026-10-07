/* Studio — comportamientos locales de los formularios de configuración (4.F1/4.F2).

   Mejora progresiva: sin JavaScript todo sigue siendo un formulario válido y el servidor es
   siempre quien valida. Aquí solo se muestra u oculta lo que no aplica.

   1. Opciones que dependen del tipo de un campo: `data-solo-tipos="FECHA,FECHA_HORA"` en un
      contenedor se muestra solo cuando el selector `…-tipo` de su formulario tiene uno de esos
      valores (p. ej. «Usar como fecha requerida»).
   2. Aprobación: `data-aprobacion` agrupa «¿Cuántos aprobadores participan?»
      (`data-aprobacion-cantidad`), una fila por aprobador (`data-aprobacion-fila`) y el
      «Modo de aprobación» (`data-aprobacion-modo`), que solo se pide con más de uno.
   3. Tipo e inicio (4.G1): en el formulario `data-config-inicio` (crear y Básico), el selector
      `tipo` decide qué se ve. `[data-solo-proceso]` (pregunta «¿Cómo inicia?») solo con Proceso;
      `[data-inicio-programado]` (calendario y responsable) solo con Proceso + «Programado»; y
      `[data-oculto-si-programado]` (lo orientado a quien solicita: visibilidad, instrucciones,
      búsqueda) se oculta con Proceso + «Programado». Lo oculto de Proceso se deshabilita para que
      no se envíe; lo demás solo se oculta, así conserva su valor. */

(function () {
  "use strict";

  /* ----------------------- Opciones que dependen del tipo ----------------------- */

  function aplicarTipo(contenedor, selector) {
    var tipos = contenedor.getAttribute("data-solo-tipos").split(",");
    contenedor.hidden = tipos.indexOf(selector.value) === -1;
  }

  function iniciarSoloTipos() {
    document.querySelectorAll("[data-solo-tipos]").forEach(function (contenedor) {
      var formulario = contenedor.closest("form");
      var selector = formulario && formulario.querySelector('select[name$="-tipo"]');
      if (!selector) return;
      aplicarTipo(contenedor, selector);
      selector.addEventListener("change", function () {
        aplicarTipo(contenedor, selector);
      });
    });
  }

  /* ------------------------------- Aprobación ------------------------------- */

  function aplicarCantidad(grupo, campo) {
    var cantidad = parseInt(campo.value, 10);
    if (isNaN(cantidad) || cantidad < 1) cantidad = 1;
    grupo.querySelectorAll("[data-aprobacion-fila]").forEach(function (fila, indice) {
      var visible = indice < cantidad;
      fila.hidden = !visible;
      fila.querySelectorAll("input, select").forEach(function (control) {
        control.disabled = !visible;
      });
    });
    grupo.querySelectorAll("[data-aprobacion-modo]").forEach(function (modo) {
      var visible = cantidad > 1;
      modo.hidden = !visible;
      modo.querySelectorAll("input, select").forEach(function (control) {
        control.disabled = !visible;
      });
    });
  }

  function iniciarAprobacion() {
    document.querySelectorAll("[data-aprobacion]").forEach(function (grupo) {
      var campo = grupo.querySelector("[data-aprobacion-cantidad]");
      if (!campo) return;
      aplicarCantidad(grupo, campo);
      campo.addEventListener("input", function () {
        aplicarCantidad(grupo, campo);
      });
      campo.addEventListener("change", function () {
        aplicarCantidad(grupo, campo);
      });
    });
  }

  /* ----------------------------- Tipo e inicio del proceso ----------------------------- */

  function mostrar(elemento, visible) {
    elemento.hidden = !visible;
    elemento.querySelectorAll("input, select, textarea").forEach(function (control) {
      control.disabled = !visible;
    });
  }

  function aplicarInicio(formulario) {
    var tipo = formulario.querySelector('select[name="tipo"]');
    var esProceso = tipo ? tipo.value === "PROCESO" : formulario.getAttribute("data-tipo") === "PROCESO";
    var modo = formulario.querySelector('input[name="modo"]:checked');
    var programado = esProceso && !!modo && modo.value === "PROGRAMADO";
    formulario.querySelectorAll("[data-solo-proceso]").forEach(function (el) {
      mostrar(el, esProceso);
    });
    formulario.querySelectorAll("[data-inicio-programado]").forEach(function (el) {
      mostrar(el, programado);
    });
    document.querySelectorAll("[data-oculto-si-programado]").forEach(function (el) {
      el.hidden = programado;
    });
  }

  function iniciarInicio() {
    document.querySelectorAll("[data-config-inicio]").forEach(function (formulario) {
      aplicarInicio(formulario);
      formulario.addEventListener("change", function (evento) {
        var nombre = evento.target && evento.target.name;
        if (nombre === "tipo" || nombre === "modo") aplicarInicio(formulario);
      });
    });
  }

  function iniciar() {
    iniciarSoloTipos();
    iniciarAprobacion();
    iniciarInicio();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", iniciar);
  } else {
    iniciar();
  }
})();
