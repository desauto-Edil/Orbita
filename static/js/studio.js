/* Studio — comportamientos locales de los formularios de configuración (4.F1/4.F2).

   Mejora progresiva: sin JavaScript todo sigue siendo un formulario válido y el servidor es
   siempre quien valida. Aquí solo se muestra u oculta lo que no aplica.

   1. Opciones que dependen del tipo de un campo: `data-solo-tipos="FECHA,FECHA_HORA"` en un
      contenedor se muestra solo cuando el selector `…-tipo` de su formulario tiene uno de esos
      valores (p. ej. «Usar como fecha requerida»).
   2. Aprobación: `data-aprobacion` agrupa «¿Cuántos aprobadores participan?»
      (`data-aprobacion-cantidad`), una fila por aprobador (`data-aprobacion-fila`) y el
      «Modo de aprobación» (`data-aprobacion-modo`), que solo se pide con más de uno.
   3. Tipo e inicio (4.G1): en el formulario `data-config-inicio` (crear y Básico), el selector
      `tipo` decide qué se ve. `[data-solo-proceso]` (pregunta «¿Cómo inicia?») solo con Proceso;
      `[data-inicio-programado]` (calendario y responsable) solo con Proceso + «Programado»; y
      `[data-oculto-si-programado]` (lo orientado a quien solicita: visibilidad, instrucciones,
      búsqueda) se oculta con Proceso + «Programado». Lo oculto de Proceso se deshabilita para que
      no se envíe; lo demás solo se oculta, así conserva su valor.
   4. Fase 4.A — controles dependientes genéricos: `data-mostrar-si="sufijo=VALOR1,VALOR2"` se muestra solo
      cuando el control de SU formulario cuyo `name` termina en `sufijo` (selector o radio) tiene uno de esos
      valores (p. ej. «Persona» solo con «Una persona», parámetros de archivo solo con «Archivo»). Solo oculta: el
      valor se conserva y el servidor descarta lo que no aplica.
   5. Fase 4.A — cambios sin guardar: si se escribió en un formulario de la sección y se intenta salir por un
      enlace (navegación del Studio, Atrás/Continuar/Omitir) o cerrando la página, se pide confirmación. No hay
      autoguardado: guardar es siempre el botón del formulario.
   6. Fase 4.B — estado de guardado real, botón «Guardar» del pie, confirmaciones (`data-confirmar`), navegación
      secundaria (`data-subnav-ambito`), asistente por pasos (`form[data-pasos]`) y selección múltiple accesible
      (`select[multiple][data-multiselect]`). */

(function () {
  "use strict";

  /* ----------------------- Opciones que dependen del tipo ----------------------- */

  function aplicarTipo(contenedor, selector) {
    var tipos = contenedor.getAttribute("data-solo-tipos").split(",");
    contenedor.hidden = tipos.indexOf(selector.value) === -1;
  }

  function iniciarSoloTipos() {
    document.querySelectorAll("[data-solo-tipos]").forEach(function (contenedor) {
      var formulario = contenedor.closest("form");
      var selector = formulario && formulario.querySelector('select[name$="-tipo"]');
      if (!selector) return;
      aplicarTipo(contenedor, selector);
      selector.addEventListener("change", function () {
        aplicarTipo(contenedor, selector);
      });
    });
  }

  /* ------------------------------- Aprobación ------------------------------- */

  function aplicarCantidad(grupo, campo) {
    var cantidad = parseInt(campo.value, 10);
    if (isNaN(cantidad) || cantidad < 1) cantidad = 1;
    grupo.querySelectorAll("[data-aprobacion-fila]").forEach(function (fila, indice) {
      var visible = indice < cantidad;
      fila.hidden = !visible;
      fila.querySelectorAll("input, select").forEach(function (control) {
        control.disabled = !visible;
      });
    });
    grupo.querySelectorAll("[data-aprobacion-modo]").forEach(function (modo) {
      var visible = cantidad > 1;
      modo.hidden = !visible;
      modo.querySelectorAll("input, select").forEach(function (control) {
        control.disabled = !visible;
      });
    });
  }

  function iniciarAprobacion() {
    document.querySelectorAll("[data-aprobacion]").forEach(function (grupo) {
      var campo = grupo.querySelector("[data-aprobacion-cantidad]");
      if (!campo) return;
      aplicarCantidad(grupo, campo);
      campo.addEventListener("input", function () {
        aplicarCantidad(grupo, campo);
      });
      campo.addEventListener("change", function () {
        aplicarCantidad(grupo, campo);
      });
    });
  }

  /* ----------------------------- Tipo e inicio del proceso ----------------------------- */

  function mostrar(elemento, visible) {
    elemento.hidden = !visible;
    elemento.querySelectorAll("input, select, textarea").forEach(function (control) {
      control.disabled = !visible;
    });
  }

  function aplicarInicio(formulario) {
    // El tipo es un selector (Básico) o dos tarjetas de opción (crear: radios `tipo`).
    var tipo = formulario.querySelector('select[name="tipo"]');
    var tipoMarcado = formulario.querySelector('input[name="tipo"]:checked');
    var hayRadios = !!formulario.querySelector('input[name="tipo"]');
    var valorTipo = tipo ? tipo.value : tipoMarcado ? tipoMarcado.value : hayRadios ? "" : formulario.getAttribute("data-tipo");
    var esProceso = valorTipo === "PROCESO";
    var modo = formulario.querySelector('input[name="modo"]:checked');
    var programado = esProceso && !!modo && modo.value === "PROGRAMADO";
    // Crear: los datos del borrador aparecen cuando ya se eligió qué crear (sin JS se ven siempre).
    formulario.querySelectorAll("[data-tras-tipo]").forEach(function (el) {
      el.hidden = !valorTipo;
    });
    formulario.querySelectorAll("[data-solo-proceso]").forEach(function (el) {
      mostrar(el, esProceso);
    });
    formulario.querySelectorAll("[data-inicio-programado]").forEach(function (el) {
      mostrar(el, programado);
    });
    document.querySelectorAll("[data-oculto-si-programado]").forEach(function (el) {
      // Un panel de la navegación secundaria lo muestra la navegación; aquí solo se oculta si no aplica.
      if (el.hasAttribute("data-subpanel")) {
        if (programado) el.hidden = true;
        return;
      }
      // `data-deshabilitar-oculto`: además de ocultarlo, no se envía (crear: instrucciones de un programado).
      if (el.hasAttribute("data-deshabilitar-oculto")) mostrar(el, !programado);
      else el.hidden = programado;
    });
  }

  function iniciarInicio() {
    document.querySelectorAll("[data-config-inicio]").forEach(function (formulario) {
      aplicarInicio(formulario);
      formulario.addEventListener("change", function (evento) {
        var nombre = evento.target && evento.target.name;
        if (nombre === "tipo" || nombre === "modo") aplicarInicio(formulario);
      });
    });
  }

  /* --------------------------- Controles dependientes (4.A) --------------------------- */

  function valorDe(formulario, sufijo) {
    var marcado = formulario.querySelector('input[type="radio"][name$="' + sufijo + '"]:checked');
    if (marcado) return marcado.value;
    var control = formulario.querySelector('select[name$="' + sufijo + '"], input[name$="' + sufijo + '"]:not([type="radio"])');
    if (control && control.type === "checkbox") return control.checked ? control.value || "on" : "";
    return control ? control.value : "";
  }

  function aplicarMostrarSi(elemento) {
    // `data-mostrar-ambito` acota la búsqueda (p. ej. una fila de aprobador dentro de un formulario con varias).
    var formulario = elemento.closest("[data-mostrar-ambito]") || elemento.closest("form");
    if (!formulario) return;
    var regla = elemento.getAttribute("data-mostrar-si").split("=");
    var valores = (regla[1] || "").split(",");
    elemento.hidden = valores.indexOf(valorDe(formulario, regla[0])) === -1;
  }

  function iniciarMostrarSi() {
    var elementos = document.querySelectorAll("[data-mostrar-si]");
    elementos.forEach(aplicarMostrarSi);
    document.addEventListener("change", function (evento) {
      var formulario = evento.target && evento.target.closest && evento.target.closest("form");
      if (!formulario) return;
      formulario.querySelectorAll("[data-mostrar-si]").forEach(aplicarMostrarSi);
    });
  }

  /* ------------------- Cambios sin guardar y estado de guardado (4.A / 4.B) ------------------- */

  /* El estado es REAL: «Cambios sin guardar» cuando se escribió en un formulario y no se envió; «Guardando…» al
     enviarlo; tras la recarga, «Guardado» o «Error al guardar» según el mensaje que devolvió el servidor; si no,
     «Sin cambios». No hay autoguardado. El botón «Guardar» del pie envía el formulario con cambios cuando hay uno
     solo; con varios, avisa para guardar cada uno con su botón (cada formulario es una operación distinta). */

  var ESTADOS_GUARDADO = {
    limpio: "Sin cambios",
    sucio: "Cambios sin guardar",
    guardando: "Guardando…",
    guardado: "Guardado",
    error: "Error al guardar",
  };

  function pintarGuardado(estado) {
    document.querySelectorAll("[data-estado-guardado]").forEach(function (el) {
      el.setAttribute("data-estado", estado);
      var texto = el.querySelector("[data-estado-texto]") || el;
      texto.textContent = ESTADOS_GUARDADO[estado];
    });
  }

  // Formularios con cambios sin enviar (los comparte la navegación secundaria para guardar al avanzar).
  var sucios = [];

  function iniciarCambiosSinGuardar() {
    var panel = document.getElementById("studio-panel");
    if (!panel) return;
    var aviso = "Tienes cambios sin guardar en esta sección. ¿Salir sin guardarlos?";
    var botonGuardar = document.querySelector("[data-guardar]");

    var inicial = "limpio";
    if (document.querySelector(".messages .alert--danger")) inicial = "error";
    else if (document.querySelector(".messages .alert--success")) inicial = "guardado";
    pintarGuardado(inicial);

    function actualizar() {
      if (botonGuardar) {
        botonGuardar.disabled = sucios.length === 0;
        botonGuardar.title = sucios.length > 1 ? "Hay cambios en varios formularios: guárdalos uno a uno." : "";
      }
      pintarGuardado(sucios.length ? "sucio" : inicial === "error" ? "error" : inicial);
    }

    function marcar(evento) {
      var formulario = evento.target.form || evento.target.closest("form");
      if (!formulario || evento.target.closest("[data-sin-aviso]") || formulario.hasAttribute("data-sin-aviso")) return;
      if (formulario.method && formulario.method.toLowerCase() !== "post") return;
      if (sucios.indexOf(formulario) === -1) sucios.push(formulario);
      inicial = "limpio";
      actualizar();
    }
    panel.addEventListener("input", marcar);
    panel.addEventListener("change", marcar);
    document.addEventListener("submit", function (evento) {
      if (evento.defaultPrevented) return;
      // Enviar un formulario recarga la página: lo escrito en OTROS formularios se perdería.
      var otros = sucios.filter(function (f) {
        return f !== evento.target;
      });
      if (otros.length && !window.confirm(
        "Hay cambios sin guardar en otro grupo de esta sección y se perderán al guardar este. ¿Continuar?"
      )) {
        evento.preventDefault();
        return;
      }
      sucios = [];
      pintarGuardado("guardando");
      if (botonGuardar) botonGuardar.disabled = true;
    });
    if (botonGuardar) {
      botonGuardar.addEventListener("click", function () {
        if (sucios.length === 1) {
          if (typeof sucios[0].requestSubmit === "function") sucios[0].requestSubmit();
          else sucios[0].submit();
        } else if (sucios.length > 1 && window.Orbita && window.Orbita.toast) {
          window.Orbita.toast("Hay cambios en " + sucios.length + " formularios: guarda cada uno con su botón.", "info");
        }
      });
    }
    document.querySelectorAll("[data-studio-navegacion] a, a[data-studio-navegacion]").forEach(function (enlace) {
      enlace.addEventListener("click", function (evento) {
        if (sucios.length && !window.confirm(aviso)) {
          evento.preventDefault();
        } else {
          sucios = [];
        }
      });
    });
    window.addEventListener("beforeunload", function (evento) {
      if (!sucios.length) return;
      evento.preventDefault();
      evento.returnValue = aviso;
    });
    actualizar();
  }

  /* --------------------------- Confirmaciones (4.B, `data-confirmar`) --------------------------- */

  function iniciarConfirmaciones() {
    document.addEventListener("submit", function (evento) {
      var formulario = evento.target;
      var mensaje = formulario.getAttribute && formulario.getAttribute("data-confirmar");
      if (mensaje && formulario.getAttribute("data-confirmado") !== "1" && !window.confirm(mensaje)) {
        evento.preventDefault();
        evento.stopImmediatePropagation();
      }
    }, true);
  }

  /* -------------------- Navegación secundaria dentro de una sección (4.B) -------------------- */

  /* `[data-subnav-ambito="id"]` agrupa unos botones `[data-subnav-ir="clave"]` y unos paneles
     `[data-subpanel="clave"]`: se ve UN panel a la vez. El elegido se recuerda por servicio y sección
     (sessionStorage) para volver al mismo grupo después de guardar. Sin JS se ven todos los paneles.

     Con `data-subnav-asistente` el ámbito es además un ASISTENTE de pantallas (Básico):
     - Anterior / Continuar (`[data-subnav-anterior]`, `[data-subnav-siguiente]`, `[data-subnav-posicion]`).
     - Desbloqueo progresivo solo en la configuración inicial (`data-subnav-progresivo`, que el servidor pone al
       llegar desde «Crear borrador»): los grupos posteriores al alcanzado no se pueden abrir todavía; se
       desbloquean al continuar. Ningún grupo es obligatorio: Continuar siempre avanza. Un registro existente
       navega libremente. Volver a un grupo anterior siempre está permitido.
     - Continuar GUARDA si el grupo actual tiene cambios en UN formulario: lo envía al servidor (que valida y
       responde con su mensaje) y, solo si respondió con éxito, abre el grupo siguiente; si hubo error, se queda
       en el mismo grupo. Con cambios en varios formularios pide guardarlos uno a uno. Nunca simula guardar.
     - Los botones `[data-guardar-integrado]` (Guardar del formulario general) se ocultan: Continuar los
       sustituye; sin JS siguen ahí. */

  function guardarSesion(clave, valor) {
    try {
      if (valor === null) window.sessionStorage.removeItem(clave);
      else window.sessionStorage.setItem(clave, valor);
    } catch (e) {
      /* sin almacenamiento: no se recuerda */
    }
  }

  function leerSesion(clave) {
    try {
      return window.sessionStorage.getItem(clave);
    } catch (e) {
      return null;
    }
  }

  function clave(ambito, sufijo) {
    var raiz = document.querySelector("[data-studio]");
    return "orbita:studio:" + (raiz ? raiz.getAttribute("data-servicio") + ":" + raiz.getAttribute("data-tab") : "") +
      ":" + ambito.getAttribute("data-subnav-ambito") + (sufijo ? ":" + sufijo : "");
  }

  function propios(ambito, selector) {
    return Array.prototype.filter.call(ambito.querySelectorAll(selector), function (el) {
      return el.closest("[data-subnav-ambito]") === ambito;
    });
  }

  function disponibles(ambito) {
    // Grupos que aplican a este registro (p. ej. «Inicio» solo en un Proceso; «Visibilidad» no en uno programado).
    return propios(ambito, "[data-subnav-ir]").filter(function (boton) {
      return !boton.hidden;
    });
  }

  function indiceDe(ambito, destino) {
    return disponibles(ambito).map(function (b) {
      return b.getAttribute("data-subnav-ir");
    }).indexOf(destino);
  }

  function actualDe(ambito) {
    return ambito.getAttribute("data-subnav-actual") || "";
  }

  function alcanzadoDe(ambito) {
    var valor = parseInt(ambito.getAttribute("data-subnav-alcanzado"), 10);
    return isNaN(valor) ? Infinity : valor;
  }

  function desbloquear(ambito, indice) {
    if (alcanzadoDe(ambito) === Infinity) return;
    var alcanzado = Math.max(alcanzadoDe(ambito), indice);
    if (alcanzado >= disponibles(ambito).length - 1) {
      // Recorrido completo: desde aquí se navega libremente.
      ambito.removeAttribute("data-subnav-alcanzado");
      guardarSesion(clave(ambito, "alcanzado"), null);
    } else {
      ambito.setAttribute("data-subnav-alcanzado", String(alcanzado));
      guardarSesion(clave(ambito, "alcanzado"), String(alcanzado));
    }
  }

  function bloqueado(ambito, boton) {
    return disponibles(ambito).indexOf(boton) > alcanzadoDe(ambito);
  }

  function pintarAsistente(ambito) {
    var botones = disponibles(ambito);
    var indice = indiceDe(ambito, actualDe(ambito));
    propios(ambito, "[data-subnav-ir]").forEach(function (boton) {
      var cerrado = bloqueado(ambito, boton);
      // El `title` del servidor describe el estado guardado del grupo; bloquear solo lo sustituye mientras dure.
      if (!boton.hasAttribute("data-titulo-estado")) boton.setAttribute("data-titulo-estado", boton.getAttribute("title") || "");
      boton.classList.toggle("is-bloqueado", cerrado);
      if (cerrado) {
        boton.setAttribute("aria-disabled", "true");
        boton.title = "Se habilita al continuar desde el grupo anterior";
      } else {
        boton.removeAttribute("aria-disabled");
        var titulo = boton.getAttribute("data-titulo-estado");
        if (titulo) boton.title = titulo;
        else boton.removeAttribute("title");
      }
    });
    propios(ambito, "[data-subnav-anterior]").forEach(function (b) {
      b.disabled = indice <= 0;
    });
    propios(ambito, "[data-subnav-siguiente]").forEach(function (b) {
      var siguiente = botones[indice + 1];
      b.hidden = !siguiente;
      if (siguiente) {
        // Solo el nombre del grupo (el botón también lleva su indicador de estado).
        var nombre = siguiente.querySelector(".subnav__texto") || siguiente;
        b.textContent = "Continuar: " + nombre.textContent.trim();
      }
    });
    propios(ambito, "[data-subnav-posicion]").forEach(function (el) {
      el.textContent = indice >= 0 ? "Paso " + (indice + 1) + " de " + botones.length : "";
    });
  }

  function mostrarSubpanel(ambito, destino) {
    var encontrado = false;
    propios(ambito, "[data-subpanel]").forEach(function (panel) {
      var visible = panel.getAttribute("data-subpanel") === destino;
      panel.hidden = !visible;
      if (!panel.id) panel.id = "subpanel-" + ambito.getAttribute("data-subnav-ambito") + "-" + panel.getAttribute("data-subpanel");
      encontrado = encontrado || visible;
    });
    if (!encontrado) return false;
    propios(ambito, "[data-subnav-ir]").forEach(function (boton) {
      var activo = boton.getAttribute("data-subnav-ir") === destino;
      boton.classList.toggle("is-activo", activo);
      boton.setAttribute("aria-selected", activo ? "true" : "false");
      boton.setAttribute("aria-controls", "subpanel-" + ambito.getAttribute("data-subnav-ambito") + "-" + boton.getAttribute("data-subnav-ir"));
      boton.tabIndex = activo ? 0 : -1;
    });
    ambito.setAttribute("data-subnav-actual", destino);
    guardarSesion(clave(ambito), destino);
    if (ambito.hasAttribute("data-subnav-asistente")) pintarAsistente(ambito);
    return true;
  }

  function formulariosConCambios(panel) {
    // Un control puede estar en el panel y pertenecer a un formulario de fuera (atributo `form=`).
    return sucios.filter(function (formulario) {
      if (panel.contains(formulario)) return true;
      return Array.prototype.some.call(formulario.elements, function (control) {
        return panel.contains(control);
      });
    });
  }

  function continuar(ambito) {
    var botones = disponibles(ambito);
    var indice = indiceDe(ambito, actualDe(ambito));
    var siguiente = botones[indice + 1];
    if (!siguiente) return;
    var destino = siguiente.getAttribute("data-subnav-ir");
    var panel = propios(ambito, "[data-subpanel]").filter(function (p) {
      return p.getAttribute("data-subpanel") === actualDe(ambito);
    })[0];
    var pendientes = panel ? formulariosConCambios(panel) : [];
    if (pendientes.length > 1) {
      window.alert("Hay cambios en " + pendientes.length + " formularios de este grupo: guarda cada uno con su botón antes de continuar.");
      return;
    }
    if (pendientes.length === 1) {
      var formulario = pendientes[0];
      if (typeof formulario.reportValidity === "function" && !formulario.reportValidity()) return;
      // Se abrirá el grupo siguiente solo si el servidor confirma que guardó (ver `iniciarSubnav`).
      guardarSesion(clave(ambito, "pendiente"), JSON.stringify({ origen: actualDe(ambito), destino: destino, indice: indice + 1 }));
      if (typeof formulario.requestSubmit === "function") formulario.requestSubmit();
      else formulario.submit();
      return;
    }
    desbloquear(ambito, indice + 1);
    mostrarSubpanel(ambito, destino);
    var foco = propios(ambito, "[data-subpanel]").filter(function (p) {
      return !p.hidden;
    })[0];
    if (foco) {
      foco.setAttribute("tabindex", "-1");
      foco.focus({ preventScroll: false });
    }
  }

  function iniciarSubnav() {
    var exito = !!document.querySelector(".messages .alert--success") && !document.querySelector(".messages .alert--danger");
    document.querySelectorAll("[data-subnav-ambito]").forEach(function (ambito) {
      var asistente = ambito.hasAttribute("data-subnav-asistente");
      var inicialServidor = ambito.getAttribute("data-subnav-inicial");
      var inicial = inicialServidor || leerSesion(clave(ambito));
      if (asistente) {
        var alcanzado = leerSesion(clave(ambito, "alcanzado"));
        if (ambito.hasAttribute("data-subnav-progresivo") && alcanzado === null) alcanzado = "0";
        if (alcanzado !== null) ambito.setAttribute("data-subnav-alcanzado", alcanzado);
        var pendiente = leerSesion(clave(ambito, "pendiente"));
        guardarSesion(clave(ambito, "pendiente"), null);
        if (pendiente) {
          try {
            pendiente = JSON.parse(pendiente);
          } catch (e) {
            pendiente = null;
          }
        }
        if (pendiente) {
          if (exito) {
            desbloquear(ambito, pendiente.indice);
            inicial = pendiente.destino;
          } else {
            inicial = pendiente.origen;
          }
        }
        if (inicial && bloqueado(ambito, propios(ambito, '[data-subnav-ir="' + inicial + '"]')[0])) inicial = null;
        propios(ambito, "[data-guardar-integrado]").forEach(function (el) {
          el.hidden = true;
        });
        propios(ambito, "[data-subnav-pasos]").forEach(function (el) {
          el.hidden = false;
        });
      }
      var primero = disponibles(ambito)[0] || propios(ambito, "[data-subnav-ir]")[0];
      if (!inicial || !mostrarSubpanel(ambito, inicial)) {
        if (primero) mostrarSubpanel(ambito, primero.getAttribute("data-subnav-ir"));
      }
      ambito.addEventListener("click", function (evento) {
        if (evento.target.closest("[data-subnav-siguiente]")) {
          evento.preventDefault();
          continuar(ambito);
          return;
        }
        if (evento.target.closest("[data-subnav-anterior]")) {
          evento.preventDefault();
          var anterior = disponibles(ambito)[indiceDe(ambito, actualDe(ambito)) - 1];
          if (anterior) mostrarSubpanel(ambito, anterior.getAttribute("data-subnav-ir"));
          return;
        }
        var boton = evento.target.closest("[data-subnav-ir]");
        if (!boton || boton.closest("[data-subnav-ambito]") !== ambito) return;
        evento.preventDefault();
        if (asistente && bloqueado(ambito, boton)) return;
        mostrarSubpanel(ambito, boton.getAttribute("data-subnav-ir"));
      });
      ambito.addEventListener("keydown", function (evento) {
        var boton = evento.target.closest("[data-subnav-ir]");
        if (!boton || boton.closest("[data-subnav-ambito]") !== ambito) return;
        if (["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp", "Home", "End"].indexOf(evento.key) === -1) return;
        var botones = disponibles(ambito).filter(function (b) {
          return !(asistente && bloqueado(ambito, b));
        });
        var actual = botones.indexOf(boton);
        var indice;
        if (evento.key === "Home") indice = 0;
        else if (evento.key === "End") indice = botones.length - 1;
        else indice = (actual + (evento.key === "ArrowRight" || evento.key === "ArrowDown" ? 1 : -1) + botones.length) % botones.length;
        var siguiente = botones[indice];
        if (!siguiente) return;
        evento.preventDefault();
        siguiente.focus();
        mostrarSubpanel(ambito, siguiente.getAttribute("data-subnav-ir"));
      });
    });
  }

  /* ------------------------ Asistente por pasos dentro de un formulario (4.B) ------------------------ */

  /* `form[data-pasos]` con `fieldset[data-paso="Título"]`: un paso a la vez con Anterior / Siguiente y un índice
     de pasos. Sin JS se ven todos. Enviar siempre manda el formulario completo (no hay guardado parcial). */

  function iniciarPasos() {
    document.querySelectorAll("form[data-pasos]").forEach(function (formulario) {
      var pasos = Array.prototype.slice.call(formulario.querySelectorAll("[data-paso]"));
      if (pasos.length < 2) return;
      var indice = 0;
      var indiceNav = document.createElement("ol");
      indiceNav.className = "pasos";
      pasos.forEach(function (paso, i) {
        var item = document.createElement("li");
        var boton = document.createElement("button");
        boton.type = "button";
        boton.className = "pasos__item";
        boton.textContent = i + 1 + ". " + paso.getAttribute("data-paso");
        boton.addEventListener("click", function () {
          ir(i);
        });
        item.appendChild(boton);
        indiceNav.appendChild(item);
      });
      formulario.insertBefore(indiceNav, pasos[0]);
      var controles = document.createElement("div");
      controles.className = "pasos__controles";
      var anterior = document.createElement("button");
      anterior.type = "button";
      anterior.className = "btn btn--ghost btn--sm";
      anterior.textContent = "Anterior";
      var siguiente = document.createElement("button");
      siguiente.type = "button";
      siguiente.className = "btn btn--secondary btn--sm";
      siguiente.textContent = "Siguiente";
      controles.appendChild(anterior);
      controles.appendChild(siguiente);
      pasos[pasos.length - 1].parentNode.insertBefore(controles, pasos[pasos.length - 1].nextSibling);
      anterior.addEventListener("click", function () {
        ir(indice - 1);
      });
      siguiente.addEventListener("click", function () {
        ir(indice + 1);
      });

      function ir(nuevo) {
        indice = Math.max(0, Math.min(pasos.length - 1, nuevo));
        pasos.forEach(function (paso, i) {
          paso.hidden = i !== indice;
        });
        indiceNav.querySelectorAll(".pasos__item").forEach(function (b, i) {
          b.classList.toggle("is-activo", i === indice);
          b.setAttribute("aria-current", i === indice ? "step" : "false");
        });
        anterior.disabled = indice === 0;
        siguiente.hidden = indice === pasos.length - 1;
      }
      // Si el servidor devolvió el formulario con un campo inválido, se abre su paso.
      var conError = pasos.findIndex(function (paso) {
        return paso.querySelector(".errorlist, [aria-invalid='true']");
      });
      ir(conError > -1 ? conError : 0);
    });
  }

  /* --------------------- Propiedades del elemento seleccionado (constructor, 4.B) --------------------- */

  /* En `[data-constructor]`, `[data-mostrar-propiedad="id"]` muestra el panel `[data-propiedad="id"]` del
     contenedor `[data-propiedades]` (uno a la vez) y resalta su fila. La selección se recuerda (sessionStorage) y se
     restaura tras guardar. Sin JS se ven todos los paneles. */

  function iniciarPropiedades() {
    document.querySelectorAll("[data-constructor]").forEach(function (constructor) {
      var contenedor = constructor.querySelector("[data-propiedades]");
      if (!contenedor) return;
      var paneles = Array.prototype.slice.call(contenedor.querySelectorAll("[data-propiedad]"));
      var raiz = document.querySelector("[data-studio]");
      var claveGuardado = "orbita:studio:" + (raiz ? raiz.getAttribute("data-servicio") : "") + ":propiedad:" +
        window.location.search;

      function mostrar(id) {
        var panel = paneles.filter(function (p) {
          return p.getAttribute("data-propiedad") === id;
        })[0];
        if (!panel) panel = paneles[0];
        paneles.forEach(function (p) {
          p.hidden = p !== panel;
        });
        constructor.querySelectorAll(".campo-fila").forEach(function (fila) {
          var activa = fila.getAttribute("data-mostrar-propiedad") === panel.getAttribute("data-propiedad");
          fila.classList.toggle("is-seleccionado", activa);
          var nombre = fila.querySelector(".campo-fila__nombre");
          if (nombre) {
            if (activa) nombre.setAttribute("aria-current", "true");
            else nombre.removeAttribute("aria-current");
          }
        });
        // Sin nada seleccionado no hay nada que editar: el modo ampliado se cierra.
        if (panel === paneles[0]) ampliar(false);
        try {
          window.sessionStorage.setItem(claveGuardado, panel.getAttribute("data-propiedad"));
        } catch (e) {
          /* sin almacenamiento */
        }
      }

      /* Modo de edición AMPLIADO (4.B): el panel de propiedades ocupa también el ancho de la lista, dentro del
         mismo constructor. Es solo presentación: no guarda nada ni cambia de página. */
      var claveAmpliado = claveGuardado + ":ampliado";

      function ampliar(activo) {
        constructor.classList.toggle("is-ampliado", activo);
        contenedor.querySelectorAll("[data-propiedades-ampliar]").forEach(function (boton) {
          boton.setAttribute("aria-pressed", activo ? "true" : "false");
          boton.setAttribute("aria-label", activo ? "Reducir el panel de edición" : "Ampliar el panel de edición");
        });
        try {
          window.sessionStorage.setItem(claveAmpliado, activo ? "1" : "");
        } catch (e) {
          /* sin almacenamiento */
        }
      }

      contenedor.addEventListener("click", function (evento) {
        if (!evento.target.closest("[data-propiedades-ampliar]")) return;
        ampliar(!constructor.classList.contains("is-ampliado"));
      });
      constructor.addEventListener("keydown", function (evento) {
        if (evento.key === "Escape" && constructor.classList.contains("is-ampliado")) ampliar(false);
      });

      constructor.addEventListener("click", function (evento) {
        var disparador = evento.target.closest("[data-mostrar-propiedad]");
        if (!disparador || evento.target.closest("form")) return;
        mostrar(disparador.getAttribute("data-mostrar-propiedad"));
        if (disparador.hasAttribute("data-ampliar-propiedad")) ampliar(true);
        // Al seleccionar desde la lista, el panel de propiedades queda a la vista en pantallas pequeñas.
        if (window.matchMedia && window.matchMedia("(max-width: 1279px)").matches) contenedor.scrollIntoView({ block: "start" });
      });
      var previo = null;
      try {
        previo = window.sessionStorage.getItem(claveGuardado);
      } catch (e) {
        previo = null;
      }
      mostrar(previo || paneles[0].getAttribute("data-propiedad"));
      var ampliadoPrevio = "";
      try {
        ampliadoPrevio = window.sessionStorage.getItem(claveAmpliado) || "";
      } catch (e) {
        ampliadoPrevio = "";
      }
      if (ampliadoPrevio && !(paneles[0] && !paneles[0].hidden)) ampliar(true);
    });
  }

  /* --------------------------- Selección múltiple accesible (4.B) --------------------------- */

  /* `select[multiple][data-multiselect]` → chips de lo elegido (con «quitar»), un buscador y una lista con
     casillas. El <select> original sigue siendo lo que se envía (oculto y sincronizado). */

  function iniciarMultiselect() {
    document.querySelectorAll("select[multiple][data-multiselect]").forEach(function (select, n) {
      if (select.getAttribute("data-multiselect-listo")) return;
      select.setAttribute("data-multiselect-listo", "1");
      var envoltura = document.createElement("div");
      envoltura.className = "multiselect";
      var chips = document.createElement("ul");
      chips.className = "multiselect__chips";
      chips.setAttribute("aria-label", "Seleccionados");
      var buscador = document.createElement("input");
      buscador.type = "search";
      buscador.className = "multiselect__buscar";
      buscador.placeholder = "Buscar…";
      buscador.setAttribute("aria-label", "Buscar en " + ((select.labels && select.labels[0] && select.labels[0].textContent) || "la lista"));
      var lista = document.createElement("ul");
      lista.className = "multiselect__lista";
      lista.setAttribute("role", "group");
      envoltura.appendChild(chips);
      envoltura.appendChild(buscador);
      envoltura.appendChild(lista);
      select.parentNode.insertBefore(envoltura, select.nextSibling);
      select.hidden = true;

      function pintar() {
        chips.innerHTML = "";
        lista.innerHTML = "";
        var filtro = buscador.value.trim().toLowerCase();
        Array.prototype.forEach.call(select.options, function (opcion, i) {
          if (opcion.selected) {
            var chip = document.createElement("li");
            chip.className = "chip multiselect__chip";
            chip.textContent = opcion.text;
            var quitar = document.createElement("button");
            quitar.type = "button";
            quitar.setAttribute("aria-label", "Quitar " + opcion.text);
            quitar.textContent = "×";
            quitar.addEventListener("click", function () {
              opcion.selected = false;
              select.dispatchEvent(new Event("change", { bubbles: true }));
              pintar();
            });
            chip.appendChild(quitar);
            chips.appendChild(chip);
          }
          if (filtro && opcion.text.toLowerCase().indexOf(filtro) === -1) return;
          var item = document.createElement("li");
          var etiqueta = document.createElement("label");
          etiqueta.className = "multiselect__opcion";
          var casilla = document.createElement("input");
          casilla.type = "checkbox";
          casilla.checked = opcion.selected;
          casilla.id = "ms-" + n + "-" + i;
          casilla.addEventListener("change", function () {
            opcion.selected = casilla.checked;
            select.dispatchEvent(new Event("change", { bubbles: true }));
            pintar();
            var nueva = document.getElementById(casilla.id);
            if (nueva) nueva.focus();
          });
          etiqueta.appendChild(casilla);
          etiqueta.appendChild(document.createTextNode(" " + opcion.text));
          item.appendChild(etiqueta);
          lista.appendChild(item);
        });
        if (!chips.children.length) {
          var nada = document.createElement("li");
          nada.className = "multiselect__nada";
          nada.textContent = "Nada seleccionado";
          chips.appendChild(nada);
        }
      }
      buscador.addEventListener("input", pintar);
      pintar();
    });
  }

  /* --------------------- Crear borrador: selección → configuración (4.B) --------------------- */

  /* `[data-crear]` tiene dos estados visuales en la MISMA vista (`data-crear-estado`): «seleccion» (solo las dos
     tarjetas) y «configuracion» (tarjetas compactas arriba y el formulario). Elegir o cambiar de tarjeta no
     recarga ni cambia la URL; nombre, categoría y descripción se conservan porque son los mismos controles, y lo
     que no aplica lo deshabilita `aplicarInicio` (no se envía). `form[data-envio-unico]` se envía una sola vez. */

  function iniciarCrear() {
    document.querySelectorAll("[data-crear]").forEach(function (vista) {
      vista.setAttribute("data-crear-js", "");
      var configuracion = vista.querySelector("[data-crear-configuracion]");
      vista.addEventListener("change", function (evento) {
        if (!evento.target || evento.target.name !== "tipo") return;
        var primera = vista.getAttribute("data-crear-estado") !== "configuracion";
        vista.setAttribute("data-crear-estado", "configuracion");
        if (primera && configuracion) {
          var nombre = configuracion.querySelector('input[name="nombre"]');
          // Tras la transición, el foco pasa al primer dato del borrador (sin desplazar de golpe).
          window.setTimeout(function () {
            if (nombre) nombre.focus({ preventScroll: true });
          }, 200);
        }
      });
    });
  }

  function iniciarEnvioUnico() {
    document.addEventListener("submit", function (evento) {
      var formulario = evento.target;
      if (!formulario.hasAttribute || !formulario.hasAttribute("data-envio-unico")) return;
      if (formulario.getAttribute("data-enviando") === "1") {
        evento.preventDefault();
        return;
      }
      if (evento.defaultPrevented) return;
      formulario.setAttribute("data-enviando", "1");
      formulario.querySelectorAll("[data-enviar]").forEach(function (boton) {
        boton.setAttribute("aria-disabled", "true");
        boton.textContent = "Creando…";
      });
    });
    // Volver con «Atrás» del navegador restaura la página desde caché: se habilita de nuevo.
    window.addEventListener("pageshow", function () {
      document.querySelectorAll("form[data-envio-unico]").forEach(function (formulario) {
        formulario.removeAttribute("data-enviando");
        formulario.querySelectorAll("[data-enviar]").forEach(function (boton) {
          boton.removeAttribute("aria-disabled");
          boton.textContent = "Crear borrador";
        });
      });
    });
  }

  // Arranque: un fallo aislado de un comportamiento no debe dejar sin los demás.
  function iniciar() {
    [
      iniciarSoloTipos, iniciarAprobacion, iniciarInicio, iniciarMostrarSi, iniciarCambiosSinGuardar,
      iniciarConfirmaciones, iniciarSubnav, iniciarPasos, iniciarPropiedades, iniciarMultiselect, iniciarCrear,
      iniciarEnvioUnico,
    ].forEach(function (comportamiento) {
      try {
        comportamiento();
      } catch (error) {
        if (window.console) window.console.error(error);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", iniciar);
  } else {
    iniciar();
  }
})();
