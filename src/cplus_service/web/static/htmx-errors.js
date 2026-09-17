// What a rejected write looks like now that nothing reloads.
//
// Every write in this admin UI posts via htmx and swaps a fresh fragment over
// what it replaced. htmx deliberately does not swap a 4xx/5xx response, which
// is right — a validation error must not be painted in as if it were the saved
// state — but on its own it means a rejected save simply does nothing visible.
// Before the writes landed in place, the browser at least navigated to
// FastAPI's error body; that page is exactly what we no longer want, so the
// reason has to come back some other way.
//
// So: one toast, bottom of the viewport, fed from the handler's own
// HTTPException detail. That detail is written to be read by an admin ("An
// action named 'Grab in 4K' already exists.") rather than by a client, so it
// is the whole message and needs no rewording here.
//
// Delegated on document, so it covers every page and every fragment swapped
// into one, with nothing per-form to remember to wire up.
(function () {
  var TIMEOUT_MS = 8000;
  var timer = null;

  function toast() {
    var existing = document.getElementById("htmx-error-toast");
    if (existing) return existing;

    var node = document.createElement("div");
    node.id = "htmx-error-toast";
    node.className = "error-toast";
    node.setAttribute("role", "status");
    // polite, not assertive: this interrupts nothing the admin is doing, it
    // reports on something they just asked for and are already looking at.
    node.setAttribute("aria-live", "polite");
    document.body.appendChild(node);
    return node;
  }

  // FastAPI answers an HTTPException as {"detail": "..."}; anything else that
  // goes wrong (a proxy's own error page, a truncated body) falls back to the
  // status line, which is at least true.
  function reasonFrom(xhr) {
    try {
      var body = JSON.parse(xhr.responseText);
      if (body && typeof body.detail === "string") return body.detail;
    } catch (err) {
      /* not JSON; fall through */
    }
    if (xhr.status === 0) return "The server could not be reached.";
    return "That did not save (" + xhr.status + " " + (xhr.statusText || "error") + ").";
  }

  function show(message) {
    var node = toast();
    node.textContent = message;
    node.classList.add("on");
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () {
      node.classList.remove("on");
    }, TIMEOUT_MS);
  }

  document.addEventListener("htmx:responseError", function (event) {
    show(reasonFrom(event.detail.xhr));
  });

  // No response at all — the box went away, or the browser is offline.
  document.addEventListener("htmx:sendError", function () {
    show("The server could not be reached. Nothing was saved.");
  });

  // Dismiss on click: eight seconds is right for reading one sentence and
  // wrong for anyone who has already read it.
  document.addEventListener("click", function (event) {
    if (event.target.id === "htmx-error-toast") {
      event.target.classList.remove("on");
    }
  });
})();
