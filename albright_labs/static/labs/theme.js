/*
 * Public pages (landing, resume): apply the theme chosen with the site's
 * toggle (localStorage "theme") before first paint. Loaded as a blocking
 * <script src> in <head> - an external file, so the strict CSP
 * (script-src 'self') holds. No choice saved: the CSS follows the system.
 */
(function () {
    "use strict";
    var stored = null;
    try { stored = window.localStorage.getItem("theme"); } catch (e) {}
    if (stored === "light" || stored === "dark") document.documentElement.setAttribute("data-theme", stored);
})();
