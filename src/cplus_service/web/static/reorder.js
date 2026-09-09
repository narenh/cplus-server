// Vanilla drag-and-drop list reordering. No library — this install ships as a
// container image with no outbound access assumed, same reasoning as vendoring
// htmx.min.js instead of pulling it from a CDN.
//
// Usage: a container with [data-reorder-list] and [data-reorder-form="<id>"]
// (a <form method=post> already on the page, pointed at the reorder endpoint)
// holding direct children with [draggable=true] and [data-reorder-id]. On
// drop, the row order in the DOM is written into that form as one hidden
// "order" input per id and the form is submitted for real — a normal page
// reload that comes back with the new order persisted, the same way every
// other write on this page works.
(function () {
  function rowsOf(list) {
    return Array.from(list.querySelectorAll("[data-reorder-id]"));
  }

  document.addEventListener("dragstart", function (event) {
    var row = event.target.closest("[data-reorder-id]");
    if (!row) return;
    event.dataTransfer.effectAllowed = "move";
    // Required for Firefox to start the drag at all; the value itself is
    // unused — the new order is read back from the DOM on drop.
    event.dataTransfer.setData("text/plain", row.getAttribute("data-reorder-id"));
    row.classList.add("dragging");
  });

  document.addEventListener("dragend", function (event) {
    var row = event.target.closest("[data-reorder-id]");
    if (row) row.classList.remove("dragging");
  });

  document.addEventListener("dragover", function (event) {
    var list = event.target.closest("[data-reorder-list]");
    if (!list) return;
    event.preventDefault();

    var dragging = list.querySelector(".dragging");
    var over = event.target.closest("[data-reorder-id]");
    if (!dragging || !over || over === dragging) return;

    var rect = over.getBoundingClientRect();
    var before = event.clientY - rect.top < rect.height / 2;
    list.insertBefore(dragging, before ? over : over.nextSibling);
  });

  document.addEventListener("drop", function (event) {
    var list = event.target.closest("[data-reorder-list]");
    if (!list) return;
    event.preventDefault();

    var formId = list.getAttribute("data-reorder-form");
    var form = formId && document.getElementById(formId);
    if (!form) return;

    form.innerHTML = "";
    rowsOf(list).forEach(function (row) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "order";
      input.value = row.getAttribute("data-reorder-id");
      form.appendChild(input);
    });
    form.requestSubmit();
  });
})();
