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
 *   resume        the launcher's tiles and Home's app tiles open each app at the last page you visited in it;
 *                 the launcher's "Recent" row lists your last 4 pages across apps (app icon + page name).
 *
 * Only same-site URLs ("/path", never "//host" or another origin) are stored or followed. sessionStorage (this
 * tab): the trail of pages and the return chip; localStorage (kept per signed-in user, so someone else signing
 * in on this device never sees yours): the last page per app and Recent. Every read and write is wrapped -
 * private browsing just goes without. Nothing here touches touch events, so iOS's edge swipe back still works.
 */
(function () {
    "use strict";

    var root = document.documentElement;                           // <html data-app-key ... data-nav-user>
    var user = root.getAttribute("data-nav-user");
    if (!user) return;                                             // signed out
    var KEY = root.getAttribute("data-app-key") || "", LABEL = root.getAttribute("data-app-label") || "";
    var LAST = "nav_last:" + user, RECENT = "nav_recent:" + user;
    var RECENT_KEEP = 12, RECENT_SHOW = 4, FROM_FRESH_MS = 10 * 60 * 1000;

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
    // "Monday, November 2 · Albright Planner" -> "Monday, November 2" ("Camping trip · Notes · Albright Notes" ->
    // "Camping trip"); a page with no title of its own -> the app.
    function pageName() {
        var title = document.title || "", cut = title.lastIndexOf(" · ");
        var name = cut > 0 ? title.slice(0, cut) : "", suffix = " · " + LABEL;
        if (name.slice(-suffix.length) === suffix) name = name.slice(0, -suffix.length);
        return name || LABEL;
    }

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

    // ---------- Resume each app where you were, and Recent ----------
    function remember() {
        var url = here(), last = read("localStorage", LAST, {}), recent = read("localStorage", RECENT, []);
        if (typeof last !== "object" || Array.isArray(last)) last = {};
        if (!Array.isArray(recent)) recent = [];
        if (KEY) last[KEY] = url;
        recent = [{url: url, key: KEY, name: pageName()}].concat(recent.filter(function (r) {
            return r && r.url !== url && sameSite(r.url);
        })).slice(0, RECENT_KEEP);
        write("localStorage", LAST, last);
        write("localStorage", RECENT, recent);
        return {last: last, recent: recent};
    }
    function resumeLinks(last) {
        // The launcher's tiles and Home's tiles: each app at its last page - not the app you're in (its tile
        // goes to its main page) and not Home (one page).
        function point(link, key) {
            var url = last[key];
            if (key && key !== KEY && key !== "home" && sameSite(url)) link.setAttribute("href", url);
        }
        document.querySelectorAll(".app-launcher__tile[data-app-key]").forEach(function (tile) {
            point(tile, tile.getAttribute("data-app-key"));
        });
        document.querySelectorAll(".labs-pick[data-key] [data-pick-link]").forEach(function (link) {
            point(link, link.closest(".labs-pick").getAttribute("data-key"));
        });
    }
    function showRecent(recent) {
        var box = document.getElementById("app-launcher-recent"), list = document.getElementById("app-launcher-recent-list");
        if (!box || !list) return;
        var url = here(), shown = [];
        recent.forEach(function (r) {
            if (shown.length >= RECENT_SHOW || r.url === url || !sameSite(r.url)) return;
            var tile = document.querySelector('.app-launcher__tile[data-app-key="' + String(r.key).replace(/[^a-z0-9_-]/gi, "") + '"]');
            if (tile) shown.push({entry: r, tile: tile});           // only apps you can open (they have a tile)
        });
        list.textContent = "";
        shown.forEach(function (s) {
            var link = document.createElement("a");
            link.className = "app-launcher__recent-item";
            link.href = s.entry.url;
            link.setAttribute("role", "menuitem");
            link.setAttribute("tabindex", "-1");
            link.setAttribute("data-no-return", "");
            link.setAttribute("data-app-target", s.tile.getAttribute("data-app-target") || "");
            var icon = s.tile.querySelector(".app-launcher__icon");
            if (icon) link.appendChild(icon.cloneNode(true));
            var name = document.createElement("span");
            name.className = "app-launcher__recent-name";
            name.textContent = String(s.entry.name || "");
            link.title = name.textContent;
            link.appendChild(name);
            list.appendChild(link);
        });
        box.hidden = !shown.length;
    }

    function run() {
        visit();
        showBack(returnTo());
        var memory = remember();
        resumeLinks(memory.last);
        showRecent(memory.recent);
    }
    run();
    // Back / Forward from the back-forward cache: the page wasn't reloaded, so bring it up to date.
    window.addEventListener("pageshow", function (event) { if (event.persisted) run(); });
})();
