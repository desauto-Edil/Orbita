/* Trabajo — vista previa de Mi trabajo (bandejas técnicas) y resumen de la Cola (mejora progresiva).
   Sin este script cada fila es un enlace normal a su detalle. Con él, la fila abre un panel con un fragmento
   que renderiza el servidor (que también decide si el usuario puede verlo); "Abrir" lleva al detalle completo.

   UX-2: el panel NO reserva espacio mientras no hay nada seleccionado: aparece al elegir una fila y se cierra
   con su botón o con Escape. En pantallas amplias es una columna lateral; en las angostas solo se usa si la
   vista lo pide (`data-trabajo-movil`, la Cola) y entonces se muestra como hoja superpuesta. */
(function () {
  "use strict";

  var raiz = document.querySelector("[data-trabajo]");
  if (!raiz || !window.fetch || !window.matchMedia) return;

  var panel = raiz.querySelector("[data-trabajo-vista]");
  var contenido = raiz.querySelector("[data-trabajo-contenido]");
  var cerrarBoton = raiz.querySelector("[data-trabajo-cerrar]");
  var amplio = window.matchMedia("(min-width: 1024px)");
  var enMovil = raiz.hasAttribute("data-trabajo-movil");
  var turno = 0;

  function activo() {
    return amplio.matches || enMovil;
  }

  function seleccionar(fila) {
    raiz.querySelectorAll("[data-vista].is-selected").forEach(function (f) {
      f.classList.remove("is-selected");
      f.removeAttribute("aria-current");
    });
    if (fila) {
      fila.classList.add("is-selected");
      fila.setAttribute("aria-current", "true");
    }
  }

  function abrirPanel() {
    panel.hidden = false;
    raiz.classList.add("has-preview");
  }

  function cerrar() {
    turno += 1;
    seleccionar(null);
    contenido.hidden = true;
    contenido.innerHTML = "";
    panel.hidden = true;
    raiz.classList.remove("has-preview");
  }

  function mostrar(html) {
    contenido.innerHTML = html;
    contenido.hidden = false;
    abrirPanel();
    if (cerrarBoton && !amplio.matches) cerrarBoton.focus();
  }

  if (cerrarBoton) cerrarBoton.addEventListener("click", cerrar);
  if (amplio.addEventListener) {
    amplio.addEventListener("change", function () {
      if (!activo()) cerrar();
    });
  }

  raiz.addEventListener("click", function (event) {
    var fila = event.target.closest("[data-vista]");
    if (!fila || !activo()) return;
    // Abrir en pestaña nueva, etc.: se respeta el comportamiento del enlace.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button) return;
    event.preventDefault();
    if (fila.classList.contains("is-selected") && fila.tagName === "A" && fila.classList.contains("work-row")) {
      window.location.href = fila.href; // segundo clic sobre una fila: abrir
      return;
    }
    var mio = (turno += 1);
    seleccionar(fila);
    panel.classList.add("is-loading");
    fetch(fila.getAttribute("data-vista"), { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
      .then(function (respuesta) {
        if (!respuesta.ok) throw new Error(respuesta.status);
        return respuesta.text();
      })
      .then(function (html) {
        if (mio === turno) mostrar(html);
      })
      .catch(function () {
        // Si la vista previa falla, lo útil es llegar al detalle.
        if (mio === turno) window.location.href = fila.href;
      })
      .then(function () {
        if (mio === turno) panel.classList.remove("is-loading");
      });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !panel.hidden) cerrar();
  });
})();
