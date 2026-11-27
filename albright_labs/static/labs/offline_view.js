// The offline page (albright_labs/offline.html): /sw.js shows it, at the page's own address, offline - always for the
// sections that can be changed offline (push_views.edit_paths), and for any other page it has no saved copy of. This
// draws the section from the IndexedDB snapshot (labs/offline.js, AlbrightOffline.get) and lets you change it:
//   Today / Week   check off, skip, add a task, edit title / date / time / notes, tick checklist items, delete
//   Groceries      add, check off, edit, delete
//   Notes          new note, edit, tick checklist items, delete (to Recently deleted)
//   Fitness        log food, log a weight, quick-log a workout, mark a planned workout done or skip it
//   Meals          move a planned meal to another day; Recipes: rate one
// Every change goes through AlbrightSync.write (labs/sync.js): it shows at once, waits in the outbox and syncs when
// the connection is back; something changed but not yet synced has a small dot. Anything else stays read-only
// ("Available when you're back online"). Everything is text (textContent) - nothing from the data is ever HTML.
(function () {
    var view = document.getElementById("offline-view");
    if (!view) return;
    var out = document.getElementById("offline-snapshot"), card = document.getElementById("offline-card");
    var retry = document.getElementById("offline-retry");
    if (retry) retry.addEventListener("click", function () { window.location.reload(); });
    var url = function (name) { return view.getAttribute("data-" + name); };
    var path = window.location.pathname;
    if (path === url("offline") || path === "/offline/") return;            // the page itself: just the card
    var ID = "(\\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})";

    function route() {
        var m;
        if (path === "/" || path === "/home/" || path === url("planner") || path === url("today")) return {name: "today"};
        if (path === url("week")) return {name: "week"};
        if (path === url("groceries")) return {name: "groceries"};
        if (path === url("meals")) return {name: "meals"};
        if (path === url("recipes")) return {name: "recipes"};
        if ((m = path.match(new RegExp("^(.*\\/recipes\\/)(\\d+)\\/$"))) && m[1] === url("recipes")) return {name: "recipe", id: m[2]};
        if (path === url("notes")) return {name: "notes"};
        if ((m = path.match(new RegExp("^(.*\\/)" + ID + "\\/$"))) && m[1] === url("notes")) return {name: "note", id: m[2]};
        if (path.indexOf(url("fitness")) === 0) return {name: "fitness"};
        return null;
    }

    // ---------- little DOM helpers (text only) ----------
    function el(tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text != null) e.textContent = text;
        return e;
    }
    function link(href, text, cls) { var a = el("a", cls, text); a.href = href; return a; }
    function button(text, cls, fn) {
        var b = el("button", cls || "btn btn-secondary btn-sm", text);
        b.type = "button";
        if (fn) b.addEventListener("click", fn);
        return b;
    }
    function input(type, value, attrs) {
        var i = document.createElement(type === "textarea" ? "textarea" : "input");
        if (type !== "textarea") i.type = type;
        i.className = "ov__input";
        if (value != null) i.value = value;
        Object.keys(attrs || {}).forEach(function (k) { i.setAttribute(k, attrs[k]); });
        return i;
    }
    function form(cls, fields, label, onSubmit) {
        var f = el("form", "ov__form " + (cls || ""));
        fields.forEach(function (x) { f.appendChild(x); });
        var b = el("button", "btn btn-primary btn-sm", label);
        b.type = "submit";
        f.appendChild(b);
        f.addEventListener("submit", function (e) { e.preventDefault(); onSubmit(); });
        return f;
    }
    function section(title, sub) {
        var box = el("section", "card ov__card");
        var head = el("div", "card__header");
        head.appendChild(el("span", "card__label", title));
        if (sub) head.appendChild(el("span", "ov__sub", sub));
        box.appendChild(head);
        return box;
    }
    function empty(box, text) { box.appendChild(el("p", "ov__empty", text)); return box; }
    function day(iso, opts) {
        var p = iso.split("-");
        return new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString([], opts || {weekday: "long", month: "short", day: "numeric"});
    }
    function time(hm) {
        if (!hm) return "";
        var p = hm.split(":"), d = new Date();
        d.setHours(+p[0], +p[1], 0, 0);
        return d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
    }
    function iso(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
    function quoted(t) { return "“" + (t || "Untitled") + "”"; }
    function check(done, label, fn, disabled) {
        var b = el("button", "ov__check" + (done ? " is-done" : ""), done ? "✓" : "");
        b.type = "button";
        b.setAttribute("role", "checkbox");
        b.setAttribute("aria-checked", done ? "true" : "false");
        b.setAttribute("aria-label", label);
        if (disabled || !fn) { b.disabled = true; b.title = "View only"; } else b.addEventListener("click", fn);
        return b;
    }
    function keyed(node, key) { if (key) node.setAttribute("data-sync-key", key); return node; }
    function nav(active) {
        var bar = el("nav", "ov__nav");
        bar.setAttribute("aria-label", "Saved for offline");
        [["today", "Today", url("today")], ["week", "Week", url("week")], ["meals", "Meals", url("meals")],
         ["groceries", "Groceries", url("groceries")], ["recipes", "Recipes", url("recipes")],
         ["notes", "Notes", url("notes")], ["fitness", "Fitness", url("fitness")]].forEach(function (s) {
            var a = link(s[2], s[1], "ov__tab" + (s[0] === active ? " is-on" : ""));
            if (s[0] === active) a.setAttribute("aria-current", "page");
            bar.appendChild(a);
        });
        return bar;
    }

    // ---------- changing things (labs/sync.js) ----------
    var Sync = window.AlbrightSync || {};
    var snap = null, r = route(), openPanel = null;
    function canEdit() { return !!Sync.enabled; }
    function edit(action, o, quiet) {
        return Sync.write(action, o).then(function (fresh) {
            if (fresh) snap = fresh;
            if (quiet) marks(); else draw();                // quiet: someone is typing - leave the page as it is
            return fresh;
        }).catch(function (err) { toast(err && err.message ? err.message : "That couldn't be saved on this device."); });
    }
    function uuid() { return window.AlbrightSyncCore ? window.AlbrightSyncCore.uuid() : String(Date.now()); }
    function toast(text) {
        var t = document.getElementById("toast-container");
        if (!t) {
            t = el("div", "toast-container");
            t.id = "toast-container";
            t.setAttribute("role", "status");
            t.setAttribute("aria-live", "polite");
            document.body.appendChild(t);
        }
        var item = el("div", "toast", text);
        t.appendChild(item);
        setTimeout(function () { if (item.parentNode) item.parentNode.removeChild(item); }, 3500);
    }
    function marks() {
        var keys = Sync.keys ? Sync.keys() : {};
        out.querySelectorAll("[data-sync-key]").forEach(function (node) {
            var state = keys[node.getAttribute("data-sync-key")];
            node.classList.toggle("is-pending", state === "pending");
            node.classList.toggle("is-failed", state === "failed");
        });
    }

    // ---------- Today / Week ----------
    function occKey(o) { return "planner:" + o.id + ":" + o.occurrence; }
    function plannerRestore(o) {
        return {action: "planner.restore", payload: {title: o.title, date: o.date, time: o.start || "", notes: o.notes || ""}, name: o.title};
    }
    function itemPanel(o) {
        var box = el("div", "ov__panel");
        var title = input("text", o.title, {"aria-label": "Title", maxlength: "200"});
        var date = input("date", o.date, {"aria-label": "Date"});
        var start = input("time", o.start || "", {"aria-label": "Time"});
        var notes = input("textarea", o.notes || "", {"aria-label": "Notes", rows: "3", placeholder: "Notes"});
        if (o.repeats) date.title = "Moves just this one";
        [title, date, start, notes].forEach(function (x) { box.appendChild(x); });
        var acts = el("div", "ov__acts");
        acts.appendChild(button("Save", "btn btn-primary btn-sm", function () {
            var base = {target: o.id, key: occKey(o), restore: plannerRestore(o)};
            var jobs = [], name = title.value.trim() || o.title;
            if (title.value.trim() && title.value.trim() !== o.title) {
                jobs.push(["planner.edit", {field: "title", value: title.value.trim(), date: o.occurrence}, "Rename " + quoted(o.title) + " to " + quoted(name)]);
            }
            if ((notes.value || "") !== (o.notes || "")) jobs.push(["planner.edit", {field: "notes", value: notes.value, date: o.occurrence}, "Edit notes on " + quoted(name)]);
            if ((start.value || "") !== (o.start || "")) {
                var end = start.value && o.end && o.end > start.value ? o.end : "";
                jobs.push(["planner.edit", {field: "time", start: start.value || "", end: end, date: o.occurrence}, "Change the time of " + quoted(name)]);
            }
            if (date.value && date.value !== o.date) jobs.push(["planner.edit", {field: "date", value: date.value, date: o.occurrence}, "Move " + quoted(name) + " to " + day(date.value, {month: "short", day: "numeric"})]);
            openPanel = null;
            var run = Promise.resolve();
            jobs.forEach(function (j) {
                run = run.then(function () { return edit(j[0], {target: base.target, key: base.key, restore: base.restore, payload: j[1], label: j[2]}); });
            });
            run.then(draw);
        }));
        if (o.repeats || o.workout || o.weigh_in) {
            acts.appendChild(button("Skip", "btn btn-secondary btn-sm", function () {
                openPanel = null;
                edit("planner.skip", {target: o.id, key: occKey(o), payload: {date: o.occurrence}, label: "Skip " + quoted(o.title)});
            }));
        }
        if (!o.workout && !o.weigh_in && (!o.repeats || o.owner)) {
            acts.appendChild(button(o.repeats ? "Delete series" : "Delete", "btn btn-danger btn-sm", function () {
                if (!window.confirm("Delete " + quoted(o.title) + (o.repeats ? " and every repeat" : "") + "?")) return;
                openPanel = null;
                edit("planner.delete", {target: o.id, key: occKey(o), payload: {date: o.occurrence, scope: "all"},
                                        label: "Delete " + quoted(o.title), restore: plannerRestore(o)});
            }));
        }
        acts.appendChild(button("Cancel", "btn btn-ghost btn-sm", function () { openPanel = null; draw(); }));
        box.appendChild(acts);
        return box;
    }
    function items(list) {
        var ul = el("ul", "ov__list");
        list.forEach(function (o) {
            var li = keyed(el("li", "ov__item" + (o.done ? " is-done" : "")), occKey(o));
            var editable = canEdit() && o.editable;
            if (o.checkable || o.kind === "task") {
                li.appendChild(check(o.done, (o.done ? "Uncheck " : "Check off ") + o.title, function () {
                    edit("planner.check", {target: o.id, key: occKey(o), payload: {date: o.occurrence, done: !o.done},
                                           label: (o.done ? "Uncheck " : "Check off ") + quoted(o.title), restore: plannerRestore(o)});
                }, !editable));
            } else li.appendChild(el("span", "ov__dot"));
            var main = el("div", "ov__main");
            var t = editable ? button(o.title, "ov__title ov__title--btn", function () {
                openPanel = openPanel === occKey(o) ? null : occKey(o);
                draw();
            }) : el("span", "ov__title", o.title);
            if (editable) t.setAttribute("aria-expanded", openPanel === occKey(o) ? "true" : "false");
            main.appendChild(t);
            var when = o.start ? time(o.start) + (o.end ? " – " + time(o.end) : "") : "";
            if (when || o.skipped) main.appendChild(el("span", "ov__meta", [when, o.skipped ? "Skipped" : ""].filter(Boolean).join(" · ")));
            if (o.checklist && o.checklist.length) {
                var sub = el("ul", "ov__checklist");
                o.checklist.forEach(function (row) {
                    var li2 = el("li", row.done ? "is-done" : "");
                    li2.appendChild(check(row.done, (row.done ? "Untick " : "Tick ") + row.text, function () {
                        edit("planner.checklist", {target: o.id, key: occKey(o), payload: {row: row.id, date: o.occurrence, done: !row.done},
                                                   label: (row.done ? "Untick " : "Tick ") + quoted(row.text) + " on " + quoted(o.title)});
                    }, !editable || typeof row.id !== "number"));
                    li2.appendChild(el("span", "", row.text));
                    sub.appendChild(li2);
                });
                main.appendChild(sub);
            }
            if (openPanel === occKey(o) && editable) main.appendChild(itemPanel(o));
            li.appendChild(main);
            ul.appendChild(li);
        });
        return ul;
    }
    function addTask(on) {
        var title = input("text", "", {placeholder: "Add a task", "aria-label": "New task", maxlength: "200"});
        var at = input("time", "", {"aria-label": "Time (optional)"});
        return form("ov__add", [title, at], "Add", function () {
            var text = title.value.trim();
            if (!text) { title.focus(); return; }
            var id = uuid();
            edit("planner.add", {client_id: id, key: "planner:" + id + ":" + on, payload: {title: text, date: on, time: at.value || ""},
                                 label: "Add " + quoted(text) + " on " + day(on, {month: "short", day: "numeric"})});
        });
    }
    function dayBox(data, d, title) {
        var list = data.planner.items.filter(function (o) { return o.date === d; });
        var box = section(title, title === day(d) ? "" : day(d));
        if (list.length) box.appendChild(items(list)); else empty(box, "Nothing planned.");
        if (canEdit()) box.appendChild(addTask(d));
        return box;
    }
    function today(data) {
        if (!data.planner) return null;
        var q = new URLSearchParams(window.location.search).get("date");
        var on = q && q >= data.planner.start && q <= data.planner.end ? q : data.planner.start;
        return dayBox(data, on, on === data.planner.start ? "Today" : day(on));
    }
    function week(data) {
        if (!data.planner) return null;
        var wrap = el("div", "ov__stack");
        var d = data.planner.start, guard = 0;
        while (d <= data.planner.end && guard++ < 10) {
            wrap.appendChild(dayBox(data, d, day(d)));
            var p = d.split("-");
            d = iso(new Date(+p[0], +p[1] - 1, +p[2] + 1));
        }
        return wrap;
    }

    // ---------- Meals ----------
    function meals(data) {
        if (!data.meal_plan) return null;
        var plan = data.meal_plan, wrap = el("div", "ov__stack");
        var dates = [];
        plan.entries.forEach(function (e) { if (dates.indexOf(e.date) < 0) dates.push(e.date); });
        if (!dates.length) return empty(section("This week's meals"), "Nothing planned this week.");
        dates.sort().forEach(function (d) {
            var box = section(day(d)), ul = el("ul", "ov__list");
            plan.entries.filter(function (e) { return e.date === d; }).forEach(function (e) {
                var li = keyed(el("li", "ov__item"), "meal:" + e.id);
                var main = el("div", "ov__main");
                var recipe = e.recipe_id && (data.recipes || []).some(function (x) { return x.id === e.recipe_id; });
                main.appendChild(recipe ? link(url("recipes") + e.recipe_id + "/", e.title, "ov__title") : el("span", "ov__title", e.title));
                main.appendChild(el("span", "ov__meta", e.slot));
                li.appendChild(main);
                if (canEdit() && e.id) {
                    var move = input("date", e.date, {"aria-label": "Move " + e.title + " to"});
                    move.className = "ov__input ov__input--date";
                    move.addEventListener("change", function () {
                        if (!move.value || move.value === e.date) return;
                        edit("meal.move", {target: e.id, key: "meal:" + e.id, payload: {date: move.value, slot: e.slot_key},
                                           label: "Move " + quoted(e.title) + " to " + day(move.value, {month: "short", day: "numeric"})});
                    });
                    li.appendChild(move);
                }
                ul.appendChild(li);
            });
            box.appendChild(ul);
            wrap.appendChild(box);
        });
        return wrap;
    }

    // ---------- Groceries ----------
    function lineOf(i) {
        var head = [i.quantity, i.unit, i.name].filter(Boolean).join(" ");
        return i.note ? head + ", " + i.note : head;
    }
    function groceries(data) {
        if (!data.groceries) return null;
        var box = section("Groceries", data.space || "");
        if (canEdit()) {
            var line = input("text", "", {placeholder: "Add an item, e.g. 2 lb chicken", "aria-label": "Add an item", maxlength: "300"});
            box.appendChild(form("ov__add", [line], "Add", function () {
                var text = line.value.trim();
                if (!text) { line.focus(); return; }
                var id = uuid();
                edit("grocery.add", {client_id: id, key: "grocery:" + id, payload: {line: text}, label: "Add " + quoted(text) + " to groceries"});
            }));
        }
        if (!data.groceries.length) return empty(box, "The list is empty.");
        var aisles = [];
        data.groceries.forEach(function (i) { var a = i.aisle || "Other"; if (aisles.indexOf(a) < 0) aisles.push(a); });
        aisles.forEach(function (a) {
            if (aisles.length > 1) box.appendChild(el("h3", "ov__h", a));
            var ul = el("ul", "ov__list");
            data.groceries.filter(function (i) { return (i.aisle || "Other") === a; }).forEach(function (i) {
                var key = "grocery:" + i.id, text = lineOf(i);
                var li = keyed(el("li", "ov__item" + (i.checked ? " is-done" : "")), key);
                var restore = {action: "grocery.restore", payload: {line: text}, name: i.name};
                li.appendChild(check(i.checked, (i.checked ? "Uncheck " : "Check off ") + i.name, canEdit() ? function () {
                    edit("grocery.check", {target: i.id, key: key, payload: {checked: !i.checked}, restore: restore,
                                           label: (i.checked ? "Uncheck " : "Check off ") + quoted(i.name)});
                } : null));
                var main = el("div", "ov__main");
                if (openPanel === key && canEdit()) {
                    var field = input("text", text, {"aria-label": "Item", maxlength: "300"});
                    main.appendChild(form("ov__inline", [field], "Save", function () {
                        var v = field.value.trim();
                        openPanel = null;
                        if (!v || v === text) { draw(); return; }
                        edit("grocery.edit", {target: i.id, key: key, payload: {line: v}, restore: restore, label: "Change " + quoted(i.name) + " to " + quoted(v)});
                    }));
                    var acts = el("div", "ov__acts");
                    acts.appendChild(button("Delete", "btn btn-danger btn-sm", function () {
                        openPanel = null;
                        edit("grocery.delete", {target: i.id, key: key, label: "Delete " + quoted(i.name), restore: restore});
                    }));
                    acts.appendChild(button("Cancel", "btn btn-ghost btn-sm", function () { openPanel = null; draw(); }));
                    main.appendChild(acts);
                    setTimeout(function () { field.focus(); }, 0);
                } else {
                    var name = [i.quantity, i.unit, i.name].filter(Boolean).join(" ");
                    main.appendChild(canEdit() ? button(name, "ov__title ov__title--btn", function () { openPanel = key; draw(); }) : el("span", "ov__title", name));
                    if (i.note) main.appendChild(el("span", "ov__meta", i.note));
                }
                li.appendChild(main);
                ul.appendChild(li);
            });
            box.appendChild(ul);
        });
        return box;
    }

    // ---------- Recipes ----------
    function recipes(data) {
        if (!data.recipes) return null;
        var box = section("Recipes", data.recipes.length + " saved");
        if (!data.recipes.length) return empty(box, "No recipes yet.");
        var ul = el("ul", "ov__list");
        data.recipes.forEach(function (x) {
            var li = keyed(el("li", "ov__item"), "recipe:" + x.id);
            var main = el("div", "ov__main");
            main.appendChild(link(url("recipes") + x.id + "/", x.title, "ov__title"));
            var meta = [x.rating, x.minutes ? x.minutes + " min" : ""].filter(Boolean).join(" · ");
            if (meta) main.appendChild(el("span", "ov__meta", meta));
            li.appendChild(main);
            ul.appendChild(li);
        });
        box.appendChild(ul);
        return box;
    }
    function steps(list, box) {
        var ol = el("ol", "ov__steps");
        list.forEach(function (s) {
            if (/^##\s*/.test(s)) { box.appendChild(ol); box.appendChild(el("h3", "ov__h", s.replace(/^##\s*/, ""))); ol = el("ol", "ov__steps"); return; }
            ol.appendChild(el("li", "", s));
        });
        box.appendChild(ol);
    }
    function recipe(data, id) {
        var x = (data.recipes || []).filter(function (y) { return String(y.id) === String(id); })[0];
        if (!x) return null;
        var wrap = el("div", "ov__stack");
        var head = keyed(section(x.title, [x.rating, x.servings ? "Serves " + x.servings : "", x.minutes ? x.minutes + " min" : ""].filter(Boolean).join(" · ")), "recipe:" + x.id);
        if (x.description) head.appendChild(el("p", "ov__text", x.description));
        if (canEdit() && (data.rating_levels || []).length) {
            var rates = el("div", "ov__rates");
            rates.setAttribute("role", "group");
            rates.setAttribute("aria-label", "Your rating");
            data.rating_levels.forEach(function (l) {
                var on = x.rating_level === l.level;
                var b = button(l.emoji + " " + l.label, "ov__rate" + (on ? " is-on" : ""), function () {
                    edit("recipe.rate", {target: x.id, key: "recipe:" + x.id, payload: {level: on ? "" : l.level},
                                         label: on ? "Clear your rating of " + quoted(x.title) : "Rate " + quoted(x.title) + " " + l.label});
                });
                b.setAttribute("aria-pressed", on ? "true" : "false");
                rates.appendChild(b);
            });
            head.appendChild(rates);
        }
        head.appendChild(link(url("recipes"), "‹ All recipes", "ov__back"));
        wrap.appendChild(head);
        var ing = section("Ingredients"), ul = el("ul", "ov__bullets");
        x.ingredients.forEach(function (i) { ul.appendChild(el("li", "", i)); });
        if (x.ingredients.length) ing.appendChild(ul); else empty(ing, "None listed.");
        wrap.appendChild(ing);
        var how = section("Steps");
        if (x.steps.length) steps(x.steps, how); else empty(how, "None listed.");
        wrap.appendChild(how);
        return wrap;
    }

    // ---------- Notes ----------
    function notes(data) {
        if (!data.notes) return null;
        var box = section("Notes", data.notes.length + " saved");
        if (canEdit()) {
            box.appendChild(button("+ New note", "btn btn-primary btn-sm ov__new", function () {
                var id = uuid();
                edit("note.create", {client_id: id, key: "note:" + id, payload: {title: "Untitled", body: ""}, label: "New note"})
                    .then(function () { window.location.href = url("notes") + id + "/"; });
            }));
        }
        if (!data.notes.length) return empty(box, "No notes yet.");
        var ul = el("ul", "ov__list");
        data.notes.forEach(function (n) {
            var li = keyed(el("li", "ov__item"), "note:" + n.id);
            var main = el("div", "ov__main");
            main.appendChild(link(url("notes") + n.id + "/", (n.pinned ? "📌 " : "") + (n.title || "Untitled"), "ov__title"));
            var first = (n.body || "").replace(/[#*>`_\-\[\]]/g, " ").replace(/\s+/g, " ").trim();
            if (first) main.appendChild(el("span", "ov__meta", first.slice(0, 120)));
            li.appendChild(main);
            ul.appendChild(li);
        });
        box.appendChild(ul);
        return box;
    }
    var noteTimer = null, editingText = false;
    function saveNote(n, title, body, quiet) {
        clearTimeout(noteTimer);
        return edit("note.edit", {target: n.id, base: typeof n.version === "number" && n.version > 0 ? n.version : null,
                                  key: "note:" + n.id, payload: {title: title, body: body}, label: "Edit " + quoted(title),
                                  restore: {action: "note.restore", target: n.id, payload: {title: title, body: body}, name: title}},
                    quiet);
    }
    function noteLines(n, box) {
        var wrap = el("div", "ov__note ov__note--lines"), index = -1, fenced = false;
        (n.body || "").split("\n").forEach(function (line) {
            if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
            var m = !fenced && line.match(/^(\s*)[-*+]\s+\[([ xX])\]\s?(.*)$/);
            if (!m) { wrap.appendChild(el("div", "ov__line", line || " ")); return; }
            var i = ++index, done = m[2] !== " ";
            var row = el("div", "ov__line ov__line--check" + (done ? " is-done" : ""));
            row.style.paddingLeft = (m[1].length * 0.5) + "em";
            row.appendChild(check(done, (done ? "Untick " : "Tick ") + m[3], canEdit() && n.complete !== false ? function () {
                var ids = n.check_ids || [];
                var local = !/^\d+$/.test(String(n.id)) || !ids[i] || (Sync.waiting && Sync.waiting("note:" + n.id, ["note.create", "note.edit"]));
                if (local) {                                    // made or edited offline: tick it in the text itself
                    var next = (n.body || "").split("\n"), k = -1, f2 = false;
                    next = next.map(function (l) {
                        if (/^\s*(```|~~~)/.test(l)) f2 = !f2;
                        if (!f2 && /^\s*[-*+]\s+\[[ xX]\]/.test(l) && ++k === i) return l.replace(/\[[ xX]\]/, done ? "[ ]" : "[x]");
                        return l;
                    }).join("\n");
                    saveNote(n, n.title, next);
                } else {
                    edit("note.check", {target: n.id, key: "note:" + n.id, payload: {item_id: ids[i], index: i, checked: !done},
                                        label: (done ? "Untick " : "Tick ") + quoted(m[3]) + " in " + quoted(n.title)});
                }
            } : null));
            row.appendChild(el("span", "", m[3]));
            wrap.appendChild(row);
        });
        box.appendChild(wrap);
    }
    function note(data, id) {
        var n = (data.notes || []).filter(function (x) { return String(x.id) === String(id); })[0];
        if (!n) return null;
        var editable = canEdit() && n.complete !== false;
        var box = keyed(section(editable ? "Note" : (n.title || "Untitled"), n.tags && n.tags.length ? "#" + n.tags.join(" #") : ""), "note:" + n.id);
        if (editable) {
            var title = input("text", n.title || "", {"aria-label": "Title", maxlength: "200", placeholder: "Title"});
            title.className = "ov__input ov__note-title";
            title.addEventListener("input", function () {
                clearTimeout(noteTimer);
                noteTimer = setTimeout(function () { n.title = title.value.trim() || "Untitled"; saveNote(n, n.title, n.body || "", true); }, 800);
            });
            box.appendChild(title);
        }
        if (editable && editingText) {
            var text = input("textarea", n.body || "", {"aria-label": "Note", rows: "14"});
            text.className = "ov__input ov__note-text";
            text.addEventListener("input", function () {
                clearTimeout(noteTimer);
                noteTimer = setTimeout(function () {
                    n.body = text.value;
                    saveNote(n, (title && title.value.trim()) || n.title || "Untitled", text.value, true);
                }, 800);
            });
            box.appendChild(text);
            box.appendChild(button("Done", "btn btn-primary btn-sm", function () {
                editingText = false;
                saveNote(n, (title && title.value.trim()) || n.title || "Untitled", text.value);
            }));
            setTimeout(function () { if (!document.activeElement || document.activeElement === document.body) text.focus(); }, 0);
        } else {
            noteLines(n, box);
            var acts = el("div", "ov__acts");
            if (editable) acts.appendChild(button("Edit text", "btn btn-secondary btn-sm", function () { editingText = true; draw(); }));
            if (canEdit()) {
                acts.appendChild(button("Delete", "btn btn-danger btn-sm", function () {
                    if (!window.confirm("Move " + quoted(n.title) + " to Recently deleted?")) return;
                    edit("note.delete", {target: n.id, key: "note:" + n.id, label: "Delete " + quoted(n.title),
                                         restore: {action: "note.restore", target: n.id, payload: {title: n.title, body: n.body}, name: n.title}})
                        .then(function () { window.location.href = url("notes"); });
                }));
            }
            box.appendChild(acts);
            if (canEdit() && n.complete === false) box.appendChild(el("p", "ov__meta", "This note is too long to change offline."));
        }
        box.appendChild(link(url("notes"), "‹ All notes", "ov__back"));
        return box;
    }

    // ---------- Fitness ----------
    function fitness(data) {
        if (!data.food && !data.weights && !data.planner) return null;
        var wrap = el("div", "ov__stack");
        var planned = data.planner ? data.planner.items.filter(function (o) {
            return o.date === data.planner.start && (o.workout || o.weigh_in);
        }) : [];
        if (planned.length) {
            var pbox = section("Planned today"), ul = el("ul", "ov__list");
            planned.forEach(function (o) {
                var li = keyed(el("li", "ov__item" + (o.done ? " is-done" : "")), occKey(o));
                var main = el("div", "ov__main");
                main.appendChild(el("span", "ov__title", o.title));
                if (o.skipped) main.appendChild(el("span", "ov__meta", "Skipped"));
                else if (o.done) main.appendChild(el("span", "ov__meta", "Done"));
                li.appendChild(main);
                if (canEdit() && !o.weigh_in) {
                    var acts = el("div", "ov__acts");
                    acts.appendChild(button(o.done ? "Reopen" : "Done", "btn btn-primary btn-sm", function () {
                        edit("planner.check", {target: o.id, key: occKey(o), payload: {date: o.occurrence, done: !o.done},
                                               label: (o.done ? "Reopen " : "Done: ") + quoted(o.title)});
                    }));
                    if (!o.done) acts.appendChild(button("Skip", "btn btn-secondary btn-sm", function () {
                        edit("planner.skip", {target: o.id, key: occKey(o), payload: {date: o.occurrence}, label: "Skip " + quoted(o.title)});
                    }));
                    li.appendChild(acts);
                }
                ul.appendChild(li);
            });
            pbox.appendChild(ul);
            wrap.appendChild(pbox);
        }
        if (data.food) {
            var box = section("Food today", data.food.calories + " kcal · " + data.food.protein + " g protein");
            if (!data.food.entries.length) empty(box, "Nothing logged today.");
            else {
                var fl = el("ul", "ov__list");
                data.food.entries.forEach(function (f) {
                    var li = keyed(el("li", "ov__item"), "food:" + f.id), main = el("div", "ov__main");
                    main.appendChild(el("span", "ov__title", f.label));
                    main.appendChild(el("span", "ov__meta", f.slot + (f.calories != null ? " · " + f.calories + " kcal" : "")));
                    li.appendChild(main);
                    fl.appendChild(li);
                });
                box.appendChild(fl);
            }
            if (canEdit()) {
                var label = input("text", "", {placeholder: "What you ate", "aria-label": "Food", maxlength: "200"});
                var kcal = input("number", "", {placeholder: "kcal", "aria-label": "Calories", min: "0", step: "1", inputmode: "numeric"});
                var protein = input("number", "", {placeholder: "Protein g", "aria-label": "Protein (g)", min: "0", step: "1", inputmode: "numeric"});
                var slot = document.createElement("select");
                slot.className = "ov__input";
                slot.setAttribute("aria-label", "Meal");
                (data.food.slots || []).forEach(function (s) { var o = el("option", "", s[1]); o.value = s[0]; slot.appendChild(o); });
                var hour = new Date().getHours();
                slot.value = hour < 11 ? "breakfast" : hour < 15 ? "lunch" : hour < 21 ? "dinner" : "snack";
                box.appendChild(form("ov__add ov__add--food", [label, kcal, protein, slot], "Log", function () {
                    if (!kcal.value && !protein.value) { kcal.focus(); toast("Enter calories or protein."); return; }
                    var id = uuid(), name = label.value.trim() || "Quick add";
                    edit("food.log", {client_id: id, key: "food:" + id, label: "Log " + quoted(name),
                                      payload: {date: data.food.date, slot: slot.value, label: name, calories: kcal.value, protein: protein.value}});
                }));
            }
            wrap.appendChild(box);
        }
        if (data.weights) {
            var wbox = section("Recent weights");
            if (canEdit()) {
                var w = input("number", "", {placeholder: "Weight", "aria-label": "Weight (" + data.weights.unit + ")", step: "0.1", min: "1", inputmode: "decimal"});
                var on = data.food ? data.food.date : (data.planner ? data.planner.start : iso(new Date()));
                wbox.appendChild(form("ov__add", [w, el("span", "ov__unit", data.weights.unit)], "Log weight", function () {
                    if (!w.value) { w.focus(); return; }
                    edit("weight.log", {client_id: uuid(), key: "weight:" + on, payload: {date: on, weight: w.value, unit: data.weights.unit},
                                        label: "Log " + w.value + " " + data.weights.unit});
                }));
            }
            if (!data.weights.entries.length) empty(wbox, "No weigh-ins yet.");
            else {
                var wl = el("ul", "ov__list");
                data.weights.entries.forEach(function (x) {
                    var li = keyed(el("li", "ov__item ov__item--row"), "weight:" + x.date);
                    li.appendChild(el("span", "ov__title", x.weight + " " + data.weights.unit));
                    li.appendChild(el("span", "ov__meta", day(x.date, {month: "short", day: "numeric"})));
                    wl.appendChild(li);
                });
                wbox.appendChild(wl);
            }
            wrap.appendChild(wbox);
        }
        if (canEdit()) {
            var qbox = section("Log a workout");
            var text = input("textarea", "", {rows: "2", placeholder: "bench 185x8,8,7, run 3.1mi 28:30", "aria-label": "What you did"});
            qbox.appendChild(form("ov__add ov__add--stack", [text], "Log workout", function () {
                var v = text.value.trim();
                if (!v) { text.focus(); return; }
                edit("workout.quick", {client_id: uuid(), payload: {text: v}, label: "Log workout: " + v.slice(0, 60)})
                    .then(function () { toast("Workout saved - it syncs when you're back online."); });
            }));
            wrap.appendChild(qbox);
        }
        return wrap;
    }

    var DRAW = {today: today, week: week, meals: meals, groceries: groceries, recipes: recipes, recipe: recipe,
                notes: notes, note: note, fitness: fitness};
    var TITLES = {today: "Today", week: "This week", meals: "Meals", groceries: "Groceries", recipes: "Recipes",
                  recipe: "Recipe", notes: "Notes", note: "Note", fitness: "Fitness"};
    function draw() {
        if (!snap || !snap.data || !r) return;
        var part = DRAW[r.name](snap.data, r.id);
        if (!part) {
            out.hidden = true;
            card.hidden = false;
            document.getElementById("offline-text").textContent = r.name === "recipe" || r.name === "note"
                ? "That one isn't in your offline copy. These work without a connection:"
                : "That section isn't saved for you. These work without a connection:";
            return;
        }
        document.title = TITLES[r.name] + " (offline) · Albright Labs";
        var y = window.scrollY;
        out.textContent = "";
        out.appendChild(nav(r.name === "recipe" ? "recipes" : r.name === "note" ? "notes" : r.name));
        out.appendChild(part);
        out.appendChild(el("p", "ov__foot", canEdit()
            ? "Saved " + window.AlbrightOffline.stamp(snap.saved_at) + ". Changes you make here sync when you're back online; ones that haven't yet have a dot."
            : "Read-only copy saved " + window.AlbrightOffline.stamp(snap.saved_at) + ". Changes are available when you're back online."));
        out.hidden = false;
        card.hidden = true;
        view.classList.add("ov--data");
        marks();
        window.scrollTo(0, y);
    }
    function typing() {
        var a = document.activeElement;
        return a && out.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName);
    }
    if (!window.AlbrightOffline) return;
    window.AlbrightOffline.get().then(function (s) {
        if (!s || !s.data) {
            document.getElementById("offline-text").textContent =
                "Nothing is saved on this device yet - open the site once while you're online. These sections work offline after that:";
            return;
        }
        snap = s;
        draw();
    });
    document.addEventListener("albright:outbox", marks);
    document.addEventListener("albright:snapshot", function (e) {
        if (!e.detail || typing()) return;              // don't pull the text out from under someone typing
        snap = e.detail;
        draw();
    });
})();
