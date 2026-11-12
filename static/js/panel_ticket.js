/* Panel contextual del ticket (Conversación | Historial) y chat en tiempo real — mejora progresiva.

   Sin este script las pestañas y accesos son enlaces (`?panel=conversacion|historial`) y el panel llega abierto
   desde el servidor. Con él:
   - Se abre y cierra sin recargar (accesos de la cabecera, botón cerrar, Escape) y recuerda, POR TICKET, si estaba
     abierto y qué pestaña (`sessionStorage`, clave `orbita.panelTicket.<id>`): nunca mensajes ni permisos.
   - Conversación en DOS canales aislados (Equipo / Interesados): la persona ve solo los suyos y cambia entre ellos
     con el selector; cada canal tiene sus mensajes, su contador y su marca de lectura. Carga los mensajes del canal
     al mostrarlo (`tickets:conversacion?canal=`), se conecta por WebSocket
     (`/ws/tickets/<id>/conversacion/`), recibe los mensajes de otras personas al instante, reconecta con espera
     creciente y al reconectar recupera lo que faltó (`sincronizar` desde el último id). Envía por el WebSocket
     (o por HTTP si está caído); el autor lo pone el servidor. Cada mensaje se pinta con `textContent` (sin HTML)
     y se descarta si su id ya está (sin duplicados).
   - No leídos: el contador del acceso «Conversación» sale del servidor; se marca leído (regla explícita) cuando
     la pestaña Conversación está abierta y la página visible.
   - Archivos y emojis: el clip elige hasta N archivos (se ven como fichas que se pueden quitar) y un mensaje con
     archivos viaja por HTTP (`tickets:conversacion_enviar`, multipart); el servidor lo difunde a los demás. Los
     emojis son texto: el selector solo los inserta donde está el cursor.
   - Presencia: con la conversación a la vista se pide cada minuto quién del canal está en línea
     (`tickets:conversacion_presencia`) y se pinta con un punto en cada persona. */
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
  var canalActual = panel.getAttribute("data-canal-inicial") || "";

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
      window.sessionStorage.setItem(clave, JSON.stringify({ abierto: !panel.hidden, pestana: pestanaActual, canal: canalActual }));
    } catch (e) {
      /* almacenamiento no disponible: el panel funciona igual */
    }
  }

  function limpiarParametro() {
    if (!window.URL || !window.history || !window.history.replaceState) return;
    var url = new URL(window.location.href);
    if (!url.searchParams.has("panel") && !url.searchParams.has("canal")) return;
    url.searchParams.delete("panel");
    url.searchParams.delete("canal");
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

  var conexion = panel.querySelector("[data-chat-conexion]");
  var form = panel.querySelector("[data-chat-form]");
  var texto = panel.querySelector("[data-chat-texto]");
  var campoCanal = panel.querySelector("[data-chat-canal-campo]");
  var errorEnvio = panel.querySelector("[data-chat-error]");
  var contador = document.querySelector("[data-chat-no-leidos]");
  var contadorN = document.querySelector("[data-chat-no-leidos-n]");
  var socket = null;
  var espera = 1000;
  var sinAcceso = false;
  var formatoHora = new Intl.DateTimeFormat("es", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  // Estado POR CANAL: su lista, mensajes vistos, último id, carga y no leídos.
  var canales = {};
  Array.prototype.slice.call(panel.querySelectorAll("[data-chat-lista]")).forEach(function (lista) {
    var nombre = lista.getAttribute("data-chat-lista");
    canales[nombre] = {
      lista: lista,
      estado: lista.querySelector("[data-chat-estado]"),
      ayuda: panel.querySelector('[data-chat-ayuda="' + nombre + '"]'),
      boton: panel.querySelector('[data-chat-canal="' + nombre + '"]'),
      cuenta: panel.querySelector('[data-chat-canal-cuenta="' + nombre + '"]'),
      ids: Object.create(null),
      pendientes: Object.create(null),
      ultimoId: 0,
      cargado: false,
      cargando: false,
      noLeidos: 0,
    };
    var inicial = canales[nombre].cuenta ? parseInt(canales[nombre].cuenta.textContent, 10) : 0;
    canales[nombre].noLeidos = inicial || 0;
  });
  var nombresCanales = Object.keys(canales);
  if (!canales[canalActual]) canalActual = nombresCanales[0] || "";
  if (nombresCanales.length === 1 && contador) {
    canales[canalActual].noLeidos = parseInt(contadorN.textContent, 10) || 0;
  }

  function csrf() {
    var campo = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return campo ? campo.value : "";
  }

  function pintarContadores() {
    var total = 0;
    nombresCanales.forEach(function (nombre) {
      var c = canales[nombre];
      total += c.noLeidos;
      if (c.cuenta) {
        c.cuenta.textContent = String(c.noLeidos);
        c.cuenta.hidden = c.noLeidos === 0;
      }
    });
    if (contador) {
      contadorN.textContent = String(total);
      contador.hidden = total === 0;
    }
  }

  function fijarNoLeidos(nombre, total) {
    if (!canales[nombre]) return;
    canales[nombre].noLeidos = Math.max(0, parseInt(total, 10) || 0);
    pintarContadores();
  }

  function visible(nombre) {
    return !panel.hidden && pestanaActual === "conversacion" && canalActual === nombre && document.visibilityState === "visible";
  }

  function cercaDelFinal(lista) {
    return lista.scrollHeight - lista.scrollTop - lista.clientHeight < 80;
  }

  function mostrarEstado(c, mensaje, esError) {
    c.estado.textContent = mensaje || "";
    c.estado.classList.toggle("is-error", !!esError);
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
    var propio = String(mensaje.autor_id) === yo;
    var li = document.createElement("article");
    li.className = "tp-msj" + (propio ? " tp-msj--mio" : "");
    if (mensaje.id) li.setAttribute("data-id", mensaje.id);
    li.appendChild(avatar(mensaje));
    var cuerpo = document.createElement("div");
    var cabeza = document.createElement("p");
    cabeza.className = "tp-msj__cabeza";
    var autor = document.createElement("span");
    autor.className = "tp-msj__autor";
    autor.textContent = propio ? "Tú" : mensaje.autor;
    cabeza.appendChild(autor);
    if (mensaje.creado_en) {
      var hora = document.createElement("time");
      hora.className = "tp-msj__hora tabular";
      hora.dateTime = mensaje.creado_en;
      hora.textContent = formatoHora.format(new Date(mensaje.creado_en));
      cabeza.appendChild(hora);
    }
    cuerpo.appendChild(cabeza);
    if (mensaje.texto) {
      var cuerpoTexto = document.createElement("p");
      cuerpoTexto.className = "tp-msj__texto";
      cuerpoTexto.textContent = mensaje.texto;
      cuerpo.appendChild(cuerpoTexto);
    }
    if (mensaje.archivos && mensaje.archivos.length) cuerpo.appendChild(archivosDe(mensaje.archivos));
    li.appendChild(cuerpo);
    if (aviso) {
      var nota = document.createElement("p");
      nota.className = "tp-msj__aviso";
      nota.textContent = aviso;
      li.appendChild(nota);
    }
    return li;
  }

  function tamano(bytes) {
    if (!bytes && bytes !== 0) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1).replace(".0", "") + " MB";
  }

  // Archivos de un mensaje: las imágenes con su miniatura, lo demás como enlace con nombre y tamaño. Las URL son
  // las de la descarga autorizada del servidor (relativas, nunca datos del mensaje interpretados como HTML).
  function archivosDe(archivos) {
    var lista = document.createElement("ul");
    lista.className = "tp-msj__archivos";
    archivos.forEach(function (a) {
      if (!a || typeof a.ver !== "string" || a.ver.charAt(0) !== "/") return;
      var item = document.createElement("li");
      var enlace = document.createElement("a");
      enlace.href = a.ver;
      enlace.target = "_blank";
      enlace.rel = "noopener";
      if (a.imagen) {
        enlace.className = "tp-msj__imagen";
        var img = document.createElement("img");
        img.src = a.ver;
        img.alt = a.nombre || "";
        img.loading = "lazy";
        enlace.appendChild(img);
      } else {
        enlace.className = "tp-msj__archivo";
        var nombre = document.createElement("span");
        nombre.className = "tp-msj__archivo-nombre";
        nombre.textContent = a.nombre || "Archivo";
        var peso = document.createElement("span");
        peso.className = "tp-msj__archivo-peso tabular";
        peso.textContent = tamano(a.tamano);
        enlace.appendChild(nombre);
        enlace.appendChild(peso);
      }
      item.appendChild(enlace);
      lista.appendChild(item);
    });
    return lista;
  }

  function insertar(c, nodo, id) {
    // Orden cronológico por id; los pendientes (sin id) van al final.
    if (id) {
      var siguientes = c.lista.querySelectorAll(".tp-msj[data-id]");
      for (var i = 0; i < siguientes.length; i += 1) {
        if (parseInt(siguientes[i].getAttribute("data-id"), 10) > id) {
          c.lista.insertBefore(nodo, siguientes[i]);
          return;
        }
      }
      var primeroPendiente = c.lista.querySelector(".tp-msj.is-pendiente");
      if (primeroPendiente) {
        c.lista.insertBefore(nodo, primeroPendiente);
        return;
      }
    }
    c.lista.appendChild(nodo);
  }

  // Devuelve true si el mensaje es nuevo para este navegador.
  function agregar(mensaje, clienteId) {
    var c = canales[mensaje.canal];
    if (!c) return false;
    var id = parseInt(mensaje.id, 10);
    var nuevo = false;
    if (clienteId && c.pendientes[clienteId]) {
      var pendiente = c.pendientes[clienteId];
      delete c.pendientes[clienteId];
      if (c.ids[id]) pendiente.remove();
      else {
        pendiente.replaceWith(elemento(mensaje));
        c.ids[id] = true;
      }
    } else if (c.ids[id]) {
      return false;
    } else {
      c.ids[id] = true;
      nuevo = true;
      if (c.cargado) {
        var abajo = cercaDelFinal(c.lista);
        insertar(c, elemento(mensaje), id);
        if (abajo || String(mensaje.autor_id) === yo) c.lista.scrollTop = c.lista.scrollHeight;
      }
    }
    if (id > c.ultimoId) c.ultimoId = id;
    mostrarEstado(c, "");
    return nuevo;
  }

  function vacio(c, nombre) {
    if (c.lista.querySelector(".tp-msj")) return;
    mostrarEstado(c, nombre === "EQUIPO" ? "Aún no hay mensajes del equipo." : "Aún no hay mensajes. Escribe el primero.");
  }

  function marcarLeido(nombre) {
    var c = canales[nombre];
    if (!c || !visible(nombre) || !c.ultimoId) return;
    if (socket && socket.readyState === 1) {
      socket.send(JSON.stringify({ tipo: "leido", canal: nombre, hasta: c.ultimoId }));
      return;
    }
    var datos = new FormData();
    datos.append("canal", nombre);
    datos.append("hasta", String(c.ultimoId));
    fetch(panel.getAttribute("data-url-leido"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrf() },
      body: datos,
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (r) { if (r) fijarNoLeidos(nombre, r.no_leidos); })
      .catch(function () { /* se reintenta al volver a ver la conversación */ });
  }

  function cargar(nombre) {
    var c = canales[nombre];
    if (!c || c.cargado || c.cargando) return;
    c.cargando = true;
    mostrarEstado(c, "Cargando la conversación…");
    var url = panel.getAttribute("data-url-mensajes") + "?canal=" + encodeURIComponent(nombre);
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then(function (datos) {
        c.cargado = true;
        mostrarEstado(c, "");
        datos.mensajes.forEach(function (m) {
          var id = parseInt(m.id, 10);
          if (c.ids[id]) return;
          c.ids[id] = true;
          if (id > c.ultimoId) c.ultimoId = id;
          insertar(c, elemento(m), id);
        });
        vacio(c, nombre);
        fijarNoLeidos(nombre, datos.no_leidos);
        c.lista.scrollTop = c.lista.scrollHeight;
        marcarLeido(nombre);
      })
      .catch(function () {
        mostrarEstado(c, "No se pudo cargar la conversación. Vuelve a abrirla para reintentar.", true);
      })
      .then(function () { c.cargando = false; });
  }

  function elegirCanal(nombre) {
    if (!canales[nombre]) return;
    canalActual = nombre;
    nombresCanales.forEach(function (n) {
      var c = canales[n];
      var activo = n === nombre;
      c.lista.hidden = !activo;
      if (c.ayuda) c.ayuda.hidden = !activo;
      if (c.boton) {
        c.boton.classList.toggle("is-activo", activo);
        c.boton.setAttribute("aria-selected", activo ? "true" : "false");
      }
    });
    if (campoCanal) campoCanal.value = nombre;
    if (texto) texto.placeholder = nombre === "EQUIPO" ? "Escribe al equipo…" : "Escribe a los interesados…";
    guardarEstado();
    presenciaEn = 0;
    alMostrarConversacion();
  }

  nombresCanales.forEach(function (nombre) {
    var boton = canales[nombre].boton;
    if (boton) boton.addEventListener("click", function () { elegirCanal(nombre); });
  });

  function alMostrarConversacion() {
    var c = canales[canalActual];
    if (!c) return;
    if (!c.cargado) cargar(canalActual);
    else marcarLeido(canalActual);
    pedirPresencia(canalActual);
  }

  /* --- Presencia: quién del canal tiene Órbita abierta ahora --- */

  var formatoVisto = new Intl.RelativeTimeFormat ? new Intl.RelativeTimeFormat("es", { numeric: "auto" }) : null;

  function haceCuanto(iso) {
    if (!iso || !formatoVisto) return "";
    var minutos = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
    if (minutos > -60) return formatoVisto.format(minutos, "minute");
    var horas = Math.round(minutos / 60);
    if (horas > -24) return formatoVisto.format(horas, "hour");
    return formatoVisto.format(Math.round(horas / 24), "day");
  }

  function pintarPresencia(nombre, datos) {
    var caja = panel.querySelector('[data-chat-presencia="' + nombre + '"]');
    if (!caja) return;
    var personas = datos.personas || [];
    caja.hidden = personas.length === 0;
    if (!personas.length) return;
    var caras = caja.querySelector("[data-presencia-caras]");
    var lista = caja.querySelector("[data-presencia-lista]");
    caras.textContent = "";
    lista.textContent = "";
    personas.slice(0, 5).forEach(function (p) {
      var cara = avatar(p);
      cara.classList.add("tp-presencia__cara");
      if (p.en_linea) cara.classList.add("is-en-linea");
      caras.appendChild(cara);
    });
    personas.forEach(function (p) {
      var li = document.createElement("li");
      li.className = "tp-presencia__persona" + (p.en_linea ? " is-en-linea" : "");
      var punto = document.createElement("span");
      punto.className = "tp-presencia__punto";
      punto.setAttribute("aria-hidden", "true");
      var quien = document.createElement("span");
      quien.className = "tp-presencia__nombre";
      quien.textContent = p.nombre;
      var estado = document.createElement("span");
      estado.className = "tp-presencia__estado";
      estado.textContent = p.en_linea ? "En línea" : (p.visto_en ? "Visto " + haceCuanto(p.visto_en) : "Sin conexión reciente");
      li.appendChild(punto);
      li.appendChild(quien);
      li.appendChild(estado);
      lista.appendChild(li);
    });
    var enLinea = datos.en_linea || 0;
    caja.querySelector("[data-presencia-texto]").textContent =
      (enLinea ? enLinea + " en línea" : "Nadie más en línea") + " · " + personas.length + (personas.length === 1 ? " persona" : " personas");
  }

  var presenciaEn = 0;
  function pedirPresencia(nombre, forzar) {
    if (!nombre || panel.hidden || pestanaActual !== "conversacion" || document.visibilityState !== "visible") return;
    if (!forzar && Date.now() - presenciaEn < 20000) return;
    presenciaEn = Date.now();
    fetch(panel.getAttribute("data-url-presencia") + "?canal=" + encodeURIComponent(nombre), {
      credentials: "same-origin", headers: { Accept: "application/json" },
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (datos) { if (datos) pintarPresencia(nombre, datos); })
      .catch(function () { /* se reintenta en el siguiente minuto */ });
  }

  window.setInterval(function () { pedirPresencia(canalActual, true); }, 60000);

  /* --- WebSocket --- */

  function avisoConexion(mensaje) {
    if (!conexion) return;
    conexion.textContent = mensaje || "";
    conexion.hidden = !mensaje;
  }

  function conectar() {
    if (sinAcceso || !window.WebSocket || !nombresCanales.length) return;
    var protocolo = window.location.protocol === "https:" ? "wss://" : "ws://";
    socket = new WebSocket(protocolo + window.location.host + panel.getAttribute("data-ws"));
    socket.addEventListener("open", function () {
      espera = 1000;
      avisoConexion("");
      // Recuperar lo que faltó en cada canal ya cargado y, de todos, sus contadores.
      nombresCanales.forEach(function (nombre) {
        if (canales[nombre].cargado) socket.send(JSON.stringify({ tipo: "sincronizar", canal: nombre, despues: canales[nombre].ultimoId }));
      });
      socket.send(JSON.stringify({ tipo: "estado" }));
    });
    socket.addEventListener("message", function (evento) {
      var datos;
      try { datos = JSON.parse(evento.data); } catch (e) { return; }
      if (datos.tipo === "mensaje") {
        var canal = datos.mensaje.canal;
        var nuevo = agregar(datos.mensaje, datos.cliente_id);
        if (nuevo && String(datos.mensaje.autor_id) !== yo && canales[canal]) {
          if (visible(canal) && canales[canal].cargado) marcarLeido(canal);
          else fijarNoLeidos(canal, canales[canal].noLeidos + 1);
        }
      } else if (datos.tipo === "mensajes" && canales[datos.canal]) {
        datos.mensajes.forEach(function (m) { agregar(m); });
        vacio(canales[datos.canal], datos.canal);
        marcarLeido(datos.canal);
      } else if (datos.tipo === "no_leidos") {
        fijarNoLeidos(datos.canal, datos.total);
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
    nombresCanales.forEach(function (nombre) {
      var c = canales[nombre];
      var pendiente = clienteId && c.pendientes[clienteId];
      if (!pendiente) return;
      pendiente.classList.remove("is-pendiente");
      pendiente.classList.add("is-fallido");
      var nota = pendiente.querySelector(".tp-msj__aviso");
      if (nota) nota.textContent = "No se envió.";
      delete c.pendientes[clienteId];
    });
    if (errorEnvio) {
      errorEnvio.textContent = detalle || "No se pudo enviar el mensaje.";
      errorEnvio.hidden = false;
    }
  }

  function ajustarAltura() {
    texto.style.height = "auto";
    texto.style.height = Math.min(texto.scrollHeight, 160) + "px";
  }

  /* --- Archivos elegidos y emojis --- */

  var elegidos = [];
  var campoArchivos = panel.querySelector("[data-chat-archivos]");
  var listaElegidos = panel.querySelector("[data-chat-adjuntos]");
  var maxArchivos = form ? parseInt(form.getAttribute("data-max-archivos"), 10) || 5 : 5;
  var maxMb = form ? parseInt(form.getAttribute("data-tamano-maximo"), 10) || 25 : 25;

  function avisoError(detalle) {
    if (!errorEnvio) return;
    errorEnvio.textContent = detalle;
    errorEnvio.hidden = !detalle;
  }

  function pintarElegidos() {
    if (!listaElegidos) return;
    listaElegidos.textContent = "";
    listaElegidos.hidden = elegidos.length === 0;
    elegidos.forEach(function (archivo, indice) {
      var li = document.createElement("li");
      li.className = "tp-chat__adjunto";
      var nombre = document.createElement("span");
      nombre.className = "tp-chat__adjunto-nombre";
      nombre.textContent = archivo.name;
      var peso = document.createElement("span");
      peso.className = "tabular";
      peso.textContent = tamano(archivo.size);
      var quitar = document.createElement("button");
      quitar.type = "button";
      quitar.className = "tp-chat__adjunto-quitar";
      quitar.setAttribute("aria-label", "Quitar " + archivo.name);
      quitar.textContent = "×";
      quitar.addEventListener("click", function () {
        elegidos.splice(indice, 1);
        pintarElegidos();
        texto.focus();
      });
      li.appendChild(nombre);
      li.appendChild(peso);
      li.appendChild(quitar);
      listaElegidos.appendChild(li);
    });
  }

  function limpiarElegidos() {
    elegidos = [];
    if (campoArchivos) campoArchivos.value = "";
    pintarElegidos();
  }

  if (campoArchivos) {
    campoArchivos.addEventListener("change", function () {
      var problema = "";
      Array.prototype.slice.call(campoArchivos.files || []).forEach(function (archivo) {
        if (elegidos.length >= maxArchivos) { problema = "Puedes enviar hasta " + maxArchivos + " archivos por mensaje."; return; }
        if (archivo.size === 0) { problema = "«" + archivo.name + "» está vacío."; return; }
        if (archivo.size > maxMb * 1024 * 1024) { problema = "«" + archivo.name + "» supera los " + maxMb + " MB."; return; }
        elegidos.push(archivo);
      });
      campoArchivos.value = "";
      avisoError(problema);
      pintarElegidos();
      if (texto) texto.focus();
    });
  }

  var EMOJIS = ["😀", "😂", "😊", "😍", "🤔", "😅", "😎", "🙌", "👏", "👍", "👎", "🙏", "💪", "👀", "🎉", "🚀",
    "✅", "❌", "⚠️", "❓", "💡", "📌", "📎", "📅", "⏰", "🔥", "❤️", "⭐", "👌", "✍️", "📝", "📣"];
  var botonEmojis = panel.querySelector("[data-chat-emoji-abrir]");
  var cajaEmojis = panel.querySelector("[data-chat-emojis]");

  function cerrarEmojis() {
    if (!cajaEmojis || cajaEmojis.hidden) return;
    cajaEmojis.hidden = true;
    botonEmojis.setAttribute("aria-expanded", "false");
  }

  function insertarEnTexto(valor) {
    var inicio = texto.selectionStart != null ? texto.selectionStart : texto.value.length;
    var fin = texto.selectionEnd != null ? texto.selectionEnd : texto.value.length;
    texto.value = texto.value.slice(0, inicio) + valor + texto.value.slice(fin);
    var cursor = inicio + valor.length;
    texto.focus();
    texto.setSelectionRange(cursor, cursor);
    ajustarAltura();
  }

  if (botonEmojis && cajaEmojis) {
    EMOJIS.forEach(function (emoji) {
      var boton = document.createElement("button");
      boton.type = "button";
      boton.className = "tp-emojis__emoji";
      boton.textContent = emoji;
      boton.setAttribute("aria-label", "Insertar " + emoji);
      boton.addEventListener("click", function () {
        insertarEnTexto(emoji);
        cerrarEmojis();
      });
      cajaEmojis.appendChild(boton);
    });
    botonEmojis.addEventListener("click", function () {
      var abrirla = cajaEmojis.hidden;
      cajaEmojis.hidden = !abrirla;
      botonEmojis.setAttribute("aria-expanded", abrirla ? "true" : "false");
      if (abrirla) {
        var primero = cajaEmojis.querySelector("button");
        if (primero) primero.focus();
      }
    });
    cajaEmojis.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        cerrarEmojis();
        botonEmojis.focus();
      }
    });
    document.addEventListener("click", function (event) {
      if (!cajaEmojis.hidden && !cajaEmojis.contains(event.target) && !botonEmojis.contains(event.target)) cerrarEmojis();
    });
  }

  if (form) {
    texto.addEventListener("input", ajustarAltura);
    texto.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (form.requestSubmit) form.requestSubmit();
        else form.dispatchEvent(new Event("submit", { cancelable: true }));
      }
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var valor = texto.value.trim();
      var canal = canalActual;
      var c = canales[canal];
      var adjuntos = elegidos.slice();
      if ((!valor && !adjuntos.length) || !c) return;
      errorEnvio.hidden = true;
      var clienteId = "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      var aviso = adjuntos.length ? "Enviando " + adjuntos.length + (adjuntos.length === 1 ? " archivo…" : " archivos…") : "Enviando…";
      var pendiente = elemento({ autor_id: yo, autor: "Tú", inicial: "", texto: valor }, aviso);
      pendiente.classList.add("is-pendiente");
      c.pendientes[clienteId] = pendiente;
      mostrarEstado(c, "");
      insertar(c, pendiente, null);
      c.lista.scrollTop = c.lista.scrollHeight;
      texto.value = "";
      ajustarAltura();
      limpiarElegidos();
      cerrarEmojis();
      // Con archivos el mensaje va por HTTP (el WebSocket solo lleva texto); el servidor lo difunde a los demás.
      if (!adjuntos.length && socket && socket.readyState === 1) {
        socket.send(JSON.stringify({ tipo: "enviar", canal: canal, texto: valor, cliente_id: clienteId }));
        return;
      }
      var datos = new FormData();
      datos.append("canal", canal);
      datos.append("texto", valor);
      adjuntos.forEach(function (archivo) { datos.append("archivos", archivo, archivo.name); });
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
    if (pestanaActual === "conversacion" && !panel.hidden && document.visibilityState === "visible") alMostrarConversacion();
  });

  /* ---------------------------------------------------------------- inicio */

  var recordado = leerEstado();
  var pedidoCanal = window.URL ? new URL(window.location.href).searchParams.get("canal") : null;
  if (!pedidoCanal && recordado && recordado.canal && canales[recordado.canal]) canalActual = recordado.canal;
  if (canales[canalActual]) elegirCanalSinCargar(canalActual);

  function elegirCanalSinCargar(nombre) {
    nombresCanales.forEach(function (n) {
      var c = canales[n];
      var activo = n === nombre;
      c.lista.hidden = !activo;
      if (c.ayuda) c.ayuda.hidden = !activo;
      if (c.boton) {
        c.boton.classList.toggle("is-activo", activo);
        c.boton.setAttribute("aria-selected", activo ? "true" : "false");
      }
    });
    if (campoCanal) campoCanal.value = nombre;
    if (texto) texto.placeholder = nombre === "EQUIPO" ? "Escribe al equipo…" : "Escribe a los interesados…";
  }

  if (!panel.hidden) {
    // El servidor lo abrió (`?panel=`): ese pedido explícito manda sobre lo recordado.
    elegirPestana(pestanaActual);
    if (pagina) pagina.classList.add("has-tp");
    guardarEstado();
  } else if (recordado && recordado.abierto) {
    abrir(recordado.pestana, { sinFoco: true });
  } else if (recordado && recordado.pestana) {
    pestanaActual = recordado.pestana;
  }
  conectar();
})();
