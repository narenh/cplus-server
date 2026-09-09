// Show a text field's Save control only once its value differs from the
// one it started with — editing a name back to what it already was, or not
// touching it at all, has nothing to save.
//
// A field opts in with [data-original="<starting value>"]; its Save button
// carries [data-save-for="<field id>"] and starts `hidden`. Delegated on
// `document`, so it keeps working after an htmx swap replaces the field and
// its button with freshly server-rendered ones — the newly inserted field's
// own `data-original` is what it currently is, so a fresh row always starts
// with its Save button correctly hidden already, before any script runs.
(function () {
  document.addEventListener("input", function (event) {
    var field = event.target;
    if (!field.matches("[data-original]")) return;

    var button = document.querySelector('[data-save-for="' + field.id + '"]');
    if (button) button.hidden = field.value === field.dataset.original;
  });
})();
