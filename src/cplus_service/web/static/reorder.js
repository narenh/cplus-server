// Vanilla drag-and-drop list reordering. No library — this install ships as a
// container image with no outbound access assumed, same reasoning as vendoring
// htmx.min.js instead of pulling it from a CDN.
//
// A container needs [data-reorder-list] and its direct children need
// [draggable=true] and [data-reorder-id]. Dragging live-reorders the DOM;
// on drop, the new order is read back from the DOM and sent one of two ways:
//
// * [data-reorder-url] + [data-reorder-target] — an in-place write with no
//   page reload: the order posts via htmx, and the response (expected to be
//   the whole reorderable section, id and all) replaces the element matched
//   by [data-reorder-target].
// * [data-reorder-form="<id>"] — a plain <form method=post> already on the
//   page, pointed at the reorder endpoint. The order is written into it as
//   one hidden "order" input per id and the form is submitted for real: a
//   normal page reload that comes back with the new order persisted.
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

    var order = rowsOf(list).map(function (row) {
      return row.getAttribute("data-reorder-id");
    });

    var url = list.getAttribute("data-reorder-url");
    var targetSelector = list.getAttribute("data-reorder-target");
    var target = targetSelector && document.querySelector(targetSelector);
    if (url && target) {
      htmx.ajax("POST", url, { target: target, swap: "outerHTML", values: { order: order } });
      return;
    }

    var formId = list.getAttribute("data-reorder-form");
    var form = formId && document.getElementById(formId);
    if (!form) return;

    form.innerHTML = "";
    order.forEach(function (id) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "order";
      input.value = id;
      form.appendChild(input);
    });
    form.requestSubmit();
  });
})();
