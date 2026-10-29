// The header's app launcher (templates/index.html, labs/launcher.css) - the
// behavior of "Option B: App grid launcher" in docs/design/app_switcher_options.html:
// the button toggles the panel and focuses its first tile; ArrowRight/ArrowDown
// and ArrowLeft/ArrowUp move between tiles (Home/End: first/last); Escape, a click
// outside, Tab moving focus out, or choosing an app closes it, and Escape puts
// focus back on the button. Only one header menu is open at a time (the inline
// script's navDropdowns). Each tile's live line is fetched once, after the panel
// first opens, from data-lines-url (albright_labs.views.launcher_lines).
(function () {
    var launcher = document.getElementById('app-launcher');
    if (!launcher) return;
    var button = document.getElementById('app-launcher-button');
    var menu = document.getElementById('app-launcher-panel');
    var loaded = false;

    function items() { return Array.prototype.slice.call(menu.querySelectorAll('[role="menuitem"]')); }
    function isOpen() { return launcher.classList.contains('open'); }

    function close(refocus) {
        if (!isOpen()) return;
        launcher.classList.remove('open');
        menu.hidden = true;
        button.setAttribute('aria-expanded', 'false');
        if (refocus) button.focus();
    }

    function loadLines() {
        if (loaded || !launcher.dataset.linesUrl) return;
        loaded = true;
        fetch(launcher.dataset.linesUrl, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
            .then(function (r) { return r.ok ? r.json() : { lines: {} }; })
            .then(function (data) {
                var lines = (data && data.lines) || {};
                menu.querySelectorAll('[data-line-for]').forEach(function (el) {
                    var text = lines[el.getAttribute('data-line-for')];
                    if (typeof text === 'string') { el.textContent = text; el.title = text; }
                    else if (el.getAttribute('data-line-for') !== 'home') el.textContent = '';
                });
            })
            .catch(function () { loaded = false; });
    }

    function open() {
        if (window.navDropdowns) window.navDropdowns.forEach(function (closeOther) { closeOther(); });
        var nav = launcher.closest('.navbar'), toggle = document.getElementById('nav-toggle');
        if (nav && toggle && nav.classList.contains('navbar--menu-open')) toggle.click();   // the phone Menu
        launcher.classList.add('open');
        menu.hidden = false;
        button.setAttribute('aria-expanded', 'true');
        var list = items(), current = menu.querySelector('.app-launcher__tile.is-current');
        (current || list[0]) && (current || list[0]).focus();
        loadLines();
    }
    if (window.navDropdowns) window.navDropdowns.push(function () { close(false); });

    button.addEventListener('click', function (e) {
        e.stopPropagation();
        if (isOpen()) close(true); else open();
    });
    launcher.addEventListener('keydown', function (e) {
        if (!isOpen()) return;
        var list = items(), i = list.indexOf(document.activeElement), next = null;
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); return; }
        if (e.key === 'ArrowDown' || e.key === 'ArrowRight') next = list[(i + 1) % list.length];
        else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') next = list[(i - 1 + list.length) % list.length];
        else if (e.key === 'Home') next = list[0];
        else if (e.key === 'End') next = list[list.length - 1];
        if (next) { e.preventDefault(); next.focus(); }
    });
    menu.addEventListener('click', function (e) {
        if (e.target.closest('a')) close(false);   // choosing an app (the page then changes)
    });
    launcher.addEventListener('focusout', function (e) {
        if (e.relatedTarget && !launcher.contains(e.relatedTarget)) close(false);
    });
    document.addEventListener('click', function (e) {
        if (!launcher.contains(e.target)) close(false);
    });
})();
