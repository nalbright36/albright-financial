/* The floating dock (templates/index.html, .fab-dock in style.css): every floating button - a page's own (the
   {% block fab_items %}, e.g. Meals' Convert) and the AI assistant's, always last - sits in one fixed row at the
   bottom right, so they can never overlap. This lifts the dock (and the panels that open from it) above any fixed bar
   at the bottom of the screen - Save/Cancel bars, the workout bar, a bottom sheet - by setting --dock-lift on <html>.
   A bar opts in with data-dock-avoid if it isn't one of the known ones below. */
(function () {
    "use strict";
    var dock = document.getElementById("fab-dock");
    if (!dock || !dock.firstElementChild) return;
    var AVOID = ".fx-bar, .fx-setup__bar, .mn-actions--sticky, .wi-panel.is-open, [data-dock-avoid], [data-assistant-avoid]";
    var queued = false;
    function lift() {
        queued = false;
        var vh = window.innerHeight, need = 0, dockLeft = dock.getBoundingClientRect().left;
        Array.prototype.forEach.call(document.querySelectorAll(AVOID), function (el) {
            if (dock.contains(el) || !el.offsetParent && getComputedStyle(el).position !== "fixed") return;
            var r = el.getBoundingClientRect();
            if (r.height === 0 || r.width === 0 || r.top >= vh || r.bottom < vh - 4) return;   // not at the bottom
            if (r.right < dockLeft) return;                                                    // not under the dock
            need = Math.max(need, vh - r.top + 4);
        });
        document.documentElement.style.setProperty("--dock-lift", Math.round(need) + "px");
    }
    function queue() { if (!queued) { queued = true; window.requestAnimationFrame(lift); } }
    window.addEventListener("resize", queue);
    window.addEventListener("scroll", queue, { passive: true });
    if ("MutationObserver" in window) {
        new MutationObserver(queue).observe(document.body, { childList: true, subtree: true, attributes: true,
                                                             attributeFilter: ["class", "hidden", "style"] });
    }
    lift();
})();
