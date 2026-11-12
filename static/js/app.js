/* Órbita — comportamiento del shell y componentes base (V0).
   Sin dependencias externas. Mejora progresiva: el servidor decide qué
   existe y qué está autorizado; este script solo alterna estado visual.

   Responsabilidades:
   1. Popovers (menú de usuario, panel "Más", dropdowns):
      `data-popover-toggle="<id>"` en el disparador. Cierra con clic fuera y
      con Escape (devolviendo el foco), y se recorre con flechas.
   2. Paneles inline: `data-action="toggle-panel"` + `data-target="<id>"`.
   3. Mensajes: `data-action="cerrar-mensaje"`; `window.Orbita.toast()` o
      `data-toast="tipo" data-toast-text="…"`.
   4. Modales (<dialog>): `data-modal-open="<id>"` / `data-modal-close`.
   5. Uploader: muestra el archivo elegido y resalta el arrastre. */

(function () {
  "use strict";

  /* ------------------------------ Presencia ------------------------------
     Mientras una pestaña de Órbita esté visible, confirma cada minuto que la persona sigue aquí
     (`core:presencia`): así sus compañeros la ven «en línea» en la Conversación del ticket y los mensajes
     no le llegan por correo. Una pestaña oculta no confirma nada. */
  (function () {
    var url = document.body && document.body.getAttribute("data-presencia");
    if (!url || !window.fetch) return;
    var csrf = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    function senal() {
      if (document.visibilityState !== "visible") return;
      fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrf ? decodeURIComponent(csrf[1]) : "" },
        keepalive: true,
      }).catch(function () { /* sin red: se reintenta en el siguiente minuto */ });
    }
    window.setInterval(senal, 60000);
    document.addEventListener("visibilitychange", senal);
  })();

  // Si Google no puede entregar la foto, conservar las iniciales del avatar.
  document.addEventListener("error", function (event) {
    if (event.target.matches && event.target.matches(".avatar img")) {
      event.target.remove();
    }
  }, true);

  /* ------------------------------ Popovers ------------------------------ */

  function panelDe(disparador) {
    return document.getElementById(disparador.getAttribute("data-popover-toggle"));
  }

  function disparadoresAbiertos() {
    return Array.prototype.slice.call(
      document.querySelectorAll('[data-popover-toggle][aria-expanded="true"]')
    );
  }

  function cerrarPopover(disparador) {
    var panel = panelDe(disparador);
    if (panel) panel.hidden = true;
    disparador.setAttribute("aria-expanded", "false");
  }

  function cerrarPopovers(excepto) {
    disparadoresAbiertos().forEach(function (d) {
      if (d !== excepto) cerrarPopover(d);
    });
  }

  function itemsDe(panel) {
    return Array.prototype.slice.call(panel.querySelectorAll('[role="menuitem"]:not([disabled])'));
  }

  function abrirPopover(disparador, enfocarPrimero) {
    var panel = panelDe(disparador);
    if (!panel) return;
    cerrarPopovers(disparador);
    panel.hidden = false;
    disparador.setAttribute("aria-expanded", "true");
    if (enfocarPrimero) {
      var items = itemsDe(panel);
      if (items.length) items[0].focus();
    }
  }

  /* --------------------------- Modales (dialog) -------------------------- */

  function abrirModal(dialogo) {
    if (!dialogo) return;
    if (typeof dialogo.showModal === "function") {
      if (!dialogo.open) dialogo.showModal();
    } else {
      dialogo.setAttribute("open", "");
    }
  }

  function cerrarModal(dialogo) {
    if (!dialogo) return;
    if (typeof dialogo.close === "function") {
      dialogo.close();
    } else {
      dialogo.removeAttribute("open");
    }
  }

  /* -------------------------------- Toast -------------------------------- */

  var ICONOS_TOAST = { success: "check-circle", danger: "x-circle", warning: "alert", info: "info" };

  function regionMensajes() {
    var region = document.querySelector(".messages");
    if (!region) {
      region = document.createElement("div");
      region.className = "messages";
      region.setAttribute("aria-live", "polite");
      document.body.appendChild(region);
    }
    return region;
  }

  /* Orbita.toast("Guardado", "success") — tipos: success | danger | warning | info.
     Se descarta solo a los 5 s (los mensajes de Django, en cambio, esperan al
     usuario). Construido con nodos de texto: nunca interpreta HTML. */
  function toast(texto, tipo) {
    tipo = ICONOS_TOAST[tipo] ? tipo : "info";
    var alerta = document.createElement("div");
    alerta.className = "alert alert--" + tipo;
    alerta.setAttribute("role", "status");

    var icono = document.createElement("span");
    icono.className = "alert__icon";
    icono.setAttribute("aria-hidden", "true");
    icono.innerHTML =
      '<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-' + ICONOS_TOAST[tipo] + '"/></svg>';

    var cuerpo = document.createElement("span");
    cuerpo.className = "alert__text";
    cuerpo.textContent = texto;

    var cerrar = document.createElement("button");
    cerrar.type = "button";
    cerrar.className = "alert__close";
    cerrar.setAttribute("data-action", "cerrar-mensaje");
    cerrar.setAttribute("aria-label", "Cerrar mensaje");
    cerrar.textContent = "×";

    alerta.appendChild(icono);
    alerta.appendChild(cuerpo);
    alerta.appendChild(cerrar);
    regionMensajes().appendChild(alerta);
    window.setTimeout(function () {
      if (alerta.parentNode) alerta.remove();
    }, 5000);
    return alerta;
  }

  window.Orbita = window.Orbita || {};
  window.Orbita.toast = toast;

  /* ------------------------------ Eventos -------------------------------- */

  document.addEventListener("click", function (event) {
    var cerrarMensaje = event.target.closest('[data-action="cerrar-mensaje"]');
    if (cerrarMensaje) {
      var alerta = cerrarMensaje.closest(".alert");
      if (alerta) alerta.remove();
      return;
    }

    var disparador = event.target.closest("[data-popover-toggle]");
    if (disparador) {
      if (disparador.getAttribute("aria-expanded") === "true") {
        cerrarPopover(disparador);
      } else {
        // detail === 0: activado con teclado (Enter/Espacio) → enfocar el 1.º ítem.
        abrirPopover(disparador, event.detail === 0);
      }
      return;
    }

    var togglePanel = event.target.closest('[data-action="toggle-panel"]');
    if (togglePanel) {
      var panel = document.getElementById(togglePanel.getAttribute("data-target"));
      if (panel) panel.hidden = !panel.hidden;
      return;
    }

    // Demostración/uso declarativo: data-toast="success|danger|warning|info"
    // + data-toast-text="Mensaje".
    var disparaToast = event.target.closest("[data-toast]");
    if (disparaToast) {
      toast(disparaToast.getAttribute("data-toast-text") || "", disparaToast.getAttribute("data-toast"));
      return;
    }

    var abrir = event.target.closest("[data-modal-open]");
    if (abrir) {
      abrirModal(document.getElementById(abrir.getAttribute("data-modal-open")));
      return;
    }

    var cerrar = event.target.closest("[data-modal-close]");
    if (cerrar) {
      cerrarModal(cerrar.closest("dialog"));
      return;
    }

    // Clic sobre el fondo del <dialog> (el propio elemento, no su contenido).
    if (event.target.tagName === "DIALOG" && event.target.classList.contains("modal")) {
      cerrarModal(event.target);
      return;
    }

    if (!event.target.closest(".popover-host")) {
      cerrarPopovers();
    }
  });

  document.addEventListener("keydown", function (event) {
    var abiertos = disparadoresAbiertos();

    if (event.key === "Escape" && abiertos.length) {
      var ultimo = abiertos[abiertos.length - 1];
      cerrarPopovers();
      ultimo.focus();
      return;
    }

    if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Home" && event.key !== "End") {
      return;
    }

    // Flecha abajo sobre un disparador cerrado lo abre (patrón de menú).
    var d = event.target.closest && event.target.closest("[data-popover-toggle]");
    if (d && d.getAttribute("aria-expanded") !== "true" && event.key === "ArrowDown") {
      event.preventDefault();
      abrirPopover(d, true);
      return;
    }

    var panelAbierto = event.target.closest && event.target.closest(".popover");
    if (!panelAbierto || panelAbierto.hidden) return;
    var items = itemsDe(panelAbierto);
    if (!items.length) return;
    event.preventDefault();
    var indice = items.indexOf(document.activeElement);
    if (event.key === "Home") indice = 0;
    else if (event.key === "End") indice = items.length - 1;
    else if (event.key === "ArrowDown") indice = (indice + 1) % items.length;
    else indice = (indice - 1 + items.length) % items.length;
    items[indice].focus();
  });

  /* ------------------- «Publicar a los interesados» = aprobar ------------------- */
  /* Con la casilla marcada solo queda «Aprobar»: las demás decisiones del formulario se deshabilitan (el servidor
     también rechaza combinarlas). Solo se reactivan las que deshabilitó esta casilla (no las que bloquea un dibujo). */

  document.addEventListener("change", function (event) {
    var casilla = event.target;
    if (!casilla.matches || !casilla.matches("input[data-publicar]")) return;
    var form = casilla.closest("form");
    if (!form) return;
    form.querySelectorAll('button[name="decision"]').forEach(function (boton) {
      if (boton.value === "APROBADA") return;
      if (casilla.checked) {
        if (!boton.disabled) {
          boton.disabled = true;
          boton.setAttribute("data-bloqueado-por-publicar", "");
          boton.title = "Publicar es aprobar: quita la marca para elegir otra decisión.";
        }
      } else if (boton.hasAttribute("data-bloqueado-por-publicar")) {
        boton.disabled = false;
        boton.removeAttribute("data-bloqueado-por-publicar");
        boton.removeAttribute("title");
      }
    });
  });

  /* ------------------------------ Uploader ------------------------------- */

  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches(".uploader input[type=file]")) return;
    var nombre = input.closest(".uploader").querySelector(".uploader__name");
    if (!nombre) return;
    var archivos = Array.prototype.map.call(input.files || [], function (f) {
      return f.name;
    });
    nombre.textContent = archivos.join(", ");
  });

  ["dragenter", "dragover"].forEach(function (tipo) {
    document.addEventListener(tipo, function (event) {
      var zona = event.target.closest && event.target.closest(".uploader");
      if (!zona) return;
      event.preventDefault();
      zona.classList.add("is-dragover");
    });
  });

  ["dragleave", "drop"].forEach(function (tipo) {
    document.addEventListener(tipo, function (event) {
      var zona = event.target.closest && event.target.closest(".uploader");
      if (!zona) return;
      zona.classList.remove("is-dragover");
      if (tipo === "drop") {
        event.preventDefault();
        var input = zona.querySelector("input[type=file]");
        if (input && event.dataTransfer && event.dataTransfer.files.length) {
          input.files = event.dataTransfer.files;
          input.dispatchEvent(new Event("change", { bubbles: true }));
        }
      }
    });
  });
})();
