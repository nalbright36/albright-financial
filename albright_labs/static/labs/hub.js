/*
 * Home (albright_labs/hub.html): each tile's live number is fetched on its
 * own (labs:tile), so one slow or failing source never blocks the others.
 * Vanilla JS. The landing-page dropdown only matters for "A specific page".
 */
(function () {
    "use strict";

    document.querySelectorAll("[data-tile-url]").forEach(function (tile) {
        var value = tile.querySelector("[data-tile-value]");
        var note = tile.querySelector("[data-tile-note]");
        function show(text, detail, tone, size) {
            value.classList.toggle("is-text", size === "text");   // a name (Meals' dinner), not a number
            value.textContent = text;
            note.textContent = detail || "";
            tile.classList.toggle("is-unavailable", text === "—");
            ["gain", "loss", "warn"].forEach(function (t) { value.classList.toggle(t, t === tone); });
        }
        fetch(tile.dataset.tileUrl, {headers: {"X-Requested-With": "XMLHttpRequest"}, credentials: "same-origin"})
            .then(function (r) { return r.ok ? r.json() : {ok: false, note: "Not available right now"}; })
            .then(function (data) {
                if (data.ok) show(data.value, data.note, data.tone, data.size);
                else show("—", data.note);
            })
            .catch(function () { show("—", "Not available right now"); });
    });

    var form = document.getElementById("landing-form");
    if (form) {
        var select = form.querySelector("select");
        function sync() {
            var picked = form.querySelector('input[name="landing"]:checked');
            if (select) select.disabled = !picked || picked.value !== "page";
        }
        form.addEventListener("change", sync);
        sync();
    }
})();
