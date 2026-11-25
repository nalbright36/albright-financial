// The global search - "Option B: Command palette" (docs/design/global_search_options.html; albright_labs.search_views,
// the one source: albright_notes.links.find). The header's Search button, Ctrl/Cmd K, or "/" (not while typing in a
// field) opens it: a centered window over a dimmed page on wide screens, a sheet sliding up from the bottom on phones
// (labs/search.css). Type to search (debounced, the newest reply wins); results are grouped by app with the app's color
// dot, the words marked. Up/Down move, Enter opens the highlighted result (the first one to start), Tab jumps to the
// next group, Esc closes. Before typing: recent searches (saved per person on the server) and suggestions.
(function () {
    "use strict";
    var trigger = document.getElementById("search-trigger");
    if (!trigger) return;
    var SEARCH_URL = trigger.getAttribute("data-search-url");
    var RECENT_URL = trigger.getAttribute("data-recent-url");
    var IS_MAC = /Mac|iPhone|iPad|iPod/.test((navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "");
    var kbdHint = document.getElementById("search-trigger-kbd");
    if (kbdHint && IS_MAC) kbdHint.textContent = "⌘K";

    var ICONS = {
        search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
        cal: '<rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4M9 15l2 2 4-4"/>',
        check: '<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M8 12l3 3 5-6"/>',
        note: '<path d="M4 4h12l4 4v12H4z"/><path d="M8 12h8M8 16h5"/>',
        fork: '<path d="M7 2v20M4 2v6a3 3 0 0 0 6 0V2M17 2c-2 2-3 5-3 8h3v12"/>',
        card: '<rect x="2" y="6" width="20" height="13" rx="2"/><path d="M2 10h20"/>',
        bulb: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.5 1 2.5h6c0-1 .3-1.8 1-2.5A6 6 0 0 0 12 3z"/>',
        dumb: '<path d="M6 7v10M3 9v6M18 7v10M21 9v6M6 12h12"/>',
        user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
        clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>'
    };
    function icon(name, size) {
        size = size || 16;
        return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" ' +
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + (ICONS[name] || ICONS.search) + "</svg>";
    }
    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
        });
    }
    function words(q) { return q.split(/\s+/).filter(Boolean).slice(0, 6); }
    // Escapes the text, then marks every word of the search in it.
    function mark(text, ws) {
        var s = String(text || "");
        if (!ws.length) return esc(s);
        var re = new RegExp("(" + ws.map(function (w) { return w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }).join("|") + ")", "gi");
        var out = "", last = 0, m;
        while ((m = re.exec(s))) {
            if (!m[0].length) { re.lastIndex++; continue; }
            out += esc(s.slice(last, m.index)) + "<mark>" + esc(m[0]) + "</mark>";
            last = m.index + m[0].length;
        }
        return out + esc(s.slice(last));
    }
    function csrf() {
        var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : "";
    }
    function color(accent) { return "--c: var(--app-" + accent + ", var(--app-labs))"; }

    // ---- the palette (built the first time it opens) -----------------------------------------------------------
    var root, box, input, chips, list, live, backFocus = null;
    var state = {q: "", app: "", data: null, active: -1, open: false};
    var cache = {}, timer = null, ctrl = null, seq = 0;

    function build() {
        root = document.createElement("div");
        root.className = "search-palette";
        root.hidden = true;
        root.innerHTML =
            '<div class="search-palette__backdrop" data-search-close></div>' +
            '<div class="search-palette__box" role="dialog" aria-modal="true" aria-label="Search">' +
            '  <div class="search-palette__grab" aria-hidden="true"></div>' +
            '  <div class="search-palette__field">' + icon("search", 18) +
            '    <input type="search" class="search-palette__input" placeholder="Search tasks, notes, recipes, money..." ' +
            '      autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="go" role="combobox" ' +
            '      aria-expanded="true" aria-controls="search-palette-list" aria-autocomplete="list" aria-label="Search">' +
            '    <kbd class="search-palette__esc">Esc</kbd>' +
            '    <button type="button" class="search-palette__cancel" data-search-close>Cancel</button>' +
            '  </div>' +
            '  <div class="search-palette__chips" role="group" aria-label="Search in"></div>' +
            '  <div class="search-palette__list" id="search-palette-list" role="listbox" aria-label="Results"></div>' +
            '  <div class="search-palette__foot" aria-hidden="true"><span><kbd>↑</kbd><kbd>↓</kbd> move</span>' +
            '    <span><kbd>↵</kbd> open</span><span><kbd>Tab</kbd> next app</span><span><kbd>Esc</kbd> close</span></div>' +
            '  <p class="visually-hidden" aria-live="polite"></p>' +
            '</div>';
        document.body.appendChild(root);
        box = root.querySelector(".search-palette__box");
        input = root.querySelector(".search-palette__input");
        chips = root.querySelector(".search-palette__chips");
        list = root.querySelector(".search-palette__list");
        live = root.querySelector("[aria-live]");

        root.addEventListener("click", function (e) {
            if (e.target.closest("[data-search-close]")) { close(); return; }
            var chip = e.target.closest("[data-app]");
            if (chip) { setApp(chip.getAttribute("data-app")); return; }
            var recent = e.target.closest("[data-recent]");
            if (recent) { input.value = recent.getAttribute("data-recent"); changed(true); input.focus(); return; }
            if (e.target.closest("[data-clear-recent]")) { clearRecent(); return; }
            var more = e.target.closest("[data-more]");
            if (more) { e.preventDefault(); setApp(more.getAttribute("data-more")); return; }
            var link = e.target.closest("a[href]");
            if (link && state.q) remember(state.q);
        });
        list.addEventListener("mousemove", function (e) {
            var opt = e.target.closest("[data-index]");
            if (opt) highlight(+opt.getAttribute("data-index"), false);
        });
        input.addEventListener("input", function () { changed(false); });
        input.addEventListener("keydown", keys);
        swipe();
    }

    function open(app) {
        if (!root) build();
        if (state.open) { input.focus(); return; }
        state.open = true;
        backFocus = document.activeElement;
        root.hidden = false;
        document.documentElement.classList.add("search-is-open");
        trigger.setAttribute("aria-expanded", "true");
        if (app !== undefined) state.app = app;
        input.focus();                       // in the click / key handler, so phones open the keyboard
        input.select();
        requestAnimationFrame(function () { root.classList.add("is-open"); });
        changed(true);
    }
    function close() {
        if (!state.open) return;
        state.open = false;
        root.classList.remove("is-open");
        box.style.transform = "";
        document.documentElement.classList.remove("search-is-open");
        trigger.setAttribute("aria-expanded", "false");
        root.hidden = true;
        if (backFocus && backFocus.focus && document.contains(backFocus)) backFocus.focus();
    }
    function setApp(app) {
        state.app = state.app === app ? "" : app;
        changed(true);
        input.focus();
    }

    // ---- fetching --------------------------------------------------------------------------------------------------
    function changed(now) {
        state.q = input.value.replace(/\s+/g, " ").trim().slice(0, 100);
        clearTimeout(timer);
        var key = state.q + "\u0000" + state.app;
        if (cache[key]) { show(cache[key]); return; }
        timer = setTimeout(function () { fetchResults(key); }, now || !state.q ? 0 : 160);
    }
    function fetchResults(key) {
        if (ctrl) ctrl.abort();
        ctrl = window.AbortController ? new AbortController() : null;
        var mine = ++seq;
        var url = SEARCH_URL + "?q=" + encodeURIComponent(state.q) + (state.app ? "&app=" + encodeURIComponent(state.app) : "");
        if (state.q) list.classList.add("is-loading");
        fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
                    signal: ctrl ? ctrl.signal : undefined})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function (data) {
                if (mine !== seq) return;
                if (data.q) cache[key] = data;
                show(data);
            })
            .catch(function (err) {
                if (err && err.name === "AbortError" || mine !== seq) return;
                list.classList.remove("is-loading");
                list.innerHTML = '<p class="search-palette__empty">' + (navigator.onLine === false
                    ? "Search is available when you're back online." : "Search didn't load. Try again.") + "</p>";
                state.data = null;
            });
    }
    function remember(q) {
        var body = new URLSearchParams({q: q});
        try {
            fetch(RECENT_URL, {method: "POST", credentials: "same-origin", keepalive: true, body: body,
                               headers: {"X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest"}});
        } catch (e) {}
        Object.keys(cache).forEach(function (k) { if (k.indexOf("\u0000") === 0) delete cache[k]; });   // stale recents
    }
    function clearRecent() {
        fetch(RECENT_URL, {method: "POST", credentials: "same-origin", body: new URLSearchParams({clear: "1"}),
                           headers: {"X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest"}})
            .then(function () { cache = {}; changed(true); });
    }

    // ---- drawing ---------------------------------------------------------------------------------------------------
    function drawChips(filters) {
        var html = '<button type="button" class="search-chip' + (state.app ? "" : " is-on") + '" data-app="" aria-pressed="' +
            (!state.app) + '">All</button>';
        (filters || []).forEach(function (f) {
            var on = state.app === f.key;
            html += '<button type="button" class="search-chip' + (on ? " is-on" : "") + '" data-app="' + esc(f.key) +
                '" aria-pressed="' + on + '" style="' + color(f.accent) + '">' + esc(f.label) + "</button>";
        });
        chips.innerHTML = html;
    }
    function show(data) {
        list.classList.remove("is-loading");
        state.data = data;
        drawChips(data.filters);
        if (!state.q) { drawStart(data); return; }
        var ws = words(state.q), html = "", index = 0;
        (data.groups || []).forEach(function (g) {
            html += '<section class="search-group" style="' + color(g.accent) + '" data-group="' + esc(g.key) + '">' +
                '<h3 class="search-group__head"><span class="search-dot" aria-hidden="true"></span>' + esc(g.label);
            if (g.see_all) html += '<a class="search-group__all" href="' + esc(g.see_all) + '">See all in ' + esc(g.label) + "</a>";
            else if (!data.app && g.more) html += '<a class="search-group__all" href="#" data-more="' + esc(g.key) + '">See all in ' + esc(g.label) + "</a>";
            html += "</h3>";
            g.results.forEach(function (r) {
                html += '<a class="search-result" role="option" id="search-opt-' + index + '" data-index="' + index + '" href="' +
                    esc(r.url) + '" aria-selected="false">' +
                    '<span class="search-result__icon" aria-hidden="true">' + icon(r.icon) + "</span>" +
                    '<span class="search-result__text"><span class="search-result__title">' + mark(r.title, ws) + "</span>" +
                    (r.snippet ? '<small class="search-result__snippet">' + mark(r.snippet, ws) + "</small>" : "") +
                    (r.info ? '<small class="search-result__info">' + esc(r.info) + "</small>" : "") +
                    '<span class="visually-hidden">, ' + esc(r.kind) + "</span></span>" +
                    '<kbd class="search-result__enter" aria-hidden="true">↵</kbd></a>';
                index++;
            });
            html += "</section>";
        });
        if (!index) {
            html = '<div class="search-palette__empty"><p>Nothing found for “' + esc(state.q) + '”' +
                (data.app ? " in " + esc(labelOf(data.app)) : "") + ".</p>";
            if (data.app) html += '<p><button type="button" class="search-palette__link" data-app="">Search everything instead</button></p>';
            (data.elsewhere || []).forEach(function (e) {
                html += '<p><a class="search-palette__link" href="' + esc(e.url) + '">' + icon("arrow", 14) + esc(e.label) + "</a></p>";
            });
            html += "</div>";
        }
        list.innerHTML = html;
        list.scrollTop = 0;
        highlight(index ? 0 : -1, false);
        live.textContent = index ? index + (index === 1 ? " result" : " results") : "No results";
    }
    function labelOf(key) {
        var f = ((state.data && state.data.filters) || []).filter(function (x) { return x.key === key; })[0];
        return f ? f.label : key;
    }
    function drawStart(data) {
        var html = "";
        if ((data.recent || []).length) {
            html += '<section class="search-start"><h3 class="search-start__head">Recent searches' +
                '<button type="button" class="search-start__clear" data-clear-recent>Clear</button></h3>';
            data.recent.forEach(function (q) {
                html += '<button type="button" class="search-recent" data-recent="' + esc(q) + '">' + icon("clock") +
                    "<span>" + esc(q) + "</span></button>";
            });
            html += "</section>";
        }
        if ((data.suggestions || []).length) {
            html += '<section class="search-start"><h3 class="search-start__head">Try</h3><div class="search-suggest">';
            data.suggestions.forEach(function (s) {
                html += '<a class="search-suggest__item" href="' + esc(s.url) + '">' + icon(s.icon, 14) + esc(s.label) + "</a>";
            });
            html += "</div></section>";
        }
        list.innerHTML = html || '<p class="search-palette__empty">Search your tasks, notes, recipes and more.</p>';
        highlight(-1, false);
        live.textContent = "";
    }

    // ---- keyboard --------------------------------------------------------------------------------------------------
    function options() { return list.querySelectorAll(".search-result"); }
    function highlight(i, scroll) {
        var opts = options();
        if (state.active >= 0 && opts[state.active]) {
            opts[state.active].classList.remove("is-active");
            opts[state.active].setAttribute("aria-selected", "false");
        }
        state.active = opts.length ? Math.max(0, Math.min(i, opts.length - 1)) : -1;
        if (i < 0) state.active = -1;
        var el = opts[state.active];
        if (el) {
            el.classList.add("is-active");
            el.setAttribute("aria-selected", "true");
            input.setAttribute("aria-activedescendant", el.id);
            if (scroll !== false) el.scrollIntoView({block: "nearest"});
        } else {
            input.removeAttribute("aria-activedescendant");
        }
    }
    function nextGroup(back) {
        var opts = options();
        if (!opts.length) return;
        var groups = Array.prototype.slice.call(list.querySelectorAll(".search-group"));
        var current = opts[state.active] ? opts[state.active].closest(".search-group") : null;
        var at = groups.indexOf(current);
        var to = groups[(at + (back ? -1 : 1) + groups.length) % groups.length];
        var first = to && to.querySelector(".search-result");
        if (first) highlight(+first.getAttribute("data-index"));
    }
    function keys(e) {
        if (e.key === "Escape") { e.preventDefault(); close(); return; }
        if (e.key === "ArrowDown") { e.preventDefault(); highlight(state.active + 1); return; }
        if (e.key === "ArrowUp") { e.preventDefault(); highlight(Math.max(0, state.active - 1)); return; }
        if (e.key === "Tab" && options().length) { e.preventDefault(); nextGroup(e.shiftKey); return; }
        if (e.key === "Enter") {
            var el = options()[state.active];
            e.preventDefault();
            if (!el) { changed(true); return; }
            remember(state.q);
            if (e.metaKey || e.ctrlKey) window.open(el.href, "_blank", "noopener");
            else window.location.href = el.href;
        }
    }
    function typing(el) {
        if (!el || el === document.body) return false;
        var tag = el.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
    }
    document.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === "k" || e.key === "K")) {
            e.preventDefault();
            if (state.open) close(); else open();
            return;
        }
        if (e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey && !state.open && !typing(document.activeElement) &&
            !document.querySelector("dialog[open]")) {
            e.preventDefault();
            open();
        }
    });
    trigger.addEventListener("click", function () { open(); });

    // ---- phones: swipe the sheet down to close ---------------------------------------------------------------------
    function swipe() {
        var startY = null, dy = 0;
        box.addEventListener("touchstart", function (e) {
            if (!window.matchMedia("(max-width: 639.98px)").matches || e.touches.length !== 1) return;
            var fromList = e.target.closest(".search-palette__list, .search-palette__chips");
            if (fromList && (list.scrollTop > 0 || e.target.closest(".search-palette__chips"))) return;
            startY = e.touches[0].clientY; dy = 0;
        }, {passive: true});
        box.addEventListener("touchmove", function (e) {
            if (startY == null) return;
            dy = e.touches[0].clientY - startY;
            if (dy > 0) { box.style.transition = "none"; box.style.transform = "translateY(" + dy + "px)"; }
        }, {passive: true});
        box.addEventListener("touchend", function () {
            if (startY == null) return;
            box.style.transition = "";
            if (dy > 90) close(); else box.style.transform = "";
            startY = null;
        });
    }

    window.AlbrightSearch = {open: open, close: close};
})();
