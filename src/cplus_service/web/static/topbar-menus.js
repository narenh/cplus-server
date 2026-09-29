// The topbar's two <details> dropdowns — the mobile nav pill and the user
// menu — close each other when one opens, so there is never more than one
// popover fighting for the same space at once.
(function () {
  document.addEventListener(
    "toggle",
    function (event) {
      var opened = event.target;
      if (!opened.matches(".nav-dropdown, .user-menu") || !opened.open) {
        return;
      }
      document.querySelectorAll(".nav-dropdown, .user-menu").forEach(function (other) {
        if (other !== opened) {
          other.open = false;
        }
      });
    },
    true, // "toggle" does not bubble; capture it on the way down instead.
  );
})();

// An open "Add library"-style menu closes on a click anywhere outside it, and
// on Escape, the way a menu does — a <details> on its own only closes from its
// own summary.
(function () {
  function closeAll(except) {
    document.querySelectorAll(".add-menu[open]").forEach(function (menu) {
      if (menu !== except) {
        menu.open = false;
      }
    });
  }
  document.addEventListener("click", function (event) {
    closeAll(event.target.closest(".add-menu"));
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeAll(null);
    }
  });
})();
