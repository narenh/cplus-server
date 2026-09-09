// Show a form's Save control only once one of its own fields differs from
// the value it started with — editing something back to what it already was,
// or not touching anything at all, has nothing to save.
//
// A field opts in with [data-original="<starting value>"] (a checkbox's
// starting value is the literal string "true" or "false"); the form's Save
// button carries [data-save-for="<form id>"] and starts with the .hidden
// class. That class (display:none !important), not the native `hidden`
// attribute, is what actually hides it — this app's own button/.button rule
// sets display:inline-block with no !important, and an ordinary author rule
// beats the UA stylesheet's [hidden] regardless of specificity, so the native
// attribute alone would never actually hide anything here.
//
// `field.form` resolves the owning form natively, including one associated
// purely via a `form="<id>"` attribute rather than DOM nesting — every field
// on this page's rows is wired up that way. `form.elements` correspondingly
// includes every such field regardless of where it sits in the DOM, which is
// what lets one field's change re-check the whole form rather than just
// itself: a shelf row's Save should show for *any* of its four fields, not
// only the one just touched.
//
// Delegated on `document`, so it keeps working after an htmx swap replaces a
// row with a freshly server-rendered one — the new row's own `data-original`
// values are whatever is now saved, so it always starts with Save correctly
// hidden again, before any script has to do anything.
(function () {
  function isDirty(field) {
    var current = field.type === "checkbox" ? String(field.checked) : field.value;
    return current !== field.dataset.original;
  }

  function sync(form) {
    var button = document.querySelector('[data-save-for="' + form.id + '"]');
    if (!button) return;

    var tracked = Array.from(form.elements).filter(function (el) {
      return el.dataset.original !== undefined;
    });
    button.classList.toggle("hidden", !tracked.some(isDirty));
  }

  function onFieldEvent(event) {
    var field = event.target;
    var form = field.form;
    if (form && field.dataset.original !== undefined) sync(form);
  }

  // `change` covers checkboxes and (in older engines) <select>; `input`
  // covers text fields and, in every current browser, <select> too. Both are
  // cheap to run twice on the same edit.
  document.addEventListener("input", onFieldEvent);
  document.addEventListener("change", onFieldEvent);
})();
