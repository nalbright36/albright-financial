/*
 * Getting around - above all in the iPhone home screen app, which has no browser Back / Forward. On every page
 * (templates/index.html), for someone signed in. Vanilla JS.
 *
 *   back          ONE back control, a chip in its own row under the header (index.html, style.css):
 *                 - "← Back to Planner" after following a link from one app into another ("Open in Notes" from
 *                   the Planner): back to the exact page you came from, for as long as you stay in that app; x
 *                   dismisses it. Not for the launcher, the logo, Home's tiles or the back chip itself.
 *                 - otherwise "← Back" (phones and the home screen app - style.css): back when the previous page
 *                   was on this site, else this app's main page (its href); hidden on one of the app's main pages
 *                   when there's nothing of this site to go back to.
 *   landing       app links (the launcher, Home's tiles, the header chip) always open the app's home page - the
 *                 server renders them from the app registry and nothing here rewrites them. The old "resume where
 *                 you left off" entries (localStorage nav_last:* / nav_recent:*, sessionStorage nav_resumed) are
 *                 deleted on load, so an old value can never send anyone to a deleted item.
 *
 * Only same-site URLs ("/path", never "//host" or another origin) are stored or followed. sessionStorage (this
 * tab): the trail of pages and the return chip. Every read and write is wrapped -
 * private browsing just goes without. Nothing here touches touch events, so iOS's edge swipe back still works.
 */
(function () {
    "use strict";

    var root = document.documentElement;                           // <html data-app-key ... data-nav-user>
    var user = root.getAttribute("data-nav-user");
    if (!user) return;                                             // signed out
    var KEY = root.getAttribute("data-app-key") || "", LABEL = root.getAttribute("data-app-label") || "";
    var FROM_FRESH_MS = 10 * 60 * 1000;

    function sameSite(url) {
        if (typeof url !== "string" || url.charAt(0) !== "/" || url.charAt(1) === "/" || url.charAt(1) === "\\") return false;
        try { return new URL(url, window.location.origin).origin === window.location.origin; } catch (e) { return false; }
    }
    function read(store, name, fallback) {
        try {
            var value = JSON.parse(window[store].getItem(name));
            return value == null ? fallback : value;
        } catch (e) { return fallback; }
    }
    function write(store, name, value) {
        try {
            if (value == null) window[store].removeItem(name);
            else window[store].setItem(name, JSON.stringify(value));
        } catch (e) { /* storage off or full */ }
    }
    function here() { return window.location.pathname + window.location.search; }

    // ---------- This tab's trail, and Back ----------
    var hasPrevious = false, backHidden = false;
    function visit() {
        var trail = read("sessionStorage", "nav_trail", []).filter(sameSite), url = here();
        if (trail[trail.length - 1] === url) { /* a reload */ }
        else if (trail.length > 1 && trail[trail.length - 2] === url) trail.pop();      // came back
        else trail.push(url);
        trail = trail.slice(-50);
        write("sessionStorage", "nav_trail", trail);
        var referrer = "";
        try {
            var ref = new URL(document.referrer);
            if (ref.origin === window.location.origin) referrer = ref.pathname + ref.search;
        } catch (e) { /* none */ }
        hasPrevious = window.history.length > 1 && (trail.length > 1 || !!referrer);
        var back = document.getElementById("nav-back");
        var mains = back ? (back.getAttribute("data-main-paths") || "").split(" ") : [];
        backHidden = mains.indexOf(window.location.pathname) >= 0 && !hasPrevious;
    }

    // ---------- The return chip ----------
    // A same-site link followed from here remembers this page (capture: before a page script handles the click).
    // The launcher, the logo (.navbar__brand), Home's tiles, Back and the chip (data-no-return) forget it instead.
    document.addEventListener("click", function (event) {
        var link = event.target.closest && event.target.closest("a[href]");
        if (link && link.closest("[data-no-return], .navbar__brand")) { write("sessionStorage", "nav_from", null); return; }
        if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
        if (link.origin !== window.location.origin) return;
        if (link.pathname === window.location.pathname && link.search === window.location.search) return;   // same page
        write("sessionStorage", "nav_from", {url: here() + window.location.hash, key: KEY, label: LABEL, at: Date.now()});
    }, true);

    function returnTo() {
        var from = read("sessionStorage", "nav_from", null);
        write("sessionStorage", "nav_from", null);
        var chip = read("sessionStorage", "nav_return", null);
        if (from && from.key && from.key !== KEY && sameSite(from.url) && Date.now() - (from.at || 0) < FROM_FRESH_MS) {
            chip = {key: KEY, url: from.url, label: String(from.label || "")};           // arrived from another app
            write("sessionStorage", "nav_return", chip);
        }
        if (chip && (chip.key !== KEY || !sameSite(chip.url))) { chip = null; write("sessionStorage", "nav_return", null); }
        return chip;
    }

    // ---------- The back chip: "← Back to <app>" or "← Back" - never both ----------
    var row = document.getElementById("back-row"), backLink = document.getElementById("nav-back");
    var backLabel = document.getElementById("nav-back-label"), backClose = document.getElementById("nav-back-close");
    var mainPage = backLink ? backLink.getAttribute("href") : "/";
    function showBack(chip) {
        if (!row || !backLink) return;
        if (chip) {
            row.setAttribute("data-mode", "return");
            backLink.setAttribute("href", chip.url);
            backLabel.textContent = "Back to " + chip.label;
            row.hidden = false;
        } else {
            row.setAttribute("data-mode", "back");
            backLink.setAttribute("href", mainPage);
            backLabel.textContent = "Back";
            row.hidden = backHidden;
        }
        if (backClose) backClose.hidden = !chip;
    }
    if (backLink) {
        backLink.addEventListener("click", function (event) {
            if (row.getAttribute("data-mode") === "return") { write("sessionStorage", "nav_return", null); return; }
            if (!hasPrevious) return;                               // the app's main page (the link itself)
            event.preventDefault();
            window.history.back();
        });
    }
    if (backClose) backClose.addEventListener("click", function () {
        write("sessionStorage", "nav_return", null);
        showBack(null);                                             // plain "← Back" again
    });

    // ---------- Clean up the old "resume where you left off" keys (one-time; harmless every load) ----------
    function forgetOldResume() {
        try {
            for (var i = window.localStorage.length - 1; i >= 0; i--) {
                var name = window.localStorage.key(i);
                if (name && (name.indexOf("nav_last") === 0 || name.indexOf("nav_recent") === 0)) window.localStorage.removeItem(name);
            }
        } catch (e) { /* storage off */ }
        write("sessionStorage", "nav_resumed", null);
    }

    function run() {
        visit();
        showBack(returnTo());
    }
    forgetOldResume();
    run();
    // Back / Forward from the back-forward cache: the page wasn't reloaded, so bring it up to date.
    window.addEventListener("pageshow", function (event) { if (event.persisted) run(); });
})();
