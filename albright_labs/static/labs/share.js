/*
 * The Share control (albright_labs/_share.html) - on every page that has one. Vanilla JS.
 *  - Adding, changing View / Edit and removing post to the control's own URL and swap in the
 *    control it sends back - no reload. Without JS the forms just post.
 *  - A type-ahead combobox lists your shareable circles and the people you're connected with
 *    (albright_labs.connections) - never every user. Without JS the <select> of the same list
 *    is what you see. The search endpoint is used as you type; a local copy covers a failed fetch.
 */
(function () {
    "use strict";

    function choicesOf(root) {
        var el = root.querySelector("script[type='application/json']");
        if (!el) return {circles: [], people: [], presets: [], search: ""};
        try { return JSON.parse(el.textContent || "{}"); } catch (e) { return {circles: [], people: [], presets: [], search: ""}; }
    }

    function matches(query, item) {
        if (!query) return true;
        var hay = ((item.name || "") + " " + (item.meta || "")).toLowerCase();
        return hay.indexOf(query) !== -1;
    }

    function optionHtml(item, kind, active) {
        var cls = "share-list__item" + (active ? " is-active" : "") + (item.already ? " is-disabled" : "");
        var icon = kind === "circle"
            ? '<span class="share-chip__icon" aria-hidden="true">&#9711;</span>'
            : (kind === "person"
                ? '<span class="share-chip__avatar" aria-hidden="true">' + (item.initials || "?") + "</span>"
                : "");
        var tag = item.already ? '<span class="share-list__tag">Already shared</span>' : "";
        var meta = item.meta ? '<span class="share-list__meta">' + escape(item.meta) + "</span>" : "";
        return '<li><button type="button" class="' + cls + '" role="option" data-target="' + escapeAttr(item.target) + '"'
            + (item.already ? " disabled" : "") + ' data-kind="' + kind + '" data-name="' + escapeAttr(item.name) + '">'
            + icon + '<span class="share-list__who"><span class="share-list__name">' + escape(item.name) + "</span>"
            + meta + "</span>" + tag + "</button></li>";
    }

    function escape(s) {
        return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }
    function escapeAttr(s) { return escape(s).replace(/'/g, "&#39;"); }

    function visibleItems(data, query) {
        var out = [], q = (query || "").trim().toLowerCase();
        (data.presets || []).forEach(function (p) { if (matches(q, p)) out.push({item: p, kind: "preset"}); });
        var circles = (data.circles || []).filter(function (c) { return matches(q, c); });
        var people = (data.people || []).filter(function (p) { return matches(q, p); });
        if (circles.length) {
            out.push({label: "Circles"});
            circles.forEach(function (c) { out.push({item: c, kind: "circle"}); });
        }
        if (people.length) {
            out.push({label: "People"});
            people.forEach(function (p) { out.push({item: p, kind: "person"}); });
        }
        return out;
    }

    function renderList(picker, data, query, activeIdx) {
        var list = picker.querySelector("[data-share-list]");
        var rows = visibleItems(data, query);
        var selectable = [];
        var html = "";
        if (!rows.length) {
            var q = (query || "").trim();
            html = '<li class="share-list__empty"><p>No one you’re connected with matches' + (q ? " “" + escape(q) + "”" : "") + ".</p></li>";
        } else {
            var si = 0;
            rows.forEach(function (row) {
                if (row.label) {
                    html += '<li class="share-list__label">' + escape(row.label) + "</li>";
                    return;
                }
                var isActive = !row.item.already && si === activeIdx;
                if (!row.item.already) selectable.push(row);
                html += optionHtml(row.item, row.kind, isActive);
                if (!row.item.already) si += 1;
            });
        }
        list.innerHTML = html;
        return selectable;
    }

    function bindPicker(root) {
        var picker = root.querySelector("[data-share-picker]");
        var select = root.querySelector("[data-share-target], [data-share-mode]");
        if (!picker || picker.getAttribute("data-bound") === "1") return;
        var input = picker.querySelector("[data-share-picker-input]");
        var list = picker.querySelector("[data-share-list]");
        if (!input || !list) return;
        picker.setAttribute("data-bound", "1");
        picker.hidden = false;
        if (select) {
            select.hidden = true;
            select.disabled = true;
            select.removeAttribute("name");
        }
        var isNew = !!root.matches && root.matches("[data-share-new]");
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = isNew ? "share_mode" : "target";
        hidden.setAttribute("data-share-picked", "");
        if (isNew) hidden.value = "default";
        picker.parentNode.insertBefore(hidden, picker);

        var data = choicesOf(root);
        var live = data;
        var active = 0;
        var selectable = [];
        var timer = null;

        function isOpen() { return picker.classList.contains("is-open"); }

        function setOpen(on) {
            picker.classList.toggle("is-open", on);
            list.hidden = !on;
            input.setAttribute("aria-expanded", on ? "true" : "false");
            if (on) {
                selectable = renderList(picker, live, input.value, active);
                var first = list.querySelector(".share-list__item.is-active, .share-list__item:not(.is-disabled)");
                if (first) input.setAttribute("aria-activedescendant", ensureId(first));
                else input.removeAttribute("aria-activedescendant");
            } else {
                input.removeAttribute("aria-activedescendant");
            }
        }

        function ensureId(el) {
            if (!el.id) el.id = "share-opt-" + Math.random().toString(36).slice(2, 8);
            return el.id;
        }

        function pick(target, name) {
            hidden.value = target;
            if (input && name) input.value = name;
            setOpen(false);
        }

        function applyQuery(q) {
            live = data;
            active = 0;
            selectable = renderList(picker, live, q, active);
            if (!data.search) return;
            var url = data.search + (data.search.indexOf("?") >= 0 ? "&" : "?") + "q=" + encodeURIComponent(q);
            fetch(url, {credentials: "same-origin", headers: {"Accept": "application/json"}})
                .then(function (r) { return r.status === 200 ? r.json() : null; })
                .then(function (fresh) {
                    if (!fresh) return;
                    live = {presets: data.presets || [], circles: fresh.circles || [], people: fresh.people || [], search: data.search};
                    if (isOpen()) selectable = renderList(picker, live, q, active);
                })
                .catch(function () {});
        }

        input.addEventListener("focus", function () {
            active = 0;
            setOpen(true);
            applyQuery(input.value);
        });
        input.addEventListener("input", function () {
            hidden.value = "";
            active = 0;
            if (!isOpen()) setOpen(true);
            selectable = renderList(picker, live, input.value, active);
            if (timer) clearTimeout(timer);
            timer = setTimeout(function () { applyQuery(input.value); }, 150);
        });
        input.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                setOpen(false);
                event.preventDefault();
                return;
            }
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                if (!isOpen()) setOpen(true);
                var n = selectable.length;
                if (n) {
                    active = event.key === "ArrowDown" ? (active + 1) % n : (active - 1 + n) % n;
                    selectable = renderList(picker, live, input.value, active);
                    var btn = list.querySelector(".share-list__item.is-active");
                    if (btn) {
                        btn.scrollIntoView({block: "nearest"});
                        input.setAttribute("aria-activedescendant", ensureId(btn));
                    }
                }
                event.preventDefault();
                return;
            }
            if (event.key === "Enter" && isOpen()) {
                var chosen = list.querySelector(".share-list__item.is-active");
                if (chosen && !chosen.disabled) {
                    pick(chosen.getAttribute("data-target"), chosen.getAttribute("data-name"));
                    event.preventDefault();
                }
            }
        });
        list.addEventListener("mousedown", function (event) {
            var btn = event.target.closest(".share-list__item");
            if (!btn || btn.disabled) return;
            event.preventDefault();
            pick(btn.getAttribute("data-target"), btn.getAttribute("data-name"));
        });
        // For new forms, prefill the default label.
        if (isNew && data.presets && data.presets[0]) {
            input.placeholder = data.presets[0].name;
        }

        var form = root.matches("[data-share-add]") ? root : (root.querySelector("[data-share-add]") || (isNew ? null : root.querySelector("form.share-add")));
        if (form) {
            form.addEventListener("submit", function (event) {
                if (!hidden.value) {
                    event.preventDefault();
                    event.stopImmediatePropagation();
                    var status = root.querySelector("[data-share-status]");
                    if (status) {
                        status.textContent = "Pick a person or circle.";
                        status.classList.add("is-error");
                    }
                    setOpen(true);
                    input.focus();
                }
            });
        }
    }

    document.addEventListener("mousedown", function (event) {
        document.querySelectorAll("[data-share-picker].is-open").forEach(function (el) {
            if (!el.contains(event.target)) {
                el.classList.remove("is-open");
                var lst = el.querySelector("[data-share-list]");
                var inp = el.querySelector("[data-share-picker-input]");
                if (lst) lst.hidden = true;
                if (inp) {
                    inp.setAttribute("aria-expanded", "false");
                    inp.removeAttribute("aria-activedescendant");
                }
            }
        });
    });

    function enhance(node) {
        if (!node || !node.querySelector) return;
        if (node.matches && (node.matches("[data-share]") || node.matches("[data-share-new]"))) bindPicker(node);
        node.querySelectorAll("[data-share], [data-share-new]").forEach(bindPicker);
    }

    // Planner's view panel (and anything else that injects the Share control after
    // load) swaps the markup in with innerHTML - enhance on DOMContentLoaded never
    // sees it. Expose enhance and watch the DOM so a dynamically inserted box
    // gets the type-ahead instead of the no-JS <select>.
    window.AlbrightShare = {enhance: enhance};

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () { enhance(document); });
    } else {
        enhance(document);
    }

    if (window.MutationObserver) {
        new MutationObserver(function (records) {
            records.forEach(function (rec) {
                Array.prototype.forEach.call(rec.addedNodes, function (n) {
                    if (n.nodeType === 1) enhance(n);
                });
            });
        }).observe(document.documentElement, {childList: true, subtree: true});
    }

    function csrfToken(form) {
        var input = form && form.querySelector && form.querySelector("input[name=csrfmiddlewaretoken]");
        if (input && input.value) return input.value;
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        if (match) return decodeURIComponent(match[1]);
        var fallback = document.querySelector("input[name=csrfmiddlewaretoken]");
        return fallback ? fallback.value : "";
    }

    function formAction(form) {
        // An <input name="action"> (add / permission / remove) shadows
        // HTMLFormElement.action in every major browser: form.action is the
        // input, not the URL. fetch(form.action) throws TypeError: Failed to
        // fetch, which showed as "Not saved - try again" with no request sent.
        return (form && form.getAttribute && form.getAttribute("action")) || "";
    }

    document.addEventListener("change", function (event) {
        var t = event.target;
        if (t.matches && t.matches("[data-share-filter]")) t.form.submit();
        if (t.matches && t.matches("[data-share-autosave]")) {
            if (t.form.requestSubmit) t.form.requestSubmit(); else t.form.dispatchEvent(new Event("submit", {cancelable: true}));
        }
    });

    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches || !form.matches("[data-share-form]")) return;
        event.preventDefault();
        var box = form.closest("[data-share]");
        var headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"};
        var token = csrfToken(form);
        if (token) headers["X-CSRFToken"] = token;
        fetch(formAction(form), {
            method: "POST", body: new FormData(form), credentials: "same-origin",
            headers: headers
        }).then(function (r) {
            return r.json().then(function (data) { return data; }, function () {
                var hint = r.status ? " (HTTP " + r.status + ")" : "";
                return {ok: false, error: "Not saved" + hint + " - try again."};
            });
        }).then(function (data) {
            if (data.html && box) {
                var holder = document.createElement("div");
                holder.innerHTML = data.html;
                var fresh = holder.firstElementChild;
                box.replaceWith(fresh);
                box = fresh;
                enhance(fresh);
            }
            var status = box && box.querySelector("[data-share-status]");
            if (status) {
                status.textContent = data.ok ? (data.message || "Saved.") : (data.error || "Not saved - try again.");
                status.classList.toggle("is-error", !data.ok);
            }
        }).catch(function () {
            var status = box && box.querySelector("[data-share-status]");
            if (status) { status.textContent = "Not saved - try again."; status.classList.add("is-error"); }
        });
    });
})();
