// A recipe's public share link in its Share box (albright_meals/_share_link.html; albright_meals.share_link_views):
// "Copy share link" makes the link (or uses the one there is) and copies it; "New link" replaces it; "Turn off" stops
// it. Saves without reloading; without JS each form posts and the page comes back.
(function () {
    "use strict";
    var box = document.querySelector("[data-share-link]");
    if (!box || box.getAttribute("data-ready")) return;
    box.setAttribute("data-ready", "1");
    var row = box.querySelector("[data-share-link-row]");
    var input = box.querySelector("[data-share-link-url]");
    var meta = box.querySelector("[data-share-link-meta]");
    var status = box.querySelector("[data-share-link-status]");

    function say(text) { status.textContent = text; }
    function copy(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return fallback(); });
        }
        return Promise.resolve(fallback());
    }
    function fallback() {
        try { input.focus(); input.select(); return document.execCommand("copy"); } catch (e) { return false; }
    }
    function show(data) {
        row.hidden = !data.active;
        input.value = data.url || "";
        Array.prototype.forEach.call(box.querySelectorAll("[data-share-link-on]"), function (f) { f.hidden = !data.active; });
        meta.textContent = data.active ? (data.views || 0) + (data.views === 1 ? " view" : " views") : "";
    }

    box.addEventListener("submit", function (e) {
        var form = e.target.closest("[data-share-link-form]");
        if (!form) return;
        e.preventDefault();
        var action = form.querySelector('[name="action"]').value;
        if (action === "create" && !row.hidden && input.value) {      // already on: copy straight away (still a click)
            copy(input.value).then(function (ok) { say(ok ? "Share link copied." : "Select the link above to copy it."); });
            return;
        }
        if (action === "regenerate" && !window.confirm("Make a new link? The old one stops working.")) return;
        if (action === "off" && !window.confirm("Turn off this link? Anyone who has it can't open the recipe any more.")) return;
        var button = form.querySelector("button");
        button.disabled = true;
        fetch(form.getAttribute("action"), {method: "POST", body: new FormData(form), credentials: "same-origin",
                            headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function (data) {
                show(data);
                if (!data.active) { say(data.message); return; }
                return copy(data.url).then(function (ok) {
                    say(ok ? (action === "regenerate" ? "New link made and copied. The old one stopped working." : "Share link made and copied.")
                           : data.message + " Select the link above to copy it.");
                });
            })
            .catch(function () { say("That didn't work. Try again."); })
            .then(function () { button.disabled = false; });
    });
})();
