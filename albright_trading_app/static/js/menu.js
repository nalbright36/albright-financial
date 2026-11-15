/*
 * "…" menus, site-wide (templates/index.html loads this on every page). Vanilla JS.
 *
 *   <details class="..." data-menu [data-menu-scope="pl"]>
 *       <summary aria-label="...">&hellip;</summary>
 *       <div class="..." data-menu-panel> buttons / links / small forms </div>
 *   </details>
 *
 * Without JS it's a plain <details>. With JS, while it's open the panel moves to a layer at the end of <body> (or of
 * the open <dialog> it's in, so it stays above the dialog) and is placed with position: fixed next to its button - so
 * no card can clip it and no later card can cover it (a card's backdrop-filter traps position: fixed and z-index
 * inside it otherwise). data-menu-scope puts those classes on the layer, so scoped styles (".pl .pl-menu__item")
 * still apply there.
 *   - Opens below the button, right edges lined up; above it when there's no room below; never off screen.
 *   - Closes on Esc (focus back on the button), a click outside, choosing an option, or Tab; scrolling and resizing
 *     move it with its button.
 *   - ArrowDown/ArrowUp/Home/End move between the options (ArrowDown on the button opens it at the first).
 *   - aria-expanded / aria-controls on the button; a panel that's only a list of actions is role="menu" with
 *     role="menuitem" options (aria-haspopup="menu"); one with form fields (the Planner's Move, Meals' Move) isn't.
 */
(function () {
    "use strict";

    var GAP = 4, EDGE = 8, current = null, uid = 0;

    function summaryOf(details) { return details.querySelector(":scope > summary"); }
    function panelOf(details) { return details._menuPanel || details.querySelector(":scope > [data-menu-panel]"); }
    function visible(el) { return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length); }
    function options(panel) {
        return Array.prototype.filter.call(
            panel.querySelectorAll("button:not([disabled]), a[href], summary, [role='menuitem']"), visible);
    }
    function isPlainMenu(panel) {
        return !panel.querySelector("input:not([type='hidden']), select, textarea");
    }

    function setup(details) {
        if (details._menuReady) return;
        details._menuReady = true;
        var summary = summaryOf(details), panel = panelOf(details);
        if (!summary || !panel) return;
        details._menuPanel = panel;
        if (!panel.id) panel.id = "menu-panel-" + (++uid);
        summary.setAttribute("aria-controls", panel.id);
        summary.setAttribute("aria-expanded", details.open ? "true" : "false");
        if (isPlainMenu(panel)) {
            summary.setAttribute("aria-haspopup", "menu");
            panel.setAttribute("role", "menu");
            if (summary.getAttribute("aria-label")) panel.setAttribute("aria-label", summary.getAttribute("aria-label"));
            options(panel).forEach(function (el) { el.setAttribute("role", "menuitem"); });
        }
        // Anyone else closing it (details.open = false) closes it properly - the panel goes back home.
        details.addEventListener("toggle", function () {
            if (!details.open && current && current.details === details) close(false);
        });
    }

    function place() {
        if (!current) return;
        var panel = current.panel, r = current.summary.getBoundingClientRect();
        var vw = document.documentElement.clientWidth, vh = window.innerHeight;
        panel.style.maxHeight = (vh - 2 * EDGE) + "px";
        var pw = panel.offsetWidth, ph = panel.offsetHeight;
        var left = Math.min(Math.max(EDGE, r.right - pw), vw - pw - EDGE);   // right edges lined up, kept on screen
        var top = r.bottom + GAP;
        if (top + ph > vh - EDGE) {
            if (r.top - GAP - ph >= EDGE) top = r.top - GAP - ph;            // no room below: open above
            else top = Math.max(EDGE, vh - EDGE - ph);                        // no room either way: as much as fits
        }
        panel.style.left = Math.round(left) + "px";
        panel.style.top = Math.round(top) + "px";
        // A dialog (or anything) with a transform would offset position: fixed - measure and correct.
        var got = panel.getBoundingClientRect();
        if (Math.abs(got.left - left) > 0.5 || Math.abs(got.top - top) > 0.5) {
            panel.style.left = Math.round(2 * left - got.left) + "px";
            panel.style.top = Math.round(2 * top - got.top) + "px";
        }
    }

    function open(details, focusFirst) {
        if (current && current.details === details) return;
        if (current) close(false);
        setup(details);
        var summary = summaryOf(details), panel = panelOf(details);
        if (!summary || !panel) return;
        var home = document.createComment("menu");
        panel.parentNode.insertBefore(home, panel);
        var layer = document.createElement("div");
        layer.className = "menu-layer " + (details.getAttribute("data-menu-scope") || "");
        layer.setAttribute("style", "position: fixed; top: 0; left: 0; width: 0; height: 0; margin: 0; padding: 0; z-index: 1000;");
        (summary.closest("dialog[open]") || document.body).appendChild(layer);
        layer.appendChild(panel);
        panel.classList.add("is-floating");
        current = {details: details, summary: summary, panel: panel, home: home, layer: layer};
        details.open = true;
        summary.setAttribute("aria-expanded", "true");
        place();
        if (focusFirst) { var first = options(panel)[0]; if (first) first.focus(); }
    }

    function close(refocus) {
        if (!current) return;
        var c = current;
        current = null;
        c.panel.classList.remove("is-floating");
        ["left", "top", "maxHeight"].forEach(function (p) { c.panel.style[p] = ""; });
        if (c.home.parentNode) { c.home.parentNode.insertBefore(c.panel, c.home); c.home.remove(); }
        c.layer.remove();
        c.summary.setAttribute("aria-expanded", "false");
        if (c.details.open) c.details.open = false;
        if (refocus) c.summary.focus();
    }

    // The button: open/close it ourselves (the panel isn't inside the <details> while open).
    document.addEventListener("click", function (event) {
        var summary = event.target.closest && event.target.closest("details[data-menu] > summary");
        if (summary) {
            event.preventDefault();
            var details = summary.parentNode;
            if (current && current.details === details) close(false); else open(details, event.detail === 0);
            return;
        }
        if (!current) return;
        if (!current.panel.contains(event.target)) { close(false); return; }        // a click outside
        var choice = event.target.closest("button, a[href]");
        if (choice && current.panel.contains(choice) && choice.tagName !== "SUMMARY") {
            window.setTimeout(function () { close(false); }, 0);                     // after its own click/submit
        }
    }, true);

    document.addEventListener("keydown", function (event) {
        var summary = event.target.closest && event.target.closest("details[data-menu] > summary");
        if (summary && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
            event.preventDefault();
            open(summary.parentNode, true);
            if (event.key === "ArrowUp" && current) { var all = options(current.panel); if (all.length) all[all.length - 1].focus(); }
            return;
        }
        if (!current) return;
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(true); return; }
        if (!current.panel.contains(event.target)) return;
        if (event.key === "Tab") { close(true); return; }                              // Tab moves on from the button
        if (["ArrowDown", "ArrowUp", "Home", "End"].indexOf(event.key) < 0) return;
        if (/^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName)) return;            // the Move form's fields
        var items = options(current.panel);
        if (!items.length) return;
        event.preventDefault();
        var at = items.indexOf(document.activeElement), next;
        if (event.key === "Home") next = 0;
        else if (event.key === "End") next = items.length - 1;
        else if (event.key === "ArrowDown") next = at < 0 ? 0 : (at + 1) % items.length;
        else next = at < 0 ? items.length - 1 : (at - 1 + items.length) % items.length;
        items[next].focus();
    }, true);

    // Scrolling and resizing place it again, next to its button (not closed: on a phone, typing in the Planner's or
    // Meals' Move field opens the keyboard, which resizes and scrolls the page); so does a part of it opening or
    // closing (the Planner's "Move…").
    var pending = false;
    function replace() {
        if (!current || pending) return;
        pending = true;
        window.requestAnimationFrame(function () { pending = false; place(); });
    }
    window.addEventListener("scroll", function (event) {
        if (current && !(event.target instanceof Node && current.panel.contains(event.target))) replace();
    }, true);
    window.addEventListener("resize", replace);
    window.addEventListener("pagehide", function () { close(false); });
    document.addEventListener("toggle", function (event) {
        if (current && current.panel.contains(event.target)) place();
    }, true);
    // A dialog closing takes its open menu with it.
    document.addEventListener("close", function (event) {
        if (current && event.target.contains && event.target.contains(current.layer)) close(false);
    }, true);

    function init(root) {
        (root || document).querySelectorAll("details[data-menu]").forEach(setup);
    }
    init();
    // Menus added later (the Planner's view panel, loaded into its dialog) get set up as they arrive.
    if (window.MutationObserver) {
        new MutationObserver(function (records) {
            records.forEach(function (record) {
                record.addedNodes.forEach(function (node) {
                    if (node.nodeType !== 1) return;
                    if (node.matches("details[data-menu]")) setup(node);
                    if (node.querySelectorAll) init(node);
                });
            });
        }).observe(document.body, {childList: true, subtree: true});
    }
    window.AlbrightMenus = {init: init, close: function () { close(false); }};
})();
