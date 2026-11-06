/* Piezas (UX-5) — visor de una pieza y anotación visual de una revisión. Mejora progresiva.

   - Cada tarjeta `[data-pieza]` tiene su `<template id>` con el visor (servidor). Al pulsarla se monta en UN
     `<dialog class="pz-visor">`: el medio a la izquierda y, a la derecha, la información y las acciones (formularios
     normales que van a la operación de siempre). Se cierra con el botón, Escape o un clic fuera. Sin JS, la tarjeta
     lleva a la ficha del ítem.
   - Anotación (`[data-anotacion]`, solo en la aprobación de un ítem, nunca en una gaceta): lápiz o resaltador en
     tres colores tomados de los tokens de estado, deshacer y borrar. Si hay trazos al enviar la decisión, se compone
     la imagen con ellos en un PNG (máx. 2000 px por lado) y viaja en el campo `anotacion` del mismo formulario:
     «apenas hay un rayón, acompaña a la revisión». El servidor la valida (`revision_piezas.decodificar_anotacion`). */
(function () {
  "use strict";

  if (!document.querySelector("[data-pieza]")) return;

  var dialogo = null;
  var origen = null;

  function crearDialogo() {
    dialogo = document.createElement("dialog");
    dialogo.className = "pz-visor";
    dialogo.setAttribute("aria-label", "Pieza");
    dialogo.addEventListener("click", function (e) {
      if (e.target === dialogo) dialogo.close();
      if (e.target.closest("[data-pieza-cerrar]")) dialogo.close();
    });
    dialogo.addEventListener("close", function () {
      dialogo.innerHTML = "";
      if (origen) origen.focus({ preventScroll: true });
    });
    document.body.appendChild(dialogo);
  }

  function abrir(enlace) {
    var plantilla = document.getElementById(enlace.getAttribute("data-pieza"));
    if (!plantilla || !window.HTMLDialogElement) return false;
    if (!dialogo) crearDialogo();
    origen = enlace;
    dialogo.innerHTML = "";
    dialogo.appendChild(plantilla.content.cloneNode(true));
    var titulo = dialogo.querySelector(".pzv__titulo");
    if (titulo) dialogo.setAttribute("aria-label", titulo.textContent.trim());
    dialogo.showModal();
    var lienzo = dialogo.querySelector("[data-anotacion]");
    if (lienzo) prepararAnotacion(dialogo, lienzo);
    var comentario = dialogo.querySelector(".pzv__pie textarea");
    if (comentario && !lienzo) comentario.focus({ preventScroll: true });
    return true;
  }

  document.addEventListener("click", function (e) {
    var enlace = e.target.closest("[data-pieza]");
    if (!enlace || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (abrir(enlace)) e.preventDefault();
  });

  // Abrir la pieza indicada en la dirección (`#pieza-<id>`) tras volver de una acción.
  if (window.location.hash.indexOf("#pieza-") === 0) {
    var li = document.getElementById(window.location.hash.slice(1));
    var enlaceInicial = li && li.querySelector("[data-pieza]");
    if (enlaceInicial) enlaceInicial.scrollIntoView({ block: "center" });
  }

  /* ------------------------------------------------------------------ anotación */

  function token(nombre) {
    return getComputedStyle(document.documentElement).getPropertyValue(nombre).trim() || "currentColor";
  }

  function prepararAnotacion(raiz, lienzo) {
    var imagen = lienzo.querySelector("[data-anotacion-imagen]");
    var canvas = lienzo.querySelector("[data-anotacion-trazos]");
    var ctx = canvas.getContext("2d");
    var form = raiz.querySelector("[data-pieza-form]");
    var campo = form && form.querySelector("[data-anotacion-campo]");
    var aviso = form && form.querySelector("[data-anotacion-aviso]");
    var herramienta = "lapiz";
    var color = token("--status-danger");
    var trazos = [];
    var actual = null;

    function ajustar() {
      if (!imagen.naturalWidth) return;
      canvas.width = imagen.naturalWidth;
      canvas.height = imagen.naturalHeight;
      redibujar();
    }

    function grosor(t) {
      var base = Math.max(canvas.width, canvas.height) / 300;
      return t.herramienta === "resaltador" ? base * 6 : base * 1.2;
    }

    function pintar(t) {
      if (t.puntos.length < 1) return;
      ctx.save();
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.strokeStyle = t.color;
      ctx.globalAlpha = t.herramienta === "resaltador" ? 0.35 : 1;
      ctx.lineWidth = grosor(t);
      ctx.beginPath();
      ctx.moveTo(t.puntos[0][0], t.puntos[0][1]);
      for (var i = 1; i < t.puntos.length; i += 1) ctx.lineTo(t.puntos[i][0], t.puntos[i][1]);
      if (t.puntos.length === 1) ctx.lineTo(t.puntos[0][0] + 0.1, t.puntos[0][1]);
      ctx.stroke();
      ctx.restore();
    }

    function redibujar() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      trazos.forEach(pintar);
      if (aviso) aviso.hidden = trazos.length === 0;
    }

    function punto(e) {
      var caja = canvas.getBoundingClientRect();
      return [
        ((e.clientX - caja.left) / caja.width) * canvas.width,
        ((e.clientY - caja.top) / caja.height) * canvas.height,
      ];
    }

    canvas.addEventListener("pointerdown", function (e) {
      if (!canvas.width) return;
      canvas.setPointerCapture(e.pointerId);
      actual = { herramienta: herramienta, color: color, puntos: [punto(e)] };
      trazos.push(actual);
      redibujar();
    });
    canvas.addEventListener("pointermove", function (e) {
      if (!actual) return;
      actual.puntos.push(punto(e));
      redibujar();
    });
    function soltar() { actual = null; }
    canvas.addEventListener("pointerup", soltar);
    canvas.addEventListener("pointercancel", soltar);

    raiz.querySelectorAll("[data-herramienta]").forEach(function (boton) {
      boton.addEventListener("click", function () {
        herramienta = boton.getAttribute("data-herramienta");
        raiz.querySelectorAll("[data-herramienta]").forEach(function (b) {
          var activa = b === boton;
          b.classList.toggle("is-activa", activa);
          b.setAttribute("aria-pressed", activa ? "true" : "false");
        });
      });
    });
    raiz.querySelectorAll("[data-color]").forEach(function (boton) {
      boton.addEventListener("click", function () {
        color = token(boton.getAttribute("data-color"));
        raiz.querySelectorAll("[data-color]").forEach(function (b) {
          var activo = b === boton;
          b.classList.toggle("is-activo", activo);
          b.setAttribute("aria-pressed", activo ? "true" : "false");
        });
      });
    });
    var deshacer = raiz.querySelector("[data-anotacion-deshacer]");
    if (deshacer) deshacer.addEventListener("click", function () { trazos.pop(); redibujar(); });
    var limpiar = raiz.querySelector("[data-anotacion-limpiar]");
    if (limpiar) limpiar.addEventListener("click", function () { trazos = []; redibujar(); });

    if (imagen.complete) ajustar();
    else imagen.addEventListener("load", ajustar);

    if (form && campo) {
      form.addEventListener("submit", function () {
        if (!trazos.length || !canvas.width) {
          campo.value = "";
          return;
        }
        // Imagen + trazos en un PNG, reducido si es muy grande (la imagen es del mismo origen: no se «contamina»).
        var escala = Math.min(1, 2000 / Math.max(canvas.width, canvas.height));
        var salida = document.createElement("canvas");
        salida.width = Math.round(canvas.width * escala);
        salida.height = Math.round(canvas.height * escala);
        var sctx = salida.getContext("2d");
        sctx.drawImage(imagen, 0, 0, salida.width, salida.height);
        sctx.drawImage(canvas, 0, 0, salida.width, salida.height);
        try {
          campo.value = salida.toDataURL("image/png");
        } catch (err) {
          campo.value = "";
        }
      });
    }
  }
})();
