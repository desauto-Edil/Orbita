/* «Mi equipo» — búsqueda, filtros, orden y vista (tarjetas / lista) de la vista general, y filtros de la tabla
   del detalle. Todo es LOCAL sobre lo que el servidor ya pintó: no consulta nada ni decide permisos. Sin este
   script se ve la cuadrícula completa y la tabla completa. La vista elegida se recuerda en `localStorage`. */
(function () {
  "use strict";

  var CLAVE_VISTA = "orbita.miEquipo.vista";

  function leer(clave) {
    try { return window.localStorage.getItem(clave); } catch (e) { return null; }
  }

  function guardar(clave, valor) {
    try { window.localStorage.setItem(clave, valor); } catch (e) { /* sin almacenamiento: no se recuerda */ }
  }

  /* ---------------- Vista general ---------------- */
  var lista = document.querySelector("[data-eq-personas]");
  if (lista) {
    var herramientas = document.querySelector("[data-eq-herramientas]");
    if (herramientas) herramientas.hidden = false;
    var personas = Array.prototype.slice.call(lista.querySelectorAll("[data-eq-persona]"));
    var buscar = document.querySelector("[data-eq-buscar]");
    var area = document.querySelector("[data-eq-area]");
    var situacion = document.querySelector("[data-eq-situacion]");
    var orden = document.querySelector("[data-eq-orden]");
    var vacio = document.querySelector("[data-eq-vacio]");
    var botonesVista = Array.prototype.slice.call(document.querySelectorAll("[data-eq-vista]"));

    var numero = function (el, attr) { return parseInt(el.getAttribute(attr), 10) || 0; };

    var aplicar = function () {
      var texto = (buscar && buscar.value || "").trim().toLowerCase();
      var areaElegida = area ? area.value : "";
      var sit = situacion ? situacion.value : "";
      var visibles = 0;
      personas.forEach(function (p) {
        var ok = (!texto || p.getAttribute("data-nombre").indexOf(texto) !== -1)
          && (!areaElegida || p.getAttribute("data-area") === areaElegida)
          && (!sit || (sit === "vencidos" && numero(p, "data-vencidos") > 0)
            || (sit === "pendientes" && numero(p, "data-pendientes") > 0)
            || (sit === "libres" && numero(p, "data-pendientes") === 0));
        p.hidden = !ok;
        if (ok) visibles += 1;
      });
      if (vacio) vacio.hidden = visibles > 0;
    };

    var ordenar = function () {
      var criterio = orden ? orden.value : "nombre";
      var atributo = { vencidos: "data-vencidos", pendientes: "data-pendientes", tickets: "data-tickets" }[criterio];
      personas.slice().sort(function (a, b) {
        if (atributo) {
          var diferencia = numero(b, atributo) - numero(a, atributo);
          if (diferencia !== 0) return diferencia;
        }
        return a.getAttribute("data-nombre").localeCompare(b.getAttribute("data-nombre"));
      }).forEach(function (p) { lista.appendChild(p); });
    };

    var elegirVista = function (vista) {
      var esLista = vista === "lista";
      lista.classList.toggle("is-lista", esLista);
      botonesVista.forEach(function (b) {
        var activo = b.getAttribute("data-eq-vista") === (esLista ? "lista" : "cuadricula");
        b.classList.toggle("is-activo", activo);
        b.setAttribute("aria-pressed", activo ? "true" : "false");
      });
    };

    if (buscar) buscar.addEventListener("input", aplicar);
    [area, situacion].forEach(function (c) { if (c) c.addEventListener("change", aplicar); });
    if (orden) orden.addEventListener("change", ordenar);
    botonesVista.forEach(function (b) {
      b.addEventListener("click", function () {
        var vista = b.getAttribute("data-eq-vista");
        elegirVista(vista);
        guardar(CLAVE_VISTA, vista);
      });
    });
    elegirVista(leer(CLAVE_VISTA) || "cuadricula");
  }

  /* ---------------- Tabla del detalle ---------------- */
  var tabla = document.querySelector("[data-eq-tabla]");
  if (tabla) {
    var barra = document.querySelector("[data-eq-tabla-barra]");
    if (barra) barra.hidden = false;
    var filas = Array.prototype.slice.call(tabla.querySelectorAll("[data-eq-fila]"));
    var segmentos = Array.prototype.slice.call(document.querySelectorAll("[data-eq-seg]"));
    var buscarTabla = document.querySelector("[data-eq-tabla-buscar]");
    var vacioTabla = document.querySelector("[data-eq-tabla-vacio]");
    var segmento = "";

    var filtrar = function () {
      var texto = (buscarTabla && buscarTabla.value || "").trim().toLowerCase();
      var visibles = 0;
      filas.forEach(function (f) {
        var situaciones = f.getAttribute("data-situacion").split(" ");
        var ok = (!segmento || situaciones.indexOf(segmento) !== -1)
          && (!texto || f.getAttribute("data-texto").indexOf(texto) !== -1);
        f.hidden = !ok;
        if (ok) visibles += 1;
      });
      if (vacioTabla) vacioTabla.hidden = visibles > 0;
    };

    segmentos.forEach(function (s) {
      s.addEventListener("click", function () {
        segmento = s.getAttribute("data-eq-seg");
        segmentos.forEach(function (o) {
          var activo = o === s;
          o.classList.toggle("is-active", activo);
          o.setAttribute("aria-pressed", activo ? "true" : "false");
        });
        filtrar();
      });
    });
    if (buscarTabla) buscarTabla.addEventListener("input", filtrar);
  }
})();
