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
 *   resume        the launcher's tiles and Home's app tiles open each app at the last page you visited in it - only
 *                 a page that really belongs to that app (albright_labs.resume_nav's rules, carried by the page):
 *                 checked when saved, when a tile uses it, and on every load (failures are deleted); a tile that
 *                 still lands in another app drops the entry and opens the app's main page.
 *
 * Only same-site URLs ("/path", never "//host" or another origin) are stored or followed. sessionStorage (this
 * tab): the trail of pages and the return chip; localStorage (kept per signed-in user, so someone else signing
 * in on this device never sees yours): the last page per app. Every read and write is wrapped -
 * private browsing just goes without. Nothing here touches touch events, so iOS's edge swipe back still works.
 */
(function () {
    "use strict";

    var root = document.documentElement;                           // <html data-app-key ... data-nav-user>
    var user = root.getAttribute("data-nav-user");
    if (!user) return;                                             // signed out
    var KEY = root.getAttribute("data-app-key") || "", LABEL = root.getAttribute("data-app-label") || "";
    var LAST = "nav_last:" + user;
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

    // ---------- Resume each app where you were ----------
    // What counts as an app's last page (albright_labs.resume_nav - the page carries the rules): on this site, in
    // that app (its URL prefix; Trading: no other app's), not a shared page (My account, Circles, Notifications,
    // Activity, sign in...), not an old page that only redirects, and an app this user can still open.
    var RULES = null;
    try { RULES = JSON.parse(document.getElementById("nav-resume-rules").textContent); } catch (e) { RULES = null; }
    function startsWithAny(path, prefixes) {
        for (var i = 0; i < (prefixes || []).length; i++) if (path.indexOf(prefixes[i]) === 0) return true;
        return false;
    }
    function valid(key, url) {                                      // resume_nav.belongs(), line for line
        if (!RULES || !sameSite(url)) return false;
        var app = RULES.apps && RULES.apps[key];
        if (!app) return false;                                     // Home, unknown, or no longer allowed
        var path = url.split("#")[0].split("?")[0];
        if (path === "/" || (RULES.retired || []).indexOf(path) >= 0) return false;
        if (startsWithAny(path, RULES.shared)) return false;
        if (app.prefixes && app.prefixes.length) return startsWithAny(path, app.prefixes);
        return !startsWithAny(path, RULES.claimed);                 // Trading: nobody else's page
    }
    function loadLast() {
        var last = read("localStorage", LAST, {});
        return (last && typeof last === "object" && !Array.isArray(last)) ? last : {};
    }
    function remember() {
        var last = loadLast();
        if (!RULES) return last;                                    // no rules on this page: change nothing
        Object.keys(last).forEach(function (key) {                  // tidy away anything that fails the check
            if (!valid(key, last[key])) delete last[key];
        });
        if (KEY && KEY !== "home" && valid(KEY, here())) last[KEY] = here();
        write("localStorage", LAST, last);
        write("localStorage", "nav_recent:" + user, null);         // the old Recent list (removed) - tidy it away
        return last;
    }
    function forget(key) {
        var last = loadLast();
        delete last[key];
        write("localStorage", LAST, last);
    }
    // A tile that followed a saved page and landed in another app (an old URL that now redirects): drop that entry
    // and open the app's main page instead. Only for the page that click led to - fresh, and arrived at from the
    // page the tile was on.
    var RESUMED_FRESH_MS = 60 * 1000;
    function referrerPath() {
        try {
            var ref = new URL(document.referrer);
            return ref.origin === window.location.origin ? ref.pathname + ref.search : "";
        } catch (e) { return ""; }
    }
    function checkResumed() {
        var resumed = read("sessionStorage", "nav_resumed", null);
        write("sessionStorage", "nav_resumed", null);
        if (!resumed || !resumed.key || resumed.key === KEY) return false;
        if (Date.now() - (resumed.at || 0) > RESUMED_FRESH_MS || referrerPath() !== resumed.from) return false;
        forget(resumed.key);
        var main = RULES && RULES.apps && RULES.apps[resumed.key] && RULES.apps[resumed.key].main;
        if (sameSite(main)) { window.location.replace(main); return true; }
        return false;
    }
    document.addEventListener("click", function (event) {
        var link = event.target.closest && event.target.closest("a[data-resume-key]");
        if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        write("sessionStorage", "nav_resumed", {key: link.getAttribute("data-resume-key"), from: here(), at: Date.now()});
    }, true);
    function resumeLinks(last) {
        // The launcher's tiles and Home's tiles: each app at its last page - not the app you're in (its tile
        // goes to its main page) and not Home (one page). A saved page that fails the check is deleted, and the
        // tile keeps the app's main page.
        function point(link, key) {
            var url = last[key];
            if (url === undefined) return;
            if (key && key !== KEY && key !== "home" && sameSite(url) && valid(key, url)) {
                link.setAttribute("href", url);
                link.setAttribute("data-resume-key", key);
            } else if (RULES && !valid(key, url)) {
                forget(key);
            }
        }
        document.querySelectorAll(".app-launcher__tile[data-app-key]").forEach(function (tile) {
            point(tile, tile.getAttribute("data-app-key"));
        });
        document.querySelectorAll(".labs-pick[data-key] [data-pick-link]").forEach(function (link) {
            point(link, link.closest(".labs-pick").getAttribute("data-key"));
        });
    }
    function run() {
        if (checkResumed()) return;                                 // on the way to the app's main page
        visit();
        showBack(returnTo());
        resumeLinks(remember());
    }
    run();
    // Back / Forward from the back-forward cache: the page wasn't reloaded, so bring it up to date.
    window.addEventListener("pageshow", function (event) { if (event.persisted) run(); });
})();
