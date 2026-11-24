// The offline page (albright_labs/offline.html): /sw.js shows it, at the page's own address, for anything it has no
// saved copy of. This draws that section read-only from the IndexedDB snapshot (labs/offline.js, AlbrightOffline.get):
//   Today (Home, Planner, Day - ?date= picks a day in the 8 saved), Week, Meals (the week's plan), Groceries, Recipes and
//   one recipe, Notes and one note, Fitness (today's food, recent weights). Anything else - or no snapshot - keeps the
//   card with the links to what works. Everything is text (textContent) - nothing from the data is ever HTML.
(function () {
    var view = document.getElementById("offline-view");
    if (!view) return;
    var out = document.getElementById("offline-snapshot"), card = document.getElementById("offline-card");
    var retry = document.getElementById("offline-retry");
    if (retry) retry.addEventListener("click", function () { window.location.reload(); });
    var url = function (name) { return view.getAttribute("data-" + name); };
    var path = window.location.pathname;
    if (path === url("offline") || path === "/offline/") return;            // the page itself: just the card

    function route() {
        var m;
        if (path === "/" || path === "/home/" || path === url("planner") || path === url("today")) return {name: "today"};
        if (path === url("week")) return {name: "week"};
        if (path === url("groceries")) return {name: "groceries"};
        if (path === url("meals")) return {name: "meals"};
        if (path === url("recipes")) return {name: "recipes"};
        if ((m = path.match(/^(.*\/recipes\/)(\d+)\/$/)) && m[1] === url("recipes")) return {name: "recipe", id: +m[2]};
        if (path === url("notes")) return {name: "notes"};
        if ((m = path.match(/^(.*\/)(\d+)\/$/)) && m[1] === url("notes")) return {name: "note", id: +m[2]};
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
    function check(done) {
        var box = el("span", "ov__check" + (done ? " is-done" : ""));
        box.setAttribute("aria-hidden", "true");
        box.textContent = done ? "✓" : "";
        return box;
    }
    function nav(active) {
        var bar = el("nav", "ov__nav");
        bar.setAttribute("aria-label", "Saved for offline");
        [["today", "Today", url("today")], ["week", "Week", url("week")], ["meals", "Meals", url("meals")],
         ["groceries", "Groceries", url("groceries")], ["recipes", "Recipes", url("recipes")],
         ["notes", "Notes", url("notes")]].forEach(function (s) {
            var a = link(s[2], s[1], "ov__tab" + (s[0] === active ? " is-on" : ""));
            if (s[0] === active) a.setAttribute("aria-current", "page");
            bar.appendChild(a);
        });
        return bar;
    }

    // ---------- the sections ----------
    function items(list) {
        var ul = el("ul", "ov__list");
        list.forEach(function (o) {
            var li = el("li", "ov__item" + (o.done ? " is-done" : ""));
            if (o.checkable || o.kind === "task") li.appendChild(check(o.done));
            else li.appendChild(el("span", "ov__dot"));
            var main = el("div", "ov__main");
            main.appendChild(el("span", "ov__title", o.title));
            var when = o.start ? time(o.start) + (o.end ? " – " + time(o.end) : "") : "";
            if (when || o.skipped) main.appendChild(el("span", "ov__meta", [when, o.skipped ? "Skipped" : ""].filter(Boolean).join(" · ")));
            if (o.checklist && o.checklist.length) {
                var sub = el("ul", "ov__checklist");
                o.checklist.forEach(function (r) {
                    var row = el("li", r.done ? "is-done" : "");
                    row.appendChild(check(r.done));
                    row.appendChild(el("span", "", r.text));
                    sub.appendChild(row);
                });
                main.appendChild(sub);
            }
            li.appendChild(main);
            ul.appendChild(li);
        });
        return ul;
    }
    function today(data) {
        if (!data.planner) return null;
        var q = new URLSearchParams(window.location.search).get("date");
        var on = q && q >= data.planner.start && q <= data.planner.end ? q : data.planner.start;
        var list = data.planner.items.filter(function (o) { return o.date === on; });
        var box = section(on === data.planner.start ? "Today" : day(on), day(on));
        return list.length ? (box.appendChild(items(list)), box) : empty(box, "Nothing planned.");
    }
    function week(data) {
        if (!data.planner) return null;
        var wrap = el("div", "ov__stack");
        var d = data.planner.start, guard = 0;
        while (d <= data.planner.end && guard++ < 10) {
            var list = data.planner.items.filter(function (o) { return o.date === d; });
            var box = section(day(d));
            wrap.appendChild(list.length ? (box.appendChild(items(list)), box) : empty(box, "Nothing planned."));
            var p = d.split("-"), next = new Date(+p[0], +p[1] - 1, +p[2] + 1);
            d = next.getFullYear() + "-" + String(next.getMonth() + 1).padStart(2, "0") + "-" + String(next.getDate()).padStart(2, "0");
        }
        return wrap;
    }
    function meals(data) {
        if (!data.meal_plan) return null;
        var plan = data.meal_plan, wrap = el("div", "ov__stack");
        var dates = [];
        plan.entries.forEach(function (e) { if (dates.indexOf(e.date) < 0) dates.push(e.date); });
        if (!dates.length) return empty(section("This week's meals"), "Nothing planned this week.");
        dates.sort().forEach(function (d) {
            var box = section(day(d)), ul = el("ul", "ov__list");
            plan.entries.filter(function (e) { return e.date === d; }).forEach(function (e) {
                var li = el("li", "ov__item");
                var main = el("div", "ov__main");
                var recipe = e.recipe_id && (data.recipes || []).some(function (r) { return r.id === e.recipe_id; });
                main.appendChild(recipe ? link(url("recipes") + e.recipe_id + "/", e.title, "ov__title") : el("span", "ov__title", e.title));
                main.appendChild(el("span", "ov__meta", e.slot));
                li.appendChild(main);
                ul.appendChild(li);
            });
            box.appendChild(ul);
            wrap.appendChild(box);
        });
        return wrap;
    }
    function groceries(data) {
        if (!data.groceries) return null;
        var box = section("Groceries", data.space || "");
        if (!data.groceries.length) return empty(box, "The list is empty.");
        var aisles = [];
        data.groceries.forEach(function (i) { var a = i.aisle || "Other"; if (aisles.indexOf(a) < 0) aisles.push(a); });
        aisles.forEach(function (a) {
            if (aisles.length > 1) box.appendChild(el("h3", "ov__h", a));
            var ul = el("ul", "ov__list");
            data.groceries.filter(function (i) { return (i.aisle || "Other") === a; }).forEach(function (i) {
                var li = el("li", "ov__item" + (i.checked ? " is-done" : ""));
                li.appendChild(check(i.checked));
                var main = el("div", "ov__main");
                main.appendChild(el("span", "ov__title", [i.quantity, i.unit, i.name].filter(Boolean).join(" ")));
                if (i.note) main.appendChild(el("span", "ov__meta", i.note));
                li.appendChild(main);
                ul.appendChild(li);
            });
            box.appendChild(ul);
        });
        return box;
    }
    function recipes(data) {
        if (!data.recipes) return null;
        var box = section("Recipes", data.recipes.length + " saved");
        if (!data.recipes.length) return empty(box, "No recipes yet.");
        var ul = el("ul", "ov__list");
        data.recipes.forEach(function (r) {
            var li = el("li", "ov__item");
            var main = el("div", "ov__main");
            main.appendChild(link(url("recipes") + r.id + "/", r.title, "ov__title"));
            var meta = [r.rating, r.minutes ? r.minutes + " min" : ""].filter(Boolean).join(" · ");
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
        var r = (data.recipes || []).filter(function (x) { return x.id === id; })[0];
        if (!r) return null;
        var wrap = el("div", "ov__stack");
        var head = section(r.title, [r.rating, r.servings ? "Serves " + r.servings : "", r.minutes ? r.minutes + " min" : ""].filter(Boolean).join(" · "));
        if (r.description) head.appendChild(el("p", "ov__text", r.description));
        head.appendChild(link(url("recipes"), "‹ All recipes", "ov__back"));
        wrap.appendChild(head);
        var ing = section("Ingredients"), ul = el("ul", "ov__bullets");
        r.ingredients.forEach(function (i) { ul.appendChild(el("li", "", i)); });
        if (r.ingredients.length) ing.appendChild(ul); else empty(ing, "None listed.");
        wrap.appendChild(ing);
        var how = section("Steps");
        if (r.steps.length) steps(r.steps, how); else empty(how, "None listed.");
        wrap.appendChild(how);
        return wrap;
    }
    function notes(data) {
        if (!data.notes) return null;
        var box = section("Notes", data.notes.length + " saved");
        if (!data.notes.length) return empty(box, "No notes yet.");
        var ul = el("ul", "ov__list");
        data.notes.forEach(function (n) {
            var li = el("li", "ov__item");
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
    function note(data, id) {
        var n = (data.notes || []).filter(function (x) { return x.id === id; })[0];
        if (!n) return null;
        var box = section(n.title || "Untitled", n.tags && n.tags.length ? "#" + n.tags.join(" #") : "");
        box.appendChild(el("div", "ov__note", n.body || ""));
        box.appendChild(link(url("notes"), "‹ All notes", "ov__back"));
        return box;
    }
    function fitness(data) {
        if (!data.food && !data.weights) return null;
        var wrap = el("div", "ov__stack");
        if (data.food) {
            var box = section("Food today", data.food.calories + " kcal · " + data.food.protein + " g protein");
            if (!data.food.entries.length) empty(box, "Nothing logged today.");
            else {
                var ul = el("ul", "ov__list");
                data.food.entries.forEach(function (f) {
                    var li = el("li", "ov__item"), main = el("div", "ov__main");
                    main.appendChild(el("span", "ov__title", f.label));
                    main.appendChild(el("span", "ov__meta", f.slot + (f.calories != null ? " · " + f.calories + " kcal" : "")));
                    li.appendChild(main);
                    ul.appendChild(li);
                });
                box.appendChild(ul);
            }
            wrap.appendChild(box);
        }
        if (data.weights) {
            var wbox = section("Recent weights");
            if (!data.weights.entries.length) empty(wbox, "No weigh-ins yet.");
            else {
                var wl = el("ul", "ov__list");
                data.weights.entries.forEach(function (w) {
                    var li = el("li", "ov__item ov__item--row");
                    li.appendChild(el("span", "ov__title", w.weight + " " + data.weights.unit));
                    li.appendChild(el("span", "ov__meta", day(w.date, {month: "short", day: "numeric"})));
                    wl.appendChild(li);
                });
                wbox.appendChild(wl);
            }
            wrap.appendChild(wbox);
        }
        return wrap;
    }

    var DRAW = {today: today, week: week, meals: meals, groceries: groceries, recipes: recipes, recipe: recipe,
                notes: notes, note: note, fitness: fitness};
    var TITLES = {today: "Today", week: "This week", meals: "Meals", groceries: "Groceries", recipes: "Recipes",
                  recipe: "Recipe", notes: "Notes", note: "Note", fitness: "Fitness"};
    var r = route();
    if (!window.AlbrightOffline) return;
    window.AlbrightOffline.get().then(function (snap) {
        if (!snap || !snap.data) {
            document.getElementById("offline-text").textContent =
                "Nothing is saved on this device yet - open the site once while you're online. These sections work offline after that:";
            return;
        }
        if (!r) return;
        var part = DRAW[r.name](snap.data, r.id);
        if (!part) {
            document.getElementById("offline-text").textContent = r.name === "recipe" || r.name === "note"
                ? "That one isn't in your offline copy. These work without a connection:"
                : "That section isn't saved for you. These work without a connection:";
            return;
        }
        document.title = TITLES[r.name] + " (offline) · Albright Labs";
        out.appendChild(nav(r.name === "recipe" ? "recipes" : r.name === "note" ? "notes" : r.name));
        out.appendChild(part);
        out.appendChild(el("p", "ov__foot", "Read-only copy saved " + window.AlbrightOffline.stamp(snap.saved_at) + ". Changes are available when you're back online."));
        out.hidden = false;
        card.hidden = true;
        view.classList.add("ov--data");
    });
})();
