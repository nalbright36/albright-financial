/* Visuals (albright_assistant.visuals): draws a chart the server checked and filled with the person's data.
   Vega, Vega-Lite and vega-embed are vendored (static/assistant/vendor - no CDN, so the CSP stays 'self'), loaded
   only when a chart is on screen. Drawn with:
     - the CSP-safe expression interpreter (ast: true + vegaInterpreter - no eval / new Function),
     - a loader that refuses every request (the data is already in the spec; nothing is ever fetched),
     - the site's light/dark colors (read from the page's CSS variables), app colors per metric (set by the server),
     - tooltips on hover / tap, the container's width.
   window.AlbrightVisuals:
     card(info, opts) -> element    a Visual card: title, chart, summary, "Updated just now", Save / Full screen / PNG
     mountAll(root)                 every [data-visual-url] element in root becomes a card (Account > Visuals pages) */
(function () {
    "use strict";
    if (window.AlbrightVisuals) return;
    var me = document.currentScript && document.currentScript.src || "";
    var BASE = me.replace(/visuals\.js(\?.*)?$/, "vendor/");
    var LIBS = ["vega.min.js", "vega-lite.min.js", "vega-embed.min.js", "vega-interpreter.js"];
    var ready = null;

    function loadScript(src) {
        return new Promise(function (resolve, reject) {
            var s = document.createElement("script");
            s.src = src; s.async = false;
            s.onload = resolve; s.onerror = function () { reject(new Error("Couldn't load " + src)); };
            document.head.appendChild(s);
        });
    }
    function libs() {
        if (window.vegaEmbed && window.vegaInterpreter) return Promise.resolve();
        if (!ready) {
            ready = LIBS.reduce(function (p, name) { return p.then(function () { return loadScript(BASE + name); }); },
                                Promise.resolve());
        }
        return ready;
    }
    function csrf() {
        var input = document.querySelector("[name=csrfmiddlewaretoken]") || document.getElementById("assistant-csrf");
        if (input) return input.value;
        var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : "";
    }
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = text;
        return n;
    }
    function cssVar(name, fallback) {
        var v = getComputedStyle(document.body).getPropertyValue(name).trim();
        return v || fallback;
    }
    function theme() {
        var text = cssVar("--text-primary", "#e6e8eb"), muted = cssVar("--text-secondary", "#8b93a1");
        var grid = cssVar("--border", "rgba(255,255,255,.08)");
        var font = getComputedStyle(document.body).fontFamily;
        return {
            background: null, font: font,
            view: { stroke: null },
            title: { color: text, font: font, fontWeight: 600, anchor: "start" },
            axis: { labelColor: muted, titleColor: muted, gridColor: grid, domainColor: grid, tickColor: grid,
                    labelFont: font, titleFont: font, titleFontWeight: 500 },
            legend: { labelColor: muted, titleColor: muted, labelFont: font, titleFont: font, orient: "bottom" },
            line: { strokeWidth: 2 }, point: { size: 50, filled: true },
            bar: { cornerRadiusEnd: 3 }
        };
    }
    // Every load is refused: the rows are already inside the spec (the server put them there).
    function blockedLoader() {
        var loader = window.vega.loader();
        var no = function () { return Promise.reject(new Error("Visuals never load anything from the network.")); };
        loader.load = no; loader.sanitize = no; loader.http = no; loader.file = no;
        return loader;
    }
    function withTheme(spec) {
        var copy = JSON.parse(JSON.stringify(spec));
        var base = theme(), mine = copy.config || {};
        Object.keys(mine).forEach(function (k) {
            base[k] = (typeof mine[k] === "object" && !Array.isArray(mine[k])) ? Object.assign({}, base[k] || {}, mine[k]) : mine[k];
        });
        copy.config = base;
        return copy;
    }
    function draw(target, spec, height) {
        return libs().then(function () {
            var s = withTheme(spec);
            if (height) s.height = height;
            return window.vegaEmbed(target, s, {
                actions: false, renderer: "svg", ast: true, expr: window.vegaInterpreter,
                loader: blockedLoader(), defaultStyle: false,
                tooltip: { theme: "custom", disableDefaultStyle: true }
            });
        });
    }
    function relative(iso) {
        var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
        if (s < 60) return "Updated just now";
        if (s < 3600) return "Updated " + Math.round(s / 60) + " min ago";
        return "Updated " + new Date(iso).toLocaleString();
    }
    function download(view, title) {
        var bg = cssVar("--bg", "#0b0d10");
        view.background(bg);
        return view.toImageURL("png", 2).then(function (url) {
            view.background(null);
            var a = document.createElement("a");
            a.href = url;
            a.download = (title || "visual").replace(/[^\w\- ]+/g, "").trim().replace(/\s+/g, "-").toLowerCase() + ".png";
            document.body.appendChild(a); a.click(); a.remove();
        });
    }
    function fullScreen(data) {
        var dlg = el("dialog", "vz-full");
        dlg.setAttribute("aria-label", data.title);
        var head = el("div", "vz-full__head");
        head.appendChild(el("h2", "vz-full__title", data.title));
        var shut = el("button", "btn btn-secondary btn-sm", "Close");
        shut.type = "button";
        shut.addEventListener("click", function () { dlg.close(); });
        head.appendChild(shut);
        var chart = el("div", "vz-full__chart");
        dlg.appendChild(head); dlg.appendChild(chart);
        if (data.summary) dlg.appendChild(el("p", "vz-full__summary", data.summary));
        dlg.addEventListener("close", function () { dlg.remove(); });
        document.body.appendChild(dlg);
        dlg.showModal();
        draw(chart, data.spec, Math.max(240, Math.round(window.innerHeight * 0.6)));
    }

    function card(info, opts) {
        opts = opts || {};
        var box = el("article", "vz-card");
        box.dataset.visual = info.id;
        var head = el("div", "vz-card__head");
        var title = el(opts.page ? "h2" : "h3", "vz-card__title");
        if (info.page_url && opts.link) {
            var a = el("a", null, info.title);
            a.href = info.page_url;
            title.appendChild(a);
        } else {
            title.textContent = info.title;
        }
        head.appendChild(title);
        if (info.version > 1 && !info.saved) head.appendChild(el("span", "vz-card__ver", "v" + info.version));
        box.appendChild(head);
        if (info.shared_by) box.appendChild(el("p", "vz-card__meta", "Shared by " + info.shared_by + " - their data"));
        var chart = el("div", "vz-card__chart");
        chart.setAttribute("role", "img");
        chart.setAttribute("aria-label", "Chart: " + info.title + (info.summary ? ". " + info.summary : ""));
        chart.appendChild(el("p", "vz-card__loading", "Drawing…"));
        box.appendChild(chart);
        if (info.summary && !opts.hideSummary) box.appendChild(el("p", "vz-card__summary", info.summary));
        var foot = el("div", "vz-card__foot");
        var updated = el("span", "vz-card__updated", "");
        foot.appendChild(updated);
        var actions = el("div", "vz-card__actions");
        foot.appendChild(actions);
        box.appendChild(foot);
        var note = el("p", "vz-card__note");
        note.setAttribute("role", "status");
        box.appendChild(note);

        var current = null, view = null;
        function button(label, handler, primary) {
            var b = el("button", "btn btn-sm " + (primary ? "btn-primary" : "btn-secondary"), label);
            b.type = "button";
            b.addEventListener("click", function () { if (current) handler(b); });
            actions.appendChild(b);
            return b;
        }
        function save(mode, b) {
            b.disabled = true;
            var body = new FormData();
            body.append("mode", mode);
            fetch(info.save_url, { method: "POST", body: body, credentials: "same-origin",
                                   headers: { "X-CSRFToken": csrf(), "Accept": "application/json" } })
                .then(function (r) { return r.json().then(function (d) { d._ok = r.ok; return d; }); })
                .then(function (d) {
                    if (!d._ok) { note.textContent = d.error || "Couldn't save it."; b.disabled = false; return; }
                    note.textContent = "";
                    var link = el("a", null, d.replaced ? "Saved over “" + info.replaces.title + "”" : "Saved to Visuals");
                    link.href = d.url;
                    note.appendChild(link);
                    info.replaces = { id: d.id, title: info.title };
                    b.disabled = false;
                })
                .catch(function () { note.textContent = "Couldn't save it - try again."; b.disabled = false; });
        }
        if (info.save_url) {
            if (info.replaces) {
                button("Replace “" + info.replaces.title.slice(0, 24) + "”", function (b) { save("replace", b); }, true);
                button("Save as new", function (b) { save("new", b); });
            } else {
                button("Save", function (b) { save("new", b); }, true);
            }
        }
        button("Full screen", function () { fullScreen(current); });
        button("Download PNG", function (b) {
            if (!view) return;
            b.disabled = true;
            download(view, info.title).catch(function () { note.textContent = "Couldn't make the PNG."; })
                .then(function () { b.disabled = false; });
        });

        fetch(info.data_url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
            .then(function (r) { return r.json().then(function (d) { d._ok = r.ok; return d; }); })
            .then(function (d) {
                if (!d._ok || d.error) { chart.textContent = d.error || "This chart can't be drawn right now."; return; }
                current = d;
                updated.textContent = relative(d.updated);
                chart.textContent = "";
                return draw(chart, d.spec, opts.height).then(function (res) { view = res.view; });
            })
            .catch(function () { chart.textContent = "This chart can't be drawn right now."; });
        return box;
    }

    function mountAll(root) {
        Array.prototype.forEach.call((root || document).querySelectorAll("[data-visual-url]"), function (slot) {
            var info = JSON.parse(slot.getAttribute("data-visual-info") || "{}");
            info.data_url = slot.getAttribute("data-visual-url");
            slot.replaceWith(card(info, { link: slot.hasAttribute("data-visual-link"),
                                          height: parseInt(slot.getAttribute("data-visual-height"), 10) || undefined,
                                          page: slot.hasAttribute("data-visual-page") }));
        });
    }

    // "Delete" forms on the Visuals pages ask first (no inline script - the CSP allows none).
    document.addEventListener("submit", function (e) {
        var form = e.target.closest && e.target.closest("form[data-confirm]");
        if (form && !window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });

    window.AlbrightVisuals = { card: card, mountAll: mountAll };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { mountAll(); });
    else mountAll();
})();
