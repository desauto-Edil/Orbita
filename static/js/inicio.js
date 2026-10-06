/* Órbita — explorador de Inicio ("Ver todo"), V1.
   Mejora progresiva: sin JS, "Ver todo" y las categorías caen al catálogo
   (`catalogo:lista`) con los mismos filtros. (El buscador del hero ya no abre
   este explorador: es "¿Qué necesitas?", ver `necesidad.js`.) Con JS abren un
   <dialog> (foco atrapado, Escape y retorno de foco los gestiona el
   navegador) cuyo contenido el servidor entrega ya filtrado por visibilidad
   (`core:explorar`): Inicio nunca carga el catálogo completo. El cierre por
   botón/fondo lo resuelve app.js (data-modal-close). Sin dependencias. */

(function () {
  "use strict";

  var dialogo = document.getElementById("explorador");
  if (!dialogo || typeof dialogo.showModal !== "function") return;

  var campo = document.getElementById("explorador-q");
  var resultados = document.getElementById("explorador-resultados");
  var chips = dialogo.querySelectorAll(".chip[data-categoria]");
  var url = dialogo.getAttribute("data-url");
  var categoria = "";
  var temporizador = null;
  var peticion = 0;

  function marcarChip(valor) {
    categoria = valor || "";
    Array.prototype.forEach.call(chips, function (chip) {
      var activo = chip.getAttribute("data-categoria") === categoria;
      chip.classList.toggle("is-active", activo);
      chip.setAttribute("aria-pressed", activo ? "true" : "false");
    });
  }

  function cargar() {
    var numero = ++peticion;
    var params = new URLSearchParams();
    if (campo.value.trim()) params.set("q", campo.value.trim());
    if (categoria) params.set("categoria", categoria);
    resultados.setAttribute("aria-busy", "true");
    fetch(url + (params.toString() ? "?" + params.toString() : ""), {
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" },
    })
      .then(function (respuesta) {
        if (!respuesta.ok) throw new Error("http " + respuesta.status);
        return respuesta.text();
      })
      .then(function (html) {
        // Una respuesta vieja no pisa a una más reciente.
        if (numero === peticion) resultados.innerHTML = html;
      })
      .catch(function () {
        if (numero === peticion) {
          resultados.textContent = "No pudimos cargar los resultados. Intenta de nuevo.";
        }
      })
      .then(function () {
        if (numero === peticion) resultados.removeAttribute("aria-busy");
      });
  }

  function abrir(texto, idCategoria) {
    campo.value = texto || "";
    marcarChip(idCategoria);
    if (!dialogo.open) dialogo.showModal();
    cargar();
    campo.focus();
  }

  document.addEventListener("click", function (evento) {
    var disparador = evento.target.closest("[data-explorer-open]");
    if (!disparador) return;
    // Ctrl/Cmd/Shift/clic medio: comportamiento normal del enlace (nueva pestaña).
    if (evento.ctrlKey || evento.metaKey || evento.shiftKey || evento.button === 1) return;
    evento.preventDefault();
    abrir("", disparador.getAttribute("data-categoria") || "");
  });

  campo.addEventListener("input", function () {
    window.clearTimeout(temporizador);
    temporizador = window.setTimeout(cargar, 250);
  });

  Array.prototype.forEach.call(chips, function (chip) {
    chip.addEventListener("click", function () {
      marcarChip(chip.getAttribute("data-categoria"));
      cargar();
    });
  });
})();
