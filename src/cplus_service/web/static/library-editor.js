// Show a text field's Save control only once its value differs from the
// one it started with — editing a name back to what it already was, or not
// touching it at all, has nothing to save.
//
// A field opts in with [data-original="<starting value>"]; its Save button
// carries [data-save-for="<field id>"] and starts with the .hidden class.
// That class (display:none !important), not the native `hidden` attribute,
// is what actually hides it — this app's own button/.button rule sets
// display:inline-block with no !important, and an ordinary author rule beats
// the UA stylesheet's [hidden] regardless of specificity, so the native
// attribute alone never actually hid anything here.
//
// Delegated on `document`, so it keeps working after an htmx swap replaces
// the field and its button with freshly server-rendered ones — the newly
// inserted field's own `data-original` is what it currently is, so a fresh
// row always starts with its Save button correctly hidden already, before
// any script runs.
(function () {
  document.addEventListener("input", function (event) {
    var field = event.target;
    if (!field.matches("[data-original]")) return;

    var button = document.querySelector('[data-save-for="' + field.id + '"]');
    if (button) button.classList.toggle("hidden", field.value === field.dataset.original);
  });
})();
