/* Órbita — Constructor visual del flujo (Studio › Flujo, Fase 4.B).

   Mejora progresiva sobre `templates/catalogo/_lienzo.html`. Sin JavaScript el lienzo se ve como columnas con sus
   tarjetas y el inspector muestra todos sus paneles (todas las operaciones son formularios normales).

   - Selección: cualquier `[data-seleccionar="<id de panel>"]` abre ese panel del inspector y resalta el nodo o la
     conexión. La selección se recuerda por servicio (sessionStorage) y se restaura tras guardar (las operaciones
     redirigen a la misma sección), así no se pierde el nodo seleccionado.
   - Conexiones: se dibujan en SVG con los datos del servidor (`#lienzo-datos`, que salen de `flujo_api`): solo lo
     que el motor ejecuta. Nada aquí decide la ejecución.
   - Arrastrar una tarjeta dentro de su columna cambia SOLO su posición visual (se guarda en el servidor). Soltarla
     en otra columna pide confirmación y usa la operación de dominio «Mover a la fase» del inspector (cambia la
     ejecución). Alt+↑/↓ con el foco en la tarjeta mueve la posición visual con el teclado.
   - Zoom (−, +, ajustar), desplazamiento (arrastrando el fondo o con el teclado) y contraer fases. */

(function () {
  "use strict";

  var lienzo = document.querySelector("[data-lienzo]");
  if (!lienzo) return;

  var SVG_NS = "http://www.w3.org/2000/svg";
  var viewport = lienzo.querySelector("[data-lienzo-viewport]");
  var contenido = lienzo.querySelector("[data-lienzo-contenido]");
  var svg = lienzo.querySelector("[data-lienzo-svg]");
  var inspector = lienzo.querySelector("[data-inspector]");
  var datosNodo = document.getElementById("lienzo-datos");
  var datos = { conexiones: [], fasesExtra: [] };
  try {
    datos = JSON.parse(datosNodo ? datosNodo.textContent : "{}") || datos;
  } catch (e) {
    /* sin datos: no se dibujan conexiones */
  }
  var servicio = lienzo.getAttribute("data-servicio");
  var puedeArrastrar = lienzo.getAttribute("data-arrastre") === "1";
  var editable = lienzo.getAttribute("data-editable") === "1";
  var claveSeleccion = "orbita:lienzo:" + servicio + ":seleccion";
  var claveContraidas = "orbita:lienzo:" + servicio + ":contraidas";
  var claveZoom = "orbita:lienzo:" + servicio + ":zoom";
  var escala = 1;

  function leer(clave, porDefecto) {
    try {
      var valor = window.sessionStorage.getItem(clave);
      return valor === null ? porDefecto : JSON.parse(valor);
    } catch (e) {
      return porDefecto;
    }
  }

  function guardar(clave, valor) {
    try {
      window.sessionStorage.setItem(clave, JSON.stringify(valor));
    } catch (e) {
      /* almacenamiento no disponible: la preferencia no se recuerda */
    }
  }

  /* ------------------------------- Selección e inspector ------------------------------- */

  var paneles = inspector ? Array.prototype.slice.call(inspector.querySelectorAll("[data-inspector-panel]")) : [];
  var vacio = inspector ? inspector.querySelector("[data-inspector-vacio]") : null;
  paneles.forEach(function (panel) {
    panel.hidden = true;
  });

  function marcar(idPanel) {
    lienzo.querySelectorAll(".is-seleccionado").forEach(function (el) {
      el.classList.remove("is-seleccionado");
    });
    var panel = idPanel ? document.getElementById(idPanel) : null;
    if (!panel) return;
    var nodo = panel.getAttribute("data-nodo-ref");
    if (nodo && document.getElementById(nodo)) document.getElementById(nodo).classList.add("is-seleccionado");
    var conexion = panel.getAttribute("data-conexion-ref");
    if (conexion) {
      svg.querySelectorAll('[data-conexion="' + conexion + '"]').forEach(function (el) {
        el.classList.add("is-seleccionado");
      });
    }
    var fase = idPanel.indexOf("inspector-fase-") === 0 ? document.getElementById("fase-" + idPanel.slice(15)) : null;
    if (fase) fase.classList.add("is-seleccionado");
  }

  function seleccionar(idPanel, origen) {
    if (!inspector) return;
    var panel = idPanel ? document.getElementById(idPanel) : null;
    if (!panel || !inspector.contains(panel)) {
      paneles.forEach(function (p) {
        p.hidden = true;
      });
      if (vacio) vacio.hidden = false;
      marcar(null);
      guardar(claveSeleccion, null);
      return;
    }
    paneles.forEach(function (p) {
      p.hidden = p !== panel;
    });
    if (vacio) vacio.hidden = true;
    var fase = origen && origen.getAttribute("data-fase-destino");
    if (fase) {
      var selector = panel.querySelector("[data-fase-select]");
      if (selector) selector.value = fase;
    }
    marcar(idPanel);
    guardar(claveSeleccion, idPanel);
    if (origen && !inspector.contains(origen)) inspector.scrollTop = 0;
  }

  lienzo.addEventListener("click", function (evento) {
    var disparador = evento.target.closest("[data-seleccionar]");
    if (disparador && lienzo.contains(disparador)) {
      if (arrastre && arrastre.movido) return;
      evento.preventDefault();
      seleccionar(disparador.getAttribute("data-seleccionar"), disparador);
      return;
    }
    if (evento.target.closest("[data-inspector-cerrar]")) {
      seleccionar(null);
    }
  });

  lienzo.addEventListener("keydown", function (evento) {
    if (evento.key === "Escape" && !evento.target.closest("input, textarea, select")) seleccionar(null);
  });

  /* ---------------------------------- Conexiones (SVG) ---------------------------------- */

  function posicion(elemento) {
    var x = 0;
    var y = 0;
    var actual = elemento;
    while (actual && actual !== contenido) {
      x += actual.offsetLeft;
      y += actual.offsetTop;
      actual = actual.offsetParent;
    }
    return { x: x, y: y, w: elemento.offsetWidth, h: elemento.offsetHeight };
  }

  function puntoVisible(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    var fase = el.closest(".lienzo-fase");
    if (fase && fase.classList.contains("is-contraida")) el = fase.querySelector(".lienzo-fase__cabecera");
    return posicion(el);
  }

  function crear(tipo, atributos) {
    var el = document.createElementNS(SVG_NS, tipo);
    Object.keys(atributos).forEach(function (clave) {
      el.setAttribute(clave, atributos[clave]);
    });
    return el;
  }

  function trayecto(a, b) {
    var mismaColumna = Math.abs(a.x - b.x) < 8;
    if (mismaColumna) {
      // Secuencia dentro de la fase: del borde inferior al superior (o un lazo si vuelve atrás).
      var x1 = a.x + a.w / 2;
      var y1 = a.y + a.h;
      var x2 = b.x + b.w / 2;
      var y2 = b.y;
      if (y2 > y1) return { d: "M" + x1 + "," + y1 + " C" + x1 + "," + (y1 + 24) + " " + x2 + "," + (y2 - 24) + " " + x2 + "," + y2, mx: x1, my: (y1 + y2) / 2 };
      var lado = a.x + a.w + 28;
      return {
        d: "M" + (a.x + a.w) + "," + (a.y + a.h / 2) + " C" + lado + "," + (a.y + a.h / 2) + " " + lado + "," + (b.y + b.h / 2) + " " + (b.x + b.w) + "," + (b.y + b.h / 2),
        mx: lado, my: (a.y + b.y + b.h) / 2,
      };
    }
    var adelante = b.x > a.x;
    var sx = adelante ? a.x + a.w : a.x;
    var ex = adelante ? b.x : b.x + b.w;
    var sy = a.y + a.h / 2;
    var ey = b.y + b.h / 2;
    var curva = Math.max(40, Math.abs(ex - sx) / 2);
    var c1 = adelante ? sx + curva : sx - curva;
    var c2 = adelante ? ex - curva : ex + curva;
    return { d: "M" + sx + "," + sy + " C" + c1 + "," + sy + " " + c2 + "," + ey + " " + ex + "," + ey, mx: (sx + ex) / 2, my: (sy + ey) / 2 };
  }

  function dibujar() {
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.setAttribute("width", contenido.scrollWidth);
    svg.setAttribute("height", contenido.scrollHeight);
    var defs = crear("defs", {});
    ["secuencia", "aprobada", "devuelta", "rechazada", "condicion", "otro_caso", "fase-extra"].forEach(function (estilo) {
      var marca = crear("marker", {
        id: "flecha-" + estilo, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7",
        orient: "auto-start-reverse",
      });
      marca.appendChild(crear("path", { d: "M0,0 L10,5 L0,10 z", class: "lienzo-flecha lienzo-flecha--" + estilo }));
      defs.appendChild(marca);
    });
    svg.appendChild(defs);

    (datos.fasesExtra || []).forEach(function (extra) {
      var a = puntoVisible(extra.de);
      var b = puntoVisible(extra.a);
      if (!a || !b) return;
      var t = trayecto({ x: a.x, y: a.y, w: a.w, h: 28 }, { x: b.x, y: b.y, w: b.w, h: 28 });
      var linea = crear("path", { d: t.d, class: "lienzo-conexion lienzo-conexion--fase-extra", "marker-end": "url(#flecha-fase-extra)" });
      var titulo = crear("title", {});
      titulo.textContent = "Conexión de la plantilla hacia «" + extra.nombre + "»: el motor no la sigue (solo la primera de cada fase).";
      linea.appendChild(titulo);
      svg.appendChild(linea);
    });

    (datos.conexiones || []).forEach(function (conexion) {
      var a = puntoVisible(conexion.de);
      var b = puntoVisible(conexion.a);
      if (!a || !b) return;
      var t = trayecto(a, b);
      var grupo = crear("g", { class: "lienzo-conexion-grupo", "data-conexion": conexion.id });
      var golpe = crear("path", { d: t.d, class: "lienzo-conexion__zona", "data-seleccionar": "inspector-" + conexion.id });
      var linea = crear("path", {
        d: t.d, class: "lienzo-conexion lienzo-conexion--" + conexion.estilo, "data-conexion": conexion.id,
        "marker-end": "url(#flecha-" + conexion.estilo + ")",
      });
      grupo.appendChild(linea);
      grupo.appendChild(golpe);
      if (conexion.etiqueta) {
        var texto = crear("text", { x: t.mx, y: t.my - 6, class: "lienzo-conexion__etiqueta", "text-anchor": "middle" });
        texto.textContent = conexion.etiqueta.length > 34 ? conexion.etiqueta.slice(0, 33) + "…" : conexion.etiqueta;
        grupo.appendChild(texto);
      }
      svg.appendChild(grupo);
    });
    marcar(leer(claveSeleccion, null));
  }

  svg &&
    svg.addEventListener("click", function (evento) {
      var zona = evento.target.closest("[data-seleccionar]");
      if (zona) seleccionar(zona.getAttribute("data-seleccionar"), zona);
    });

  /* --------------------------------------- Zoom --------------------------------------- */

  var sizer = document.createElement("div");
  sizer.className = "lienzo__escala";
  contenido.parentNode.insertBefore(sizer, contenido);
  sizer.appendChild(contenido);
  var valorZoom = lienzo.querySelector("[data-zoom-valor]");

  function aplicarZoom(nueva) {
    escala = Math.min(1.5, Math.max(0.4, Math.round(nueva * 100) / 100));
    contenido.style.transform = "scale(" + escala + ")";
    sizer.style.width = contenido.scrollWidth * escala + "px";
    sizer.style.height = contenido.scrollHeight * escala + "px";
    if (valorZoom) valorZoom.textContent = Math.round(escala * 100) + "%";
    guardar(claveZoom, escala);
  }

  lienzo.querySelectorAll("[data-zoom]").forEach(function (boton) {
    boton.addEventListener("click", function () {
      var accion = boton.getAttribute("data-zoom");
      if (accion === "ajustar") {
        aplicarZoom(Math.min(1, (viewport.clientWidth - 24) / Math.max(contenido.scrollWidth, 1)));
      } else {
        aplicarZoom(escala + 0.1 * parseInt(accion, 10));
      }
    });
  });

  /* ---------------------------------- Contraer fases ---------------------------------- */

  var contraidas = leer(claveContraidas, []);
  lienzo.querySelectorAll(".lienzo-fase").forEach(function (fase) {
    var boton = fase.querySelector("[data-fase-contraer]");
    if (contraidas.indexOf(fase.id) !== -1) {
      fase.classList.add("is-contraida");
      if (boton) boton.setAttribute("aria-expanded", "false");
    }
    if (!boton) return;
    boton.addEventListener("click", function () {
      var contraer = !fase.classList.contains("is-contraida");
      fase.classList.toggle("is-contraida", contraer);
      boton.setAttribute("aria-expanded", contraer ? "false" : "true");
      contraidas = contraidas.filter(function (id) {
        return id !== fase.id;
      });
      if (contraer) contraidas.push(fase.id);
      guardar(claveContraidas, contraidas);
      aplicarZoom(escala);
      dibujar();
    });
  });

  /* ------------------------- Posiciones visuales (guardar en servidor) ------------------------- */

  var csrf = lienzo.querySelector('input[name="csrfmiddlewaretoken"]');
  var pendientes = {};
  var temporizador = null;

  function avisar(texto, tipo) {
    if (window.Orbita && typeof window.Orbita.toast === "function") window.Orbita.toast(texto, tipo);
  }

  function guardarPosiciones() {
    var lista = Object.keys(pendientes).map(function (clave) {
      return { clave: clave, x: 0, y: pendientes[clave] };
    });
    pendientes = {};
    if (!lista.length) return;
    fetch(lienzo.getAttribute("data-posiciones-url"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf ? csrf.value : "" },
      body: JSON.stringify({ posiciones: lista }),
    })
      .then(function (respuesta) {
        if (!respuesta.ok) throw new Error("posiciones");
      })
      .catch(function () {
        avisar("No se pudo guardar la posición de la tarjeta. Solo es visual: el flujo no cambió.", "warning");
      });
  }

  function programarGuardado(tarjeta) {
    pendientes[tarjeta.getAttribute("data-clave")] = parseInt(tarjeta.getAttribute("data-y"), 10) || 0;
    window.clearTimeout(temporizador);
    temporizador = window.setTimeout(guardarPosiciones, 400);
  }

  function ajustarAltura(cuerpo) {
    var maximo = 0;
    cuerpo.querySelectorAll("[data-nodo]").forEach(function (t) {
      maximo = Math.max(maximo, t.offsetTop + t.offsetHeight);
    });
    cuerpo.style.height = Math.max(maximo + 24, 120) + "px";
  }

  /* -------------------------------------- Arrastre -------------------------------------- */

  var arrastre = null;

  function columnaBajo(x) {
    var encontrada = null;
    lienzo.querySelectorAll(".lienzo-fase").forEach(function (fase) {
      var r = fase.getBoundingClientRect();
      if (x >= r.left && x <= r.right) encontrada = fase;
    });
    return encontrada;
  }

  function moverDeFase(tarjeta, faseDestino) {
    var id = tarjeta.id.replace("nodo-", "");
    var panel = document.getElementById("inspector-bloque-" + id);
    var formulario = panel ? panel.querySelector(".inspector__mover") : null;
    var nombre = faseDestino.querySelector(".lienzo-fase__nombre");
    if (!editable || !formulario) {
      avisar("Para cambiar de fase, abre un borrador con «Editar configuración».", "info");
      return false;
    }
    var mensaje = "Mover «" + (tarjeta.querySelector(".lienzo-tarjeta__nombre") || {}).textContent +
      "» a «" + (nombre ? nombre.textContent : "otra fase") + "» cambia cuándo se ejecuta: quedará al final de esa fase (en el borrador). ¿Continuar?";
    if (!window.confirm(mensaje)) return false;
    formulario.querySelector("select[name='fase_id']").value = faseDestino.getAttribute("data-fase");
    formulario.setAttribute("data-confirmado", "1");
    formulario.submit();
    return true;
  }

  if (puedeArrastrar) {
    lienzo.addEventListener("pointerdown", function (evento) {
      var tarjeta = evento.target.closest("[data-nodo]");
      if (!tarjeta || evento.button !== 0) return;
      if (evento.target.closest("button, a, input, select, textarea") && !evento.target.closest(".lienzo-tarjeta__asa")) return;
      arrastre = {
        tarjeta: tarjeta, cuerpo: tarjeta.parentNode, inicioY: evento.clientY, inicioTop: tarjeta.offsetTop,
        movido: false, puntero: evento.pointerId,
      };
      tarjeta.setPointerCapture(evento.pointerId);
    });

    lienzo.addEventListener("pointermove", function (evento) {
      if (!arrastre || evento.pointerId !== arrastre.puntero) return;
      var delta = (evento.clientY - arrastre.inicioY) / escala;
      if (!arrastre.movido && Math.abs(delta) < 4 && !columnaFuera(evento.clientX)) return;
      arrastre.movido = true;
      arrastre.tarjeta.classList.add("is-arrastrando");
      var top = Math.max(0, Math.round(arrastre.inicioTop + delta));
      arrastre.tarjeta.style.top = top + "px";
      lienzo.querySelectorAll(".lienzo-fase.is-destino").forEach(function (f) {
        f.classList.remove("is-destino");
      });
      var destino = columnaBajo(evento.clientX);
      if (destino && destino !== arrastre.tarjeta.closest(".lienzo-fase")) destino.classList.add("is-destino");
      dibujar();
    });

    var columnaFuera = function (x) {
      var destino = columnaBajo(x);
      return arrastre && destino && destino !== arrastre.tarjeta.closest(".lienzo-fase");
    };

    var terminar = function (evento) {
      if (!arrastre || evento.pointerId !== arrastre.puntero) return;
      var actual = arrastre;
      lienzo.querySelectorAll(".lienzo-fase.is-destino").forEach(function (f) {
        f.classList.remove("is-destino");
      });
      actual.tarjeta.classList.remove("is-arrastrando");
      if (actual.movido) {
        var destino = columnaBajo(evento.clientX);
        if (destino && destino !== actual.tarjeta.closest(".lienzo-fase")) {
          actual.tarjeta.style.top = actual.inicioTop + "px";
          moverDeFase(actual.tarjeta, destino);
        } else {
          actual.tarjeta.setAttribute("data-y", actual.tarjeta.offsetTop);
          programarGuardado(actual.tarjeta);
        }
        ajustarAltura(actual.cuerpo);
        aplicarZoom(escala);
        dibujar();
      }
      window.setTimeout(function () {
        arrastre = null;
      }, 0);
    };
    lienzo.addEventListener("pointerup", terminar);
    lienzo.addEventListener("pointercancel", terminar);

    // Alternativa con teclado a la posición visual: Alt + ↑ / ↓ sobre la tarjeta.
    lienzo.addEventListener("keydown", function (evento) {
      if (!evento.altKey || (evento.key !== "ArrowUp" && evento.key !== "ArrowDown")) return;
      var tarjeta = evento.target.closest("[data-nodo]");
      if (!tarjeta) return;
      evento.preventDefault();
      var top = Math.max(0, tarjeta.offsetTop + (evento.key === "ArrowUp" ? -24 : 24));
      tarjeta.style.top = top + "px";
      tarjeta.setAttribute("data-y", top);
      ajustarAltura(tarjeta.parentNode);
      programarGuardado(tarjeta);
      aplicarZoom(escala);
      dibujar();
    });
  }

  /* ------------------------- Desplazar el lienzo arrastrando el fondo ------------------------- */

  var paneo = null;
  viewport.addEventListener("pointerdown", function (evento) {
    if (evento.button !== 0 || evento.target.closest("[data-nodo], button, a, input, select, textarea, .lienzo-fase__cabecera, [data-seleccionar]")) return;
    paneo = { x: evento.clientX, y: evento.clientY, left: viewport.scrollLeft, top: viewport.scrollTop };
    viewport.classList.add("is-desplazando");
  });
  window.addEventListener("pointermove", function (evento) {
    if (!paneo) return;
    viewport.scrollLeft = paneo.left - (evento.clientX - paneo.x);
    viewport.scrollTop = paneo.top - (evento.clientY - paneo.y);
  });
  window.addEventListener("pointerup", function () {
    paneo = null;
    viewport.classList.remove("is-desplazando");
  });

  /* -------------------------------------- Inicio -------------------------------------- */

  lienzo.classList.add("is-mejorado");
  aplicarZoom(leer(claveZoom, 1));
  dibujar();
  var previa = leer(claveSeleccion, null);
  if (previa && document.getElementById(previa)) seleccionar(previa);
  var redibujo = null;
  window.addEventListener("resize", function () {
    window.clearTimeout(redibujo);
    redibujo = window.setTimeout(function () {
      aplicarZoom(escala);
      dibujar();
    }, 120);
  });
  inspector &&
    inspector.addEventListener("toggle", function () {
      window.setTimeout(dibujar, 0);
    }, true);
})();
