/* The briefing's schedule sheet (roundup/_sheet.html): [data-rb-open] opens it as a modal, [data-rb-close], Esc or a tap
   on the backdrop closes it. Without JS, ?schedule=1 renders it open. */
(function () {
    "use strict";
    var sheet = document.getElementById("rb-sheet");
    if (!sheet || typeof sheet.showModal !== "function") return;
    var opener = null;
    document.addEventListener("click", function (e) {
        var open = e.target.closest("[data-rb-open]");
        if (open) {
            e.preventDefault();
            opener = open;
            var menu = open.closest("details");
            if (menu) menu.open = false;
            if (!sheet.open) sheet.showModal();
            return;
        }
        if (e.target.closest("[data-rb-close]") || e.target === sheet) {
            e.preventDefault();
            sheet.close();
        }
    });
    sheet.addEventListener("close", function () { if (opener) opener.focus(); });
    if (sheet.hasAttribute("open")) { sheet.removeAttribute("open"); sheet.showModal(); }
})();
