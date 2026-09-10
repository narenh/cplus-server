// A shelf row's "live" fields — everything except its Title, which is
// Save-gated (see dirty-save.js) because typing is not an event you want to
// fire a request on every keystroke of. Content, Style and "Hide release
// year" all apply the moment they change, the same way a switch elsewhere in
// this admin UI does.
//
// [data-shelf-source] is the Content picker specifically, because one of its
// entries isn't a real source: a library's "Collection Items…" is a
// placeholder that reveals a second picker of that library's own collections
// (fetched live) rather than applying anything by itself. Every other value
// in it, and every [data-auto-apply] field, submits its form immediately via
// `requestSubmit()` — a real submit event, which is what lets htmx's own
// hx-post binding on that form (not on the field) pick it up and gather every
// field correctly, the same as clicking a Save button would.
//
// [data-collection-source] is that second picker. It applies immediately too,
// but it is not part of either shelf form — it has no Style or "Hide release
// year" of its own to carry along, and changing the source always resets
// those server-side regardless (see libraries.update_shelf) — so it posts
// directly via htmx.ajax with just the two fields that matter: which
// collection, and its title, read off the option's own text since Plex
// collection ids are not scoped to a library and the server has no other way
// to learn it.
(function () {
  var COLLECTIONS_PREFIX = "collections:";

  document.addEventListener("change", function (event) {
    var field = event.target;

    if (field.matches("[data-shelf-source]")) {
      if (field.value.indexOf(COLLECTIONS_PREFIX) === 0) {
        var libraryId = field.value.slice(COLLECTIONS_PREFIX.length);
        var picker = document.getElementById("collections-picker-" + field.dataset.shelfId);
        if (picker) {
          htmx.ajax(
            "GET",
            "/admin/libraries/home/shelves/" + field.dataset.shelfId +
              "/collections?library_id=" + encodeURIComponent(libraryId),
            { target: picker, swap: "innerHTML" }
          );
        }
        return;
      }
      field.form.requestSubmit();
      return;
    }

    if (field.matches("[data-auto-apply]")) {
      field.form.requestSubmit();
      return;
    }

    if (field.matches("[data-collection-source]")) {
      var option = field.selectedOptions[0];
      if (!option || !option.value) return;
      htmx.ajax("POST", field.dataset.applyUrl, {
        target: document.getElementById("home-shelves"),
        swap: "outerHTML",
        values: { source: option.value, collection_title: option.textContent.trim() },
      });
    }
  });
})();
