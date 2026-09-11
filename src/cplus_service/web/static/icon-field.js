// Live verdict on an SF Symbol name typed into an action's icon field.
//
// [data-known] carries the space-separated names this service can vouch for —
// the same list the field's <datalist> autocompletes from. A name in it gets a
// tick; anything else gets a warning that says what will happen rather than
// blocking the save, because no server can know which symbols a given tvOS
// version actually ships. Canopy+ falls back when it cannot draw one.
(function () {
  var FALLBACK = "arrow.down.circle";

  function verdictFor(field) {
    var value = field.value.trim();
    var known = (field.getAttribute("data-known") || "").split(/\s+/);

    if (!value) return { text: "", className: "icon-verdict" };
    if (known.indexOf(value) !== -1) {
      return { text: "✓ known symbol", className: "icon-verdict ok" };
    }
    return {
      text: "⚠ can't verify this symbol — falls back to " + FALLBACK,
      className: "icon-verdict warn",
    };
  }

  function render(field) {
    var slot = field.parentNode.querySelector(".icon-verdict");
    if (!slot) return;
    var verdict = verdictFor(field);
    slot.textContent = verdict.text;
    slot.className = verdict.className;
  }

  function fields() {
    return Array.prototype.slice.call(document.querySelectorAll(".icon-field"));
  }

  document.addEventListener("input", function (event) {
    if (event.target.classList.contains("icon-field")) render(event.target);
  });

  // On load too, so a saved name that this build no longer recognises says so
  // before anyone touches the field.
  fields().forEach(render);
})();
