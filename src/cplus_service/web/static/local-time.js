// Rewrites every <time class="local-time" datetime="…"> from the UTC
// string the server rendered it with to the viewer's own local timezone.
// See format_when() in cplus_service/web/__init__.py: the server only knows
// UTC, so it renders that plus the instant in the datetime attribute, and
// this is what turns it into "whatever time it is where you are".
(function () {
  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function formatLocal(date) {
    var tzPart = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" })
      .formatToParts(date)
      .find(function (part) {
        return part.type === "timeZoneName";
      });
    var stamp =
      date.getFullYear() +
      "-" +
      pad(date.getMonth() + 1) +
      "-" +
      pad(date.getDate()) +
      " " +
      pad(date.getHours()) +
      ":" +
      pad(date.getMinutes());
    return tzPart ? stamp + " " + tzPart.value : stamp;
  }

  function localize(root) {
    (root || document).querySelectorAll("time.local-time[datetime]").forEach(function (el) {
      var date = new Date(el.getAttribute("datetime"));
      if (!isNaN(date.getTime())) {
        el.textContent = formatLocal(date);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      localize();
    });
  } else {
    localize();
  }
})();
