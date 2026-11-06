/* Tablero del ticket (UX-4b) — mejora progresiva, sin reglas de negocio.
   - «Agregar otra»: clona la fila nueva de una tabla editable con el siguiente índice (`n1__…`, `n2__…`); sin JS
     hay una fila nueva por envío y basta con guardar para tener otra.
   - Botones con `data-confirmar`: piden confirmación antes de enviar (p. ej. enviar la lluvia de ideas completa).
   - `data-galeria`: filtros locales de las piezas de una gaceta por situación.
   - `data-ampliar`: la miniatura de una imagen se abre en un visor (`<dialog>`) sin salir de la página.
   El servidor valida y autoriza todo. */
(function () {
  "use strict";

  document.querySelectorAll("[data-tabla-editable]").forEach(function (formulario) {
    var boton = formulario.querySelector("[data-agregar-fila]");
    var plantilla = formulario.querySelector("[data-plantilla]");
    if (!boton || !plantilla) return;
    var siguiente = 1;
    boton.addEventListener("click", function () {
      var fila = plantilla.cloneNode(true);
      fila.removeAttribute("data-plantilla");
      fila.querySelectorAll("[name]").forEach(function (control) {
        control.name = control.name.replace(/^n\d+__/, "n" + siguiente + "__");
        if (control.type === "checkbox") control.checked = false;
        else if (control.tagName === "SELECT") control.selectedIndex = 0;
        else control.value = "";
      });
      siguiente += 1;
      plantilla.parentNode.appendChild(fila);
      var primero = fila.querySelector("input, textarea, select");
      if (primero) primero.focus();
    });
  });

  // Vista previa: una imagen se amplía en un visor sobre la página (sin JS, el enlace la abre en otra pestaña).
  var visor = null;
  function abrirVisor(url, nombre) {
    if (!window.HTMLDialogElement) return false;
    if (!visor) {
      visor = document.createElement("dialog");
      visor.className = "tb-visor";
      visor.innerHTML = '<img alt=""><div class="tb-visor__pie"><span></span><span><a target="_blank" rel="noopener">Abrir en otra pestaña</a> · <button type="button" class="btn btn--ghost btn--sm">Cerrar</button></span></div>';
      visor.querySelector("button").addEventListener("click", function () { visor.close(); });
      visor.addEventListener("click", function (e) { if (e.target === visor) visor.close(); });
      document.body.appendChild(visor);
    }
    visor.querySelector("img").src = url;
    visor.querySelector("img").alt = nombre;
    visor.querySelector(".tb-visor__pie > span").textContent = nombre;
    visor.querySelector("a").href = url;
    visor.showModal();
    return true;
  }

  // Galería de una gaceta: filtros locales por situación (sin JS se ven todas las piezas).
  document.querySelectorAll("[data-galeria]").forEach(function (galeria) {
    galeria.addEventListener("click", function (evento) {
      var filtro = evento.target.closest("[data-filtro]");
      if (!filtro) return;
      var clave = filtro.getAttribute("data-filtro");
      galeria.querySelectorAll("[data-filtro]").forEach(function (b) {
        var activo = b === filtro;
        b.classList.toggle("is-activo", activo);
        b.setAttribute("aria-pressed", activo ? "true" : "false");
      });
      galeria.querySelectorAll("[data-situacion]").forEach(function (tarjeta) {
        tarjeta.hidden = Boolean(clave) && tarjeta.getAttribute("data-situacion") !== clave;
      });
    });
  });

  document.addEventListener("click", function (evento) {
    var ampliar = evento.target.closest("[data-ampliar]");
    if (ampliar && !evento.metaKey && !evento.ctrlKey) {
      var imagen = ampliar.querySelector("img");
      if (abrirVisor(ampliar.getAttribute("data-ampliar"), imagen ? imagen.alt : "")) evento.preventDefault();
      return;
    }
    var boton = evento.target.closest("[data-confirmar]");
    if (boton && !window.confirm(boton.getAttribute("data-confirmar"))) evento.preventDefault();
  });
})();
