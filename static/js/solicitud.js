/* Órbita — experiencia de solicitud (V2). Sin dependencias.

   Mejora progresiva sobre un formulario que ya funciona sin JS: el servidor
   renderiza todo (campos, vista previa inicial, progreso) y decide qué es
   válido. Este script solo añade:

   1. Protección contra doble envío (`form[data-sol-guard]`).
   2. Vista previa viva (construida con nodos de texto: nunca interpreta HTML)
      y su apertura/cierre (columna en desktop, hoja inferior en pantallas
      angostas).
   3. Progreso de campos obligatorios completados.
   4. Campos condicionales: NO evalúa reglas. Cuando cambia un campo que es
      origen de alguna regla (`data-regla-origen`) pregunta al servidor
      (`data-estado-url`) qué campos son visibles/requeridos y lo aplica.
   5. Lista de adjuntos elegidos, aviso de cambios sin guardar y foco en el
      primer error.
   6. Contador de textos largos con límite real y aviso inmediato (no
      bloqueante) de número/fecha/correo/URL a partir de la propia validación
      del navegador (`input.validity`, alimentada por los atributos HTML que el
      servidor ya renderiza): no replica ninguna regla; el servidor decide. */

(function () {
  "use strict";

  function qsa(raiz, selector) {
    return Array.prototype.slice.call(raiz.querySelectorAll(selector));
  }

  function el(etiqueta, clase, texto) {
    var nodo = document.createElement(etiqueta);
    if (clase) nodo.className = clase;
    if (texto !== undefined && texto !== null) nodo.textContent = texto;
    return nodo;
  }

  var saliendo = false; // se está enviando/abandonando a propósito: sin aviso

  /* ------------------ 1. Protección contra doble envío ------------------ */

  function restablecerEnvios() {
    qsa(document, "form[data-sol-guard]").forEach(function (form) {
      form.removeAttribute("data-sol-enviando");
      form.removeAttribute("aria-busy");
      qsa(form, 'button[type="submit"]').forEach(function (boton) {
        boton.disabled = false;
        boton.classList.remove("is-loading");
      });
    });
    saliendo = false;
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !form.matches) return;
    if (form.matches("[data-sol-guard]")) {
      if (form.getAttribute("data-sol-enviando") === "1") {
        event.preventDefault();
        return;
      }
      form.setAttribute("data-sol-enviando", "1");
      form.setAttribute("aria-busy", "true");
      var enviador = event.submitter;
      // Después del evento: los datos del formulario (incluido `formaction`)
      // ya se tomaron y deshabilitar los botones no los altera.
      window.setTimeout(function () {
        qsa(form, 'button[type="submit"]').forEach(function (boton) {
          boton.disabled = true;
        });
        if (enviador) enviador.classList.add("is-loading");
      }, 0);
    }
    if (!event.defaultPrevented) saliendo = true;
  });

  /* ---------------- 2–5. Espacio de trabajo del formulario ---------------- */

  var raiz = document.querySelector("[data-solicitud]");
  var formulario = document.getElementById("sol-form");

  window.addEventListener("pageshow", function (event) {
    if (event.persisted) restablecerEnvios(); // volver con el botón Atrás
  });

  if (!raiz || !formulario) return;

  var urlEstado = raiz.getAttribute("data-estado-url");
  var campos = qsa(formulario, "[data-sol-field]");
  var tieneOrigenes = campos.some(function (c) {
    return c.hasAttribute("data-regla-origen");
  });
  var consultaAncha = window.matchMedia("(min-width: 900px)");
  var sucio = false;

  raiz.classList.add("is-enhanced");

  /* ---- Lectura de valores (para vista previa y progreso) ---- */

  function textoDeOpcion(input) {
    var etiqueta = input.closest(".sol-choice").querySelector(".sol-choice__label");
    return etiqueta ? etiqueta.textContent.trim() : "";
  }

  function fechaLarga(iso) {
    var partes = iso.split("-");
    if (partes.length !== 3) return iso;
    var fecha = new Date(Number(partes[0]), Number(partes[1]) - 1, Number(partes[2]));
    if (isNaN(fecha.getTime())) return iso;
    return fecha.toLocaleDateString("es-CO", { day: "numeric", month: "long", year: "numeric" });
  }

  function fechaHoraLarga(valor) {
    var fecha = new Date(valor);
    if (isNaN(fecha.getTime())) return valor;
    return fecha.toLocaleString("es-CO", {
      day: "numeric",
      month: "long",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function nombresDeArchivos(campo) {
    var nombres = [];
    qsa(campo, "[data-sol-file-guardado]").forEach(function (item) {
      var quitar = item.querySelector('input[type="checkbox"]');
      var enlace = item.querySelector(".sol-file__name");
      var reemplazado = campo.querySelector('input[type="file"]').files.length > 0;
      if (enlace && !(quitar && quitar.checked) && !reemplazado) nombres.push(enlace.textContent.trim());
    });
    var input = campo.querySelector('input[type="file"]');
    if (input && input.files) {
      Array.prototype.forEach.call(input.files, function (archivo) {
        nombres.push(archivo.name);
      });
    }
    return nombres;
  }

  /* Fila presentable de un campo, o null si no tiene respuesta. Misma regla
     que `solicitud._fila_resumen` del servidor (los booleanos siempre
     cuentan: "No" también es una respuesta). */
  function filaDe(campo) {
    var control = campo.getAttribute("data-control");
    var etiqueta = campo.getAttribute("data-etiqueta");
    var seleccionados;

    if (control === "tarjetas") {
      var elegida = campo.querySelector("input:checked");
      return elegida ? { etiqueta: etiqueta, tipo: "texto", texto: textoDeOpcion(elegida) } : null;
    }
    if (control === "chips") {
      seleccionados = qsa(campo, "input:checked").map(textoDeOpcion);
      return seleccionados.length ? { etiqueta: etiqueta, tipo: "lista", valores: seleccionados } : null;
    }
    if (control === "switch") {
      return { etiqueta: etiqueta, tipo: "texto", texto: campo.querySelector("input").checked ? "Sí" : "No" };
    }
    if (control === "select") {
      var select = campo.querySelector("select");
      if (!select.value) return null;
      return { etiqueta: etiqueta, tipo: "texto", texto: select.options[select.selectedIndex].textContent.trim() };
    }
    if (control === "multiselect") {
      seleccionados = qsa(campo, "option")
        .filter(function (o) {
          return o.selected;
        })
        .map(function (o) {
          return o.textContent.trim();
        });
      return seleccionados.length ? { etiqueta: etiqueta, tipo: "lista", valores: seleccionados } : null;
    }
    if (control === "archivo") {
      var nombres = nombresDeArchivos(campo);
      return nombres.length ? { etiqueta: etiqueta, tipo: "lista", valores: nombres } : null;
    }

    var entrada = campo.querySelector("input, textarea");
    var texto = entrada ? entrada.value.trim() : "";
    if (!texto) return null;
    if (control === "fecha") texto = fechaLarga(texto);
    else if (control === "fecha_hora") texto = fechaHoraLarga(texto);
    return { etiqueta: etiqueta, tipo: control === "area" ? "largo" : "texto", texto: texto };
  }

  /* ---- Vista previa ---- */

  var listaResumen = raiz.querySelector("[data-sol-summary-list]");
  var avisoVacio = raiz.querySelector("[data-sol-summary-empty]");

  function renderVistaPrevia() {
    if (!listaResumen) return;
    var filas = [];
    campos.forEach(function (campo) {
      if (campo.hidden) return;
      var fila = filaDe(campo);
      if (fila) filas.push(fila);
    });

    while (listaResumen.firstChild) listaResumen.removeChild(listaResumen.firstChild);
    filas.forEach(function (fila) {
      var contenedor = el("div", "sol-summary__row");
      contenedor.appendChild(el("dt", null, fila.etiqueta));
      var dd = el("dd");
      if (fila.tipo === "lista") {
        var ul = el("ul", "sol-summary__chips");
        fila.valores.forEach(function (valor) {
          ul.appendChild(el("li", null, valor));
        });
        dd.appendChild(ul);
      } else if (fila.tipo === "largo") {
        dd.appendChild(el("span", "sol-summary__long", fila.texto));
      } else {
        dd.textContent = fila.texto;
      }
      contenedor.appendChild(dd);
      listaResumen.appendChild(contenedor);
    });
    if (avisoVacio) avisoVacio.hidden = filas.length > 0;
  }

  /* ---- Progreso (solo obligatorios visibles, según el servidor) ---- */

  var textoProgreso = raiz.querySelector("[data-sol-progress-text]");
  var barraProgreso = raiz.querySelector("[data-sol-progress-bar]");
  var relleno = raiz.querySelector("[data-sol-progress-fill]");

  function renderProgreso() {
    var requeridos = campos.filter(function (c) {
      return !c.hidden && c.getAttribute("data-requerido") === "1";
    });
    var hechos = requeridos.filter(function (c) {
      return filaDe(c) !== null;
    }).length;
    campos.forEach(function (c) {
      var completo =
        c.getAttribute("data-control") !== "switch" &&
        !c.hidden &&
        c.getAttribute("data-requerido") === "1" &&
        filaDe(c) !== null;
      c.classList.toggle("is-complete", completo);
    });
    var total = requeridos.length;
    var porcentaje = total ? Math.round((100 * hechos) / total) : 100;
    if (textoProgreso) {
      textoProgreso.textContent = total
        ? hechos + " de " + total + (total === 1 ? " campo obligatorio completado" : " campos obligatorios completados")
        : "Los campos de este formulario son opcionales";
    }
    if (barraProgreso) barraProgreso.setAttribute("aria-valuenow", String(porcentaje));
    if (relleno) relleno.style.width = porcentaje + "%";
  }

  /* ---- Abrir / ocultar la vista previa ---- */

  var botonAbrir = raiz.querySelector("[data-sol-preview-open]");
  var etiquetaAbrir = raiz.querySelector("[data-sol-preview-open-label]");
  var botonCerrar = raiz.querySelector("[data-sol-preview-close]");
  var fondo = raiz.querySelector("[data-sol-preview-backdrop]");
  var CLAVE_PREFERENCIA = "orbita.solicitud.vista_previa";

  function leerPreferencia() {
    try {
      return window.localStorage.getItem(CLAVE_PREFERENCIA);
    } catch (error) {
      return null;
    }
  }

  function guardarPreferencia(valor) {
    try {
      window.localStorage.setItem(CLAVE_PREFERENCIA, valor);
    } catch (error) {
      /* sin almacenamiento: la preferencia simplemente no se recuerda */
    }
  }

  function fijarVistaPrevia(abierta, porUsuario) {
    var ancha = consultaAncha.matches;
    raiz.setAttribute("data-preview", abierta ? "open" : "closed");
    if (ancha && porUsuario) guardarPreferencia(abierta ? "open" : "closed");
    if (botonAbrir) {
      botonAbrir.setAttribute("aria-expanded", abierta ? "true" : "false");
      botonAbrir.hidden = ancha && abierta; // en pantalla ancha solo se ofrece "Mostrar" si está oculta
    }
    if (etiquetaAbrir) etiquetaAbrir.textContent = ancha ? "Mostrar vista previa" : "Vista previa";
    if (botonCerrar) {
      botonCerrar.hidden = false;
      botonCerrar.textContent = ancha ? "Ocultar" : "Cerrar";
    }
    if (fondo) fondo.hidden = ancha || !abierta;
    // Hoja inferior abierta: lo que queda detrás es inerte (foco y lectores de
    // pantalla). La vista previa vive dentro del <form>, así que se inertan
    // sus hermanas, no el formulario entero.
    var inerte = !ancha && abierta;
    qsa(raiz, ".sol-head, .sol-main, .sol-actions").forEach(function (zona) {
      zona.inert = inerte;
    });
    if (porUsuario) {
      if (abierta && !ancha && botonCerrar) botonCerrar.focus();
      else if (!abierta && botonAbrir && !botonAbrir.hidden) botonAbrir.focus();
    }
  }

  function vistaPreviaPorDefecto() {
    return consultaAncha.matches ? leerPreferencia() !== "closed" : false;
  }

  if (botonAbrir) {
    botonAbrir.addEventListener("click", function () {
      fijarVistaPrevia(true, true);
    });
  }
  if (botonCerrar) {
    botonCerrar.addEventListener("click", function () {
      fijarVistaPrevia(false, true);
    });
  }
  if (fondo) {
    fondo.addEventListener("click", function () {
      fijarVistaPrevia(false, true);
    });
  }
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !consultaAncha.matches && raiz.getAttribute("data-preview") === "open") {
      fijarVistaPrevia(false, true);
    }
  });
  if (consultaAncha.addEventListener) {
    consultaAncha.addEventListener("change", function () {
      fijarVistaPrevia(vistaPreviaPorDefecto(), false);
    });
  }

  /* ---- Campos condicionales: el servidor decide ---- */

  var temporizador = null;
  var secuencia = 0;
  var controlador = null;

  function datosDelFormularioSinArchivos() {
    var datos = new URLSearchParams();
    new FormData(formulario).forEach(function (valor, clave) {
      if (typeof valor === "string") datos.append(clave, valor);
    });
    return datos;
  }

  function controlesDe(campo) {
    return qsa(campo, "input, select, textarea");
  }

  function aplicarEstados(mapa) {
    campos.forEach(function (campo) {
      var estado = mapa[campo.getAttribute("data-campo")];
      if (!estado) return;
      campo.setAttribute("data-requerido", estado.requerido ? "1" : "0");
      qsa(campo, "[data-sol-target]").forEach(function (destino) {
        if (destino.hasAttribute("aria-required")) {
          destino.setAttribute("aria-required", estado.requerido ? "true" : "false");
        }
      });

      if (estado.visible && campo.hidden) {
        campo.hidden = false;
        controlesDe(campo).forEach(function (control) {
          control.disabled = false;
        });
        campo.classList.add("is-entering");
        window.setTimeout(function () {
          campo.classList.remove("is-entering");
        }, 400);
      } else if (!estado.visible && !campo.hidden) {
        campo.hidden = true;
        controlesDe(campo).forEach(function (control) {
          control.disabled = true;
        });
      }
    });
    actualizarBotonesQuitar();
    renderProgreso();
    renderVistaPrevia();
  }

  function consultarEstados() {
    if (!urlEstado || !window.fetch) return;
    var mia = ++secuencia;
    if (controlador) controlador.abort();
    controlador = window.AbortController ? new AbortController() : null;
    window
      .fetch(urlEstado, {
        method: "POST",
        body: datosDelFormularioSinArchivos(),
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        signal: controlador ? controlador.signal : undefined,
      })
      .then(function (respuesta) {
        return respuesta.ok ? respuesta.json() : null;
      })
      .then(function (datos) {
        if (datos && mia === secuencia) aplicarEstados(datos.campos || {});
      })
      .catch(function () {
        /* Cancelada o sin red: el servidor vuelve a decidir al guardar/revisar. */
      });
  }

  function programarConsulta(espera) {
    window.clearTimeout(temporizador);
    temporizador = window.setTimeout(consultarEstados, espera);
  }

  /* ---- "Quitar selección" en tarjetas opcionales ---- */

  function actualizarBotonesQuitar() {
    campos.forEach(function (campo) {
      if (campo.getAttribute("data-control") !== "tarjetas") return;
      var boton = campo.querySelector("[data-sol-clear]");
      if (!boton) return;
      var haySeleccion = !!campo.querySelector("input:checked");
      boton.hidden = !haySeleccion || campo.getAttribute("data-requerido") === "1";
    });
  }

  formulario.addEventListener("click", function (event) {
    var boton = event.target.closest("[data-sol-clear]");
    if (!boton) return;
    var campo = boton.closest("[data-sol-field]");
    var marcados = qsa(campo, "input:checked");
    marcados.forEach(function (input) {
      input.checked = false;
    });
    if (marcados.length) marcados[0].dispatchEvent(new Event("change", { bubbles: true }));
  });

  /* ---- Adjuntos elegidos ---- */

  function formatearBytes(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1).replace(".", ",") + " MB";
  }

  function renderArchivosElegidos(input) {
    var contenedor = input.closest("[data-sol-upload]");
    var lista = contenedor.querySelector("[data-sol-files]");
    qsa(lista, ".sol-file--nuevo").forEach(function (item) {
      item.remove();
    });
    var guardado = lista.querySelector("[data-sol-file-guardado]");
    if (guardado) guardado.classList.toggle("is-reemplazado", input.files.length > 0);

    Array.prototype.forEach.call(input.files, function (archivo) {
      var item = el("li", "sol-file sol-file--nuevo");
      var icono = el("span", "sol-file__icon");
      icono.setAttribute("aria-hidden", "true");
      icono.innerHTML = '<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-file"/></svg>';
      item.appendChild(icono);
      item.appendChild(el("span", "sol-file__name", archivo.name));
      item.appendChild(el("span", "sol-file__size", formatearBytes(archivo.size)));
      var quitar = el("button", "sol-file__remove");
      quitar.type = "button";
      quitar.setAttribute("aria-label", "Quitar " + archivo.name);
      quitar.innerHTML = '<svg class="icon icon--sm" aria-hidden="true" focusable="false"><use href="#i-x"/></svg>';
      quitar.addEventListener("click", function () {
        input.value = "";
        renderArchivosElegidos(input);
        alCambiar({ target: input, type: "change" });
        input.focus();
      });
      item.appendChild(quitar);
      if (guardado) item.appendChild(el("span", "sol-file__note", "Reemplazará al archivo guardado."));
      lista.insertBefore(item, lista.firstChild);
    });
  }

  /* ---- Contador de textos largos con límite real ---- */

  function actualizarContador(area) {
    var campo = area.closest("[data-sol-field]");
    var contador = campo ? campo.querySelector("[data-sol-contador]") : null;
    var limite = Number(area.getAttribute("data-sol-limite"));
    if (!contador || !limite) return;
    // El servidor cuenta cada salto de línea como 2 caracteres (CRLF).
    var largo = area.value.replace(/\r?\n/g, "\r\n").length;
    contador.textContent = largo + " / " + limite + (largo > limite ? " · te pasas por " + (largo - limite) : "");
    contador.classList.toggle("is-over", largo > limite);
  }

  /* ---- Aviso inmediato (no bloqueante) de número, fecha, correo y URL ---- */

  function mensajeDeValidez(entrada) {
    var validez = entrada.validity;
    var tipo = entrada.type;
    if (validez.rangeUnderflow) {
      return tipo === "number"
        ? "El valor debe ser mayor o igual a " + entrada.min + "."
        : "La fecha debe ser posterior o igual al " + fechaLarga(entrada.min.slice(0, 10)) + ".";
    }
    if (validez.rangeOverflow) {
      return tipo === "number"
        ? "El valor debe ser menor o igual a " + entrada.max + "."
        : "La fecha debe ser anterior o igual al " + fechaLarga(entrada.max.slice(0, 10)) + ".";
    }
    if (validez.stepMismatch) return "Este campo no admite decimales.";
    if (validez.badInput) return tipo === "number" ? "Escribe un número válido." : "Escribe una fecha válida.";
    if (validez.typeMismatch) {
      return tipo === "email"
        ? "Introduce una dirección de correo válida."
        : "Introduce una URL válida, por ejemplo https://ejemplo.com.";
    }
    return "";
  }

  function avisoVivo(entrada) {
    var campo = entrada.closest("[data-sol-field]");
    var aviso = campo ? campo.querySelector("[data-sol-aviso]") : null;
    if (!aviso || entrada.disabled) return;
    var mensaje = entrada.value === "" && !entrada.validity.badInput ? "" : mensajeDeValidez(entrada);
    aviso.querySelector("[data-sol-aviso-texto]").textContent = mensaje;
    aviso.hidden = !mensaje;
    campo.classList.toggle("has-live", !!mensaje);
    if (mensaje) entrada.setAttribute("aria-invalid", "true");
    else if (!campo.classList.contains("has-error")) entrada.removeAttribute("aria-invalid");
  }

  /* ---- Cambios sin guardar ---- */

  var estadoBorrador = raiz.querySelector("[data-sol-status]");

  function marcarSucio() {
    if (sucio) return;
    sucio = true;
    if (estadoBorrador) {
      estadoBorrador.classList.remove("is-saved", "is-new");
      estadoBorrador.classList.add("is-dirty");
      estadoBorrador.textContent = "Cambios sin guardar";
    }
  }

  window.addEventListener("beforeunload", function (event) {
    if (!sucio || saliendo) return;
    event.preventDefault();
    event.returnValue = "";
  });

  /* ---- Errores del servidor: se limpian al corregir el campo ---- */

  function limpiarError(destino) {
    var campo = destino && destino.closest ? destino.closest("[data-sol-field]") : null;
    if (!campo || !campo.classList.contains("has-error")) return;
    campo.classList.remove("has-error");
    var mensaje = campo.querySelector("[data-sol-error]");
    var idMensaje = mensaje ? mensaje.id : null;
    if (mensaje) mensaje.remove();
    qsa(campo, "[aria-invalid]").forEach(function (nodo) {
      nodo.removeAttribute("aria-invalid");
    });
    qsa(campo, "[aria-describedby]").forEach(function (nodo) {
      var restantes = nodo
        .getAttribute("aria-describedby")
        .split(/\s+/)
        .filter(function (id) {
          return id && id !== idMensaje;
        });
      if (restantes.length) nodo.setAttribute("aria-describedby", restantes.join(" "));
      else nodo.removeAttribute("aria-describedby");
    });
  }

  /* ---- Un solo manejador para todo cambio del formulario ---- */

  function alCambiar(event) {
    var destino = event.target;
    marcarSucio();
    limpiarError(destino);

    if (destino.matches && destino.matches('.sol-field input[type="checkbox"][role="switch"]')) {
      var estado = destino.closest(".switch").querySelector("[data-sol-switch-state]");
      if (estado) estado.textContent = destino.checked ? "Sí" : "No";
    }
    if (destino.matches && destino.matches('[data-sol-upload] input[type="file"]')) {
      renderArchivosElegidos(destino);
    }
    if (destino.matches && destino.matches("textarea[data-sol-limite]")) actualizarContador(destino);
    var campoAviso = destino.closest ? destino.closest("[data-sol-field]") : null;
    var nodoAviso = campoAviso ? campoAviso.querySelector("[data-sol-aviso]") : null;
    if (nodoAviso && destino.matches && destino.matches("input")) {
      // Se evalúa al confirmar el valor (`change`, al salir del campo) o, si ya
      // hay un aviso visible, mientras se corrige para que desaparezca solo.
      if (event.type === "change" || !nodoAviso.hidden) avisoVivo(destino);
    }

    actualizarBotonesQuitar();
    renderProgreso();
    renderVistaPrevia();

    var campo = destino.closest ? destino.closest("[data-sol-field]") : null;
    if (campo && campo.hasAttribute("data-regla-origen")) {
      programarConsulta(event.type === "change" ? 0 : 250);
    }
  }

  formulario.addEventListener("input", alCambiar);
  formulario.addEventListener("change", alCambiar);

  /* ---- Arranque ---- */

  function enfocarPrimerError() {
    var campo = formulario.querySelector(".sol-field.has-error:not([hidden])");
    if (!campo) return;
    var control = campo.querySelector("input:not([disabled]), select, textarea");
    if (campo.scrollIntoView) campo.scrollIntoView({ block: "center" });
    if (control) control.focus({ preventScroll: true });
  }

  qsa(formulario, "textarea[data-sol-limite]").forEach(actualizarContador);
  renderProgreso();
  renderVistaPrevia();
  actualizarBotonesQuitar();
  fijarVistaPrevia(vistaPreviaPorDefecto(), false);
  if (tieneOrigenes) consultarEstados(); // alinea con el servidor si el navegador restauró valores
  enfocarPrimerError();
})();
