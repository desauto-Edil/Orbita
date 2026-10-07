/* Trabajo — vista previa de Mi trabajo y resumen de la Cola (mejora progresiva).
   Sin este script, o en pantallas angostas, cada fila es un enlace normal a su
   detalle. Con él, en pantallas amplias, la fila abre un panel lateral con un
   fragmento que renderiza el servidor (que también decide si el usuario puede
   verlo); "Abrir" lleva al detalle completo. */
(function () {
  "use strict";

  var raiz = document.querySelector("[data-trabajo]");
  if (!raiz || !window.fetch || !window.matchMedia) return;

  var panel = raiz.querySelector("[data-trabajo-vista]");
  var vacio = raiz.querySelector("[data-trabajo-vacio]");
  var contenido = raiz.querySelector("[data-trabajo-contenido]");
  var amplio = window.matchMedia("(min-width: 1024px)");
  var turno = 0;

  function sincronizar() {
    panel.hidden = !amplio.matches;
    raiz.classList.toggle("has-preview", amplio.matches);
  }
  sincronizar();
  if (amplio.addEventListener) amplio.addEventListener("change", sincronizar);

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

  function mostrar(html) {
    contenido.innerHTML = html;
    contenido.hidden = false;
    vacio.hidden = true;
  }

  function cerrar() {
    turno += 1;
    seleccionar(null);
    contenido.hidden = true;
    contenido.innerHTML = "";
    vacio.hidden = false;
  }

  raiz.addEventListener("click", function (event) {
    var fila = event.target.closest("[data-vista]");
    if (!fila || !amplio.matches) return;
    // Abrir en pestaña nueva, etc.: se respeta el comportamiento del enlace.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button) return;
    event.preventDefault();
    if (fila.classList.contains("is-selected")) {
      window.location.href = fila.href; // segundo clic: abrir
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
    if (event.key === "Escape" && !contenido.hidden) cerrar();
  });
})();
