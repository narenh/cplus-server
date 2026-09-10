// A shelf-shaped row's "live" fields — a Home shelf, the Carousel, or Top
// Shelf. Everything here is live except Title, which is Save-gated (see
// dirty-save.js) because typing is not an event you want to fire a request
// on every keystroke of. Content, Style and "Hide release year" all apply
// the moment they change (data-auto-apply, submitted via the field's own
// form) — and so does picking "Collection Items…" in the Content select
// itself: the server resolves that straight to the library's own first
// collection (see libraries._apply_shelf_update), so there is never a
// moment where the picker shows content but nothing has actually been
// saved yet.
//
// [data-collection-source] is the second picker that then appears. It is
// not part of either of the row's own two forms — it has no Style or "Hide
// release year" of its own, and changing the source always resets those
// server-side regardless — so it posts directly via htmx.ajax with just the
// two fields the server needs: which collection, and its title, read off
// the option's own text since Plex collection ids are not scoped to a
// library and the server has no other way to learn it.
(function () {
  document.addEventListener("change", function (event) {
    var field = event.target;

    if (field.matches("[data-auto-apply]")) {
      field.form.requestSubmit();
      return;
    }

    if (field.matches("[data-collection-source]")) {
      var option = field.selectedOptions[0];
      if (!option || !option.value) return;
      var target = document.querySelector(field.dataset.swapTarget);
      if (!target) return;
      htmx.ajax("POST", field.dataset.applyUrl, {
        target: target,
        swap: "outerHTML",
        values: { source: option.value, collection_title: option.textContent.trim() },
      });
    }
  });
})();
