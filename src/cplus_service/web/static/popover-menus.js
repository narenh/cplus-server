// Popover menus (<div class="popover-menu" popover>) opened by a
// popovertarget button. A popover sits in the top layer, so no scrolling
// container clips it — but the browser centres it on the screen, so this
// places it under its button, right-aligned, flipping above when there's no
// room below. It is fixed to the viewport, so any scroll or resize closes it
// rather than leaving it behind its button.
//
// Also: a file input with data-confirm asks that question before its picker
// opens, and picking a file closes the menu it was picked from.
(function () {
  var GAP = 4;
  var EDGE = 8;

  function invoker(menu) {
    return document.querySelector('[popovertarget="' + menu.id + '"]');
  }

  function closeAll() {
    document.querySelectorAll(".popover-menu:popover-open").forEach(function (menu) {
      menu.hidePopover();
    });
  }

  document.addEventListener(
    "beforetoggle",
    function (event) {
      var menu = event.target;
      if (!menu.matches(".popover-menu") || event.newState !== "open") {
        return;
      }
      var button = invoker(menu);
      if (!button) {
        return;
      }
      var r = button.getBoundingClientRect();
      menu.style.top = r.bottom + GAP + "px";
      menu.style.bottom = "auto";
      menu.style.right = Math.max(EDGE, window.innerWidth - r.right) + "px";
      menu.style.left = "auto";
    },
    true, // neither toggle event bubbles; capture them on the way down.
  );

  document.addEventListener(
    "toggle",
    function (event) {
      var menu = event.target;
      if (!menu.matches(".popover-menu") || event.newState !== "open") {
        return;
      }
      var button = invoker(menu);
      var m = menu.getBoundingClientRect();
      if (button && m.bottom > window.innerHeight - EDGE) {
        var r = button.getBoundingClientRect();
        if (r.top - GAP - m.height > EDGE) {
          menu.style.top = r.top - GAP - m.height + "px";
        }
      }
    },
    true,
  );

  window.addEventListener("scroll", closeAll, true);
  window.addEventListener("resize", closeAll);

  document.addEventListener(
    "click",
    function (event) {
      var input = event.target;
      if (input.matches && input.matches('input[type="file"][data-confirm]')) {
        if (!window.confirm(input.dataset.confirm)) {
          event.preventDefault();
        }
      }
    },
    true,
  );

  document.addEventListener("change", function (event) {
    var menu = event.target.closest && event.target.closest(".popover-menu");
    if (menu && menu.matches(":popover-open")) {
      menu.hidePopover();
    }
  });
})();
