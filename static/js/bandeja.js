/* Bandeja compartida de tickets («Mi trabajo» y «Mis tickets») — inspector lateral (mejora progresiva).

   Sin este script cada fila es un enlace normal (al espacio del ticket o al borrador). Con él, elegir una fila pide
   al servidor el fragmento de solo lectura (`tickets:inspector`, que también decide si la persona puede verlo) y lo
   muestra a la derecha sin recargar: la búsqueda, el filtro y la posición de la lista no cambian. Un segundo clic
   sobre la fila elegida abre su destino; «Cerrar» o Escape devuelven el ancho completo y el foco a la fila.
   La fila elegida queda en la URL (`?sel=`) con `history.replaceState`, así recargar conserva la selección. */
(function () {
  "use strict";

  var raiz = document.querySelector("[data-bandeja]");
  if (!raiz || !window.fetch) return;

  var panel = raiz.querySelector("[data-inspector-panel]");
  var contenido = panel.querySelector("[data-inspector-contenido]");
  var cargando = panel.querySelector("[data-inspector-cargando]");
  var escritorio = window.matchMedia("(min-width: 1024px)");
  var movil = window.matchMedia("(max-width: 639px)");
  var turno = 0;
  var elegida = null;

  function filas() {
    return Array.prototype.slice.call(raiz.querySelectorAll(".bt-fila"));
  }

  function enlaceDe(fila) {
    return fila && fila.querySelector("[data-inspector]");
  }

  function recordar(id) {
    if (!window.history || !window.history.replaceState || !window.URL) return;
    var url = new URL(window.location.href);
    if (id) url.searchParams.set("sel", id);
    else url.searchParams.delete("sel");
    window.history.replaceState(window.history.state, "", url.toString());
  }

  function marcar(fila) {
    filas().forEach(function (f) {
      f.classList.remove("is-selected");
      var enlace = enlaceDe(f);
      if (enlace) enlace.removeAttribute("aria-current");
    });
    elegida = fila;
    if (fila) {
      fila.classList.add("is-selected");
      enlaceDe(fila).setAttribute("aria-current", "true");
    }
  }

  function abrirPanel() {
    panel.hidden = false;
    raiz.classList.add("has-inspector");
    document.documentElement.classList.toggle("bt-bloqueo", movil.matches);
  }

  function cerrar() {
    turno += 1;
    var anterior = elegida;
    marcar(null);
    panel.hidden = true;
    panel.classList.remove("is-loading");
    contenido.innerHTML = "";
    raiz.classList.remove("has-inspector");
    document.documentElement.classList.remove("bt-bloqueo");
    recordar(null);
    if (anterior && enlaceDe(anterior)) enlaceDe(anterior).focus({ preventScroll: true });
  }

  function mostrar(html, enfocar) {
    contenido.innerHTML = html;
    cargando.hidden = true;
    abrirPanel();
    // En escritorio el foco sigue en la fila (se puede seguir recorriendo la lista); superpuesto, va al panel.
    if (enfocar || !escritorio.matches) {
      var titulo = contenido.querySelector("#bt-ins-titulo");
      if (titulo) titulo.focus({ preventScroll: true });
    }
  }

  function elegir(fila, opciones) {
    var enlace = enlaceDe(fila);
    if (!enlace) return;
    var mio = (turno += 1);
    marcar(fila);
    recordar(fila.getAttribute("data-fila"));
    if (panel.hidden) {
      contenido.innerHTML = "";
      cargando.hidden = false;
      abrirPanel();
    }
    panel.classList.add("is-loading");
    fetch(enlace.getAttribute("data-inspector"), {
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" },
    })
      .then(function (respuesta) {
        if (!respuesta.ok) throw new Error(String(respuesta.status));
        return respuesta.text();
      })
      .then(function (html) {
        if (mio === turno) mostrar(html, opciones && opciones.enfocar);
      })
      .catch(function () {
        // Si el inspector falla, lo útil es llegar al ticket por el camino de siempre.
        if (mio === turno && !(opciones && opciones.silencioso)) window.location.href = enlace.href;
        else if (mio === turno) cerrar();
      })
      .then(function () {
        if (mio === turno) panel.classList.remove("is-loading");
      });
  }

  raiz.addEventListener("click", function (event) {
    if (event.target.closest("[data-inspector-cerrar]")) {
      event.preventDefault();
      cerrar();
      return;
    }
    var enlace = event.target.closest("[data-inspector]");
    if (!enlace) return;
    // Abrir en pestaña nueva, etc.: se respeta el comportamiento del enlace.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button) return;
    var fila = enlace.closest(".bt-fila");
    if (fila === elegida && !panel.hidden) return; // segundo clic: abre el destino (navegación normal)
    event.preventDefault();
    elegir(fila);
  });

  // Flechas arriba/abajo recorren las filas; con el inspector abierto, lo actualizan.
  raiz.addEventListener("keydown", function (event) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    var enlace = event.target.closest("[data-inspector]");
    if (!enlace) return;
    var lista = filas();
    var indice = lista.indexOf(enlace.closest(".bt-fila")) + (event.key === "ArrowDown" ? 1 : -1);
    if (indice < 0 || indice >= lista.length) return;
    event.preventDefault();
    var siguiente = lista[indice];
    enlaceDe(siguiente).focus();
    if (!panel.hidden && escritorio.matches) elegir(siguiente);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !panel.hidden) cerrar();
  });

  function alCambiarTamano() {
    if (!panel.hidden) document.documentElement.classList.toggle("bt-bloqueo", movil.matches);
  }
  if (movil.addEventListener) movil.addEventListener("change", alCambiarTamano);

  // Selección recordada en la URL (recarga o volver atrás).
  if (window.URL) {
    var sel = new URL(window.location.href).searchParams.get("sel");
    if (sel) {
      var fila = raiz.querySelector('.bt-fila[data-fila="' + sel.replace(/[^0-9]/g, "") + '"]');
      if (fila && escritorio.matches) elegir(fila, { silencioso: true });
      else if (!fila) recordar(null);
    }
  }
})();
