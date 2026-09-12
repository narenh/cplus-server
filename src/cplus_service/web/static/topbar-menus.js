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
