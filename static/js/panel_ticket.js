/* Panel contextual del ticket (Conversación | Historial) y chat en tiempo real — mejora progresiva.

   Sin este script las pestañas y accesos son enlaces (`?panel=conversacion|historial`) y el panel llega abierto
   desde el servidor. Con él:
   - Se abre y cierra sin recargar (accesos de la cabecera, botón cerrar, Escape) y recuerda, POR TICKET, si estaba
     abierto y qué pestaña (`sessionStorage`, clave `orbita.panelTicket.<id>`): nunca mensajes ni permisos.
   - Conversación: carga los mensajes al abrirla (`tickets:conversacion`), se conecta por WebSocket
     (`/ws/tickets/<id>/conversacion/`), recibe los mensajes de otras personas al instante, reconecta con espera
     creciente y al reconectar recupera lo que faltó (`sincronizar` desde el último id). Envía por el WebSocket
     (o por HTTP si está caído); el autor lo pone el servidor. Cada mensaje se pinta con `textContent` (sin HTML)
     y se descarta si su id ya está (sin duplicados).
   - No leídos: el contador del acceso «Conversación» sale del servidor; se marca leído (regla explícita) cuando
     la pestaña Conversación está abierta y la página visible. */
(function () {
  "use strict";

  var panel = document.querySelector("[data-panel-ticket]");
  if (!panel) return;

  var pagina = document.querySelector("[data-espacio]");
  var ticket = panel.getAttribute("data-ticket");
  var yo = panel.getAttribute("data-usuario");
  var clave = "orbita.panelTicket." + ticket;
  var movil = window.matchMedia("(max-width: 639px)");
  var accesos = Array.prototype.slice.call(document.querySelectorAll("[data-tp-abrir]"));
  var pestanas = Array.prototype.slice.call(panel.querySelectorAll("[data-tp-pestana]"));
  var vistas = Array.prototype.slice.call(panel.querySelectorAll("[data-tp-vista]"));
  var pestanaActual = panel.getAttribute("data-pestana-inicial") || "conversacion";

  /* ---------------------------------------------------------------- estado visual */

  function leerEstado() {
    try {
      var valor = JSON.parse(window.sessionStorage.getItem(clave) || "null");
      return valor && typeof valor === "object" ? valor : null;
    } catch (e) {
      return null;
    }
  }

  function guardarEstado() {
    try {
      window.sessionStorage.setItem(clave, JSON.stringify({ abierto: !panel.hidden, pestana: pestanaActual }));
    } catch (e) {
      /* almacenamiento no disponible: el panel funciona igual */
    }
  }

  function limpiarParametro() {
    if (!window.URL || !window.history || !window.history.replaceState) return;
    var url = new URL(window.location.href);
    if (!url.searchParams.has("panel")) return;
    url.searchParams.delete("panel");
    window.history.replaceState(window.history.state, "", url.toString());
  }

  function marcarAccesos() {
    accesos.forEach(function (acceso) {
      var activo = !panel.hidden && acceso.getAttribute("data-tp-abrir") === pestanaActual;
      acceso.setAttribute("aria-expanded", activo ? "true" : "false");
    });
  }

  function elegirPestana(nombre) {
    // Sin pestaña Historial (quien no consulta el ticket) siempre queda la Conversación.
    var hayHistorial = !!panel.querySelector('[data-tp-vista="historial"]');
    pestanaActual = nombre === "historial" && hayHistorial ? "historial" : "conversacion";
    pestanas.forEach(function (tab) {
      var activa = tab.getAttribute("data-tp-pestana") === pestanaActual;
      tab.classList.toggle("is-active", activa);
      tab.setAttribute("aria-selected", activa ? "true" : "false");
    });
    vistas.forEach(function (vista) {
      vista.hidden = vista.getAttribute("data-tp-vista") !== pestanaActual;
    });
    marcarAccesos();
    if (pestanaActual === "conversacion") alMostrarConversacion();
  }

  function abrir(nombre, opciones) {
    panel.hidden = false;
    if (pagina) pagina.classList.add("has-tp");
    document.documentElement.classList.toggle("tp-bloqueo", movil.matches);
    elegirPestana(nombre || pestanaActual);
    guardarEstado();
    if (!(opciones && opciones.sinFoco)) {
      var tab = panel.querySelector('[data-tp-pestana="' + pestanaActual + '"]');
      if (tab) tab.focus({ preventScroll: true });
    }
  }

  function cerrar() {
    var anterior = accesos.filter(function (a) { return a.getAttribute("data-tp-abrir") === pestanaActual; })[0];
    panel.hidden = true;
    if (pagina) pagina.classList.remove("has-tp");
    document.documentElement.classList.remove("tp-bloqueo");
    marcarAccesos();
    guardarEstado();
    if (anterior) anterior.focus({ preventScroll: true });
  }

  accesos.forEach(function (acceso) {
    acceso.addEventListener("click", function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      limpiarParametro();
      var nombre = acceso.getAttribute("data-tp-abrir");
      if (!panel.hidden && pestanaActual === nombre) cerrar();
      else abrir(nombre);
    });
  });

  pestanas.forEach(function (tab) {
    tab.addEventListener("click", function (event) {
      event.preventDefault();
      limpiarParametro();
      elegirPestana(tab.getAttribute("data-tp-pestana"));
      guardarEstado();
    });
    tab.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      event.preventDefault();
      var otra = pestanas[(pestanas.indexOf(tab) + 1) % pestanas.length];
      otra.focus();
      elegirPestana(otra.getAttribute("data-tp-pestana"));
      guardarEstado();
    });
  });

  panel.querySelector("[data-tp-cerrar]").addEventListener("click", function (event) {
    event.preventDefault();
    limpiarParametro();
    cerrar();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !panel.hidden) cerrar();
  });

  if (movil.addEventListener) {
    movil.addEventListener("change", function () {
      if (!panel.hidden) document.documentElement.classList.toggle("tp-bloqueo", movil.matches);
    });
  }

  /* ---------------------------------------------------------------- conversación */

  var lista = panel.querySelector("[data-chat-lista]");
  var estado = panel.querySelector("[data-chat-estado]");
  var conexion = panel.querySelector("[data-chat-conexion]");
  var form = panel.querySelector("[data-chat-form]");
  var texto = panel.querySelector("[data-chat-texto]");
  var errorEnvio = panel.querySelector("[data-chat-error]");
  var contador = document.querySelector("[data-chat-no-leidos]");
  var contadorN = document.querySelector("[data-chat-no-leidos-n]");
  var ids = Object.create(null);
  var pendientes = Object.create(null);
  var ultimoId = 0;
  var cargado = false;
  var cargando = false;
  var socket = null;
  var espera = 1000;
  var sinAcceso = false;
  var formatoHora = new Intl.DateTimeFormat("es", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  function csrf() {
    var campo = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return campo ? campo.value : "";
  }

  function fijarNoLeidos(total) {
    if (!contador) return;
    var n = Math.max(0, parseInt(total, 10) || 0);
    contadorN.textContent = String(n);
    contador.hidden = n === 0;
  }

  function conversacionVisible() {
    return !panel.hidden && pestanaActual === "conversacion" && document.visibilityState === "visible";
  }

  function cercaDelFinal() {
    return lista.scrollHeight - lista.scrollTop - lista.clientHeight < 80;
  }

  function alFinal() {
    lista.scrollTop = lista.scrollHeight;
  }

  function mostrarEstado(mensaje, esError) {
    estado.textContent = mensaje || "";
    estado.classList.toggle("is-error", !!esError);
  }

  function avatar(mensaje) {
    var caja = document.createElement("span");
    caja.className = "avatar";
    caja.setAttribute("aria-hidden", "true");
    var inicial = document.createElement("span");
    inicial.textContent = mensaje.inicial || "?";
    caja.appendChild(inicial);
    if (mensaje.foto && /^https:\/\//.test(mensaje.foto)) {
      var img = document.createElement("img");
      img.src = mensaje.foto;
      img.alt = "";
      img.referrerPolicy = "no-referrer";
      img.loading = "lazy";
      caja.appendChild(img);
    }
    return caja;
  }

  function elemento(mensaje, aviso) {
    var li = document.createElement("article");
    li.className = "tp-msj" + (String(mensaje.autor_id) === yo ? " tp-msj--mio" : "");
    if (mensaje.id) li.setAttribute("data-id", mensaje.id);
    li.appendChild(avatar(mensaje));
    var cuerpo = document.createElement("div");
    var cabeza = document.createElement("p");
    cabeza.className = "tp-msj__cabeza";
    var autor = document.createElement("span");
    autor.className = "tp-msj__autor";
    autor.textContent = String(mensaje.autor_id) === yo ? "Tú" : mensaje.autor;
    cabeza.appendChild(autor);
    if (mensaje.creado_en) {
      var hora = document.createElement("time");
      hora.className = "tp-msj__hora tabular";
      hora.dateTime = mensaje.creado_en;
      hora.textContent = formatoHora.format(new Date(mensaje.creado_en));
      cabeza.appendChild(hora);
    }
    var cuerpoTexto = document.createElement("p");
    cuerpoTexto.className = "tp-msj__texto";
    cuerpoTexto.textContent = mensaje.texto;
    cuerpo.appendChild(cabeza);
    cuerpo.appendChild(cuerpoTexto);
    li.appendChild(cuerpo);
    if (aviso) {
      var nota = document.createElement("p");
      nota.className = "tp-msj__aviso";
      nota.textContent = aviso;
      li.appendChild(nota);
    }
    return li;
  }

  function insertar(nodo, id) {
    // Orden cronológico por id; los pendientes (sin id) van al final.
    if (id) {
      var siguientes = lista.querySelectorAll(".tp-msj[data-id]");
      for (var i = 0; i < siguientes.length; i += 1) {
        if (parseInt(siguientes[i].getAttribute("data-id"), 10) > id) {
          lista.insertBefore(nodo, siguientes[i]);
          return;
        }
      }
      var primeroPendiente = lista.querySelector(".tp-msj.is-pendiente");
      if (primeroPendiente) {
        lista.insertBefore(nodo, primeroPendiente);
        return;
      }
    }
    lista.appendChild(nodo);
  }

  function agregar(mensaje, clienteId) {
    var id = parseInt(mensaje.id, 10);
    if (clienteId && pendientes[clienteId]) {
      var pendiente = pendientes[clienteId];
      delete pendientes[clienteId];
      if (ids[id]) {
        pendiente.remove();
      } else {
        pendiente.replaceWith(elemento(mensaje));
        ids[id] = true;
      }
    } else if (ids[id]) {
      return false;
    } else {
      ids[id] = true;
      if (cargado) {
        var abajo = cercaDelFinal();
        insertar(elemento(mensaje), id);
        if (abajo || String(mensaje.autor_id) === yo) alFinal();
      }
    }
    if (id > ultimoId) ultimoId = id;
    mostrarEstado("");
    return true;
  }

  function vacio() {
    if (!lista.querySelector(".tp-msj")) mostrarEstado("Aún no hay mensajes. Escribe el primero para todo el equipo del ticket.");
  }

  function marcarLeido() {
    if (!conversacionVisible() || !ultimoId) return;
    if (socket && socket.readyState === 1) {
      socket.send(JSON.stringify({ tipo: "leido", hasta: ultimoId }));
      return;
    }
    var datos = new FormData();
    datos.append("hasta", String(ultimoId));
    fetch(panel.getAttribute("data-url-leido"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrf() },
      body: datos,
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (r) { if (r) fijarNoLeidos(r.no_leidos); })
      .catch(function () { /* se reintenta al volver a ver la conversación */ });
  }

  function cargar() {
    if (cargado || cargando) return;
    cargando = true;
    mostrarEstado("Cargando la conversación…");
    fetch(panel.getAttribute("data-url-mensajes"), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then(function (datos) {
        cargado = true;
        mostrarEstado("");
        datos.mensajes.forEach(function (m) {
          var id = parseInt(m.id, 10);
          if (ids[id]) return;
          ids[id] = true;
          if (id > ultimoId) ultimoId = id;
          insertar(elemento(m), id);
        });
        vacio();
        fijarNoLeidos(datos.no_leidos);
        alFinal();
        marcarLeido();
      })
      .catch(function () {
        mostrarEstado("No se pudo cargar la conversación. Vuelve a abrirla para reintentar.", true);
      })
      .then(function () { cargando = false; });
  }

  function alMostrarConversacion() {
    if (!cargado) cargar();
    else marcarLeido();
  }

  /* --- WebSocket --- */

  function avisoConexion(mensaje) {
    if (!conexion) return;
    conexion.textContent = mensaje || "";
    conexion.hidden = !mensaje;
  }

  function conectar() {
    if (sinAcceso || !window.WebSocket) return;
    var protocolo = window.location.protocol === "https:" ? "wss://" : "ws://";
    socket = new WebSocket(protocolo + window.location.host + panel.getAttribute("data-ws"));
    socket.addEventListener("open", function () {
      espera = 1000;
      avisoConexion("");
      // Recuperar lo que faltó mientras no había conexión (o solo el contador, si aún no se abrió el chat).
      socket.send(JSON.stringify(cargado ? { tipo: "sincronizar", despues: ultimoId } : { tipo: "estado" }));
    });
    socket.addEventListener("message", function (evento) {
      var datos;
      try { datos = JSON.parse(evento.data); } catch (e) { return; }
      if (datos.tipo === "mensaje") {
        var nuevo = agregar(datos.mensaje, datos.cliente_id);
        if (nuevo && String(datos.mensaje.autor_id) !== yo) {
          if (conversacionVisible() && cargado) marcarLeido();
          else if (contador) fijarNoLeidos((parseInt(contadorN.textContent, 10) || 0) + 1);
        }
      } else if (datos.tipo === "mensajes") {
        datos.mensajes.forEach(function (m) { agregar(m); });
        vacio();
        marcarLeido();
      } else if (datos.tipo === "no_leidos") {
        fijarNoLeidos(datos.total);
      } else if (datos.tipo === "error") {
        fallo(datos.cliente_id, datos.detalle);
      }
    });
    socket.addEventListener("close", function (evento) {
      socket = null;
      if (evento.code === 4403) {
        sinAcceso = true;
        avisoConexion("Ya no tienes acceso a esta conversación.");
        return;
      }
      avisoConexion("Sin conexión en tiempo real. Reconectando…");
      window.setTimeout(conectar, espera);
      espera = Math.min(espera * 2, 30000);
    });
  }

  /* --- Envío --- */

  function fallo(clienteId, detalle) {
    var pendiente = clienteId && pendientes[clienteId];
    if (pendiente) {
      pendiente.classList.remove("is-pendiente");
      pendiente.classList.add("is-fallido");
      var nota = pendiente.querySelector(".tp-msj__aviso");
      if (nota) nota.textContent = "No se envió.";
      delete pendientes[clienteId];
    }
    if (errorEnvio) {
      errorEnvio.textContent = detalle || "No se pudo enviar el mensaje.";
      errorEnvio.hidden = false;
    }
  }

  function ajustarAltura() {
    texto.style.height = "auto";
    texto.style.height = Math.min(texto.scrollHeight, 160) + "px";
  }

  if (form) {
    texto.addEventListener("input", ajustarAltura);
    texto.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", { cancelable: true }));
      }
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var valor = texto.value.trim();
      if (!valor) return;
      errorEnvio.hidden = true;
      var clienteId = "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      var pendiente = elemento({ autor_id: yo, autor: "Tú", inicial: "", texto: valor }, "Enviando…");
      pendiente.classList.add("is-pendiente");
      pendientes[clienteId] = pendiente;
      if (!cargado) mostrarEstado("");
      insertar(pendiente, null);
      alFinal();
      texto.value = "";
      ajustarAltura();
      if (socket && socket.readyState === 1) {
        socket.send(JSON.stringify({ tipo: "enviar", texto: valor, cliente_id: clienteId }));
        return;
      }
      var datos = new FormData();
      datos.append("texto", valor);
      fetch(panel.getAttribute("data-url-enviar"), {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrf(), Accept: "application/json" },
        body: datos,
      })
        .then(function (r) { return r.json().then(function (cuerpo) { return { ok: r.ok, cuerpo: cuerpo }; }); })
        .then(function (r) {
          if (r.ok) agregar(r.cuerpo.mensaje, clienteId);
          else fallo(clienteId, r.cuerpo.error);
        })
        .catch(function () { fallo(clienteId, "Sin conexión. El mensaje no se envió."); });
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (conversacionVisible()) alMostrarConversacion();
  });

  /* ---------------------------------------------------------------- inicio */

  if (!panel.hidden) {
    // El servidor lo abrió (`?panel=`): ese pedido explícito manda sobre lo recordado.
    elegirPestana(pestanaActual);
    if (pagina) pagina.classList.add("has-tp");
    guardarEstado();
  } else {
    var recordado = leerEstado();
    if (recordado && recordado.abierto) abrir(recordado.pestana, { sinFoco: true });
    else if (recordado && recordado.pestana) pestanaActual = recordado.pestana;
  }
  conectar();
})();
