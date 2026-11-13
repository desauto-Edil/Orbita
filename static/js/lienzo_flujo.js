/* Lienzo técnico de flujos (sacado de la plantilla para la CSP, Versión 1). */
(function () {
  var workspace = document.querySelector("[data-flow-workspace]");
  if (!workspace) return;

  var inspector = workspace.querySelector("#flow-inspector");
  var toggle = workspace.querySelector("[data-flow-toggle-inspector]");
  var panels = Array.prototype.slice.call(workspace.querySelectorAll(".flow-inspector__section.is-action-panel"));
  var nodes = Array.prototype.slice.call(workspace.querySelectorAll("[data-flow-select]"));

  function setInspector(open) {
    workspace.classList.toggle("is-inspector-collapsed", !open);
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function openPanel(id) {
    setInspector(true);
    panels.forEach(function (panel) {
      panel.hidden = panel.id !== id;
    });
    var target = id ? document.getElementById(id) : null;
    if (target) target.hidden = false;
  }

  workspace.addEventListener("click", function (event) {
    var open = event.target.closest("[data-flow-open-inspector]");
    if (open) {
      event.preventDefault();
      openPanel(open.getAttribute("data-target"));
      return;
    }

    if (event.target.closest("[data-flow-close-inspector]")) {
      event.preventDefault();
      setInspector(false);
      return;
    }

    if (event.target.closest("[data-flow-toggle-inspector]")) {
      event.preventDefault();
      setInspector(workspace.classList.contains("is-inspector-collapsed"));
      return;
    }

    var selected = event.target.closest("[data-flow-select]");
    if (selected && !event.target.closest(".flow-menu")) {
      nodes.forEach(function (node) { node.classList.remove("is-selected"); });
      selected.classList.add("is-selected");
      openPanel(selected.getAttribute("data-target"));
    }
  });

  workspace.addEventListener("keydown", function (event) {
    if ((event.key === "Enter" || event.key === " ") && event.target.matches("[data-flow-select]")) {
      event.preventDefault();
      event.target.click();
    }
  });
})();
