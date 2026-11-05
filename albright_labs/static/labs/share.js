/*
 * The Share control (albright_labs/_share.html) - on every page that has one. Vanilla JS.
 *  - Adding, changing View / Edit and removing post to the control's own URL (labs.views.share) and swap in the
 *    control it sends back - no reload. Without JS the forms just post.
 *  - "A person…" shows the username / email box; a circle hides it. New forms: the same for share_mode.
 */
(function () {
    "use strict";

    function toggleWho(select, input) {
        if (!select || !input) return;
        input.hidden = select.value !== "person";
        if (!input.hidden) input.focus();
    }

    document.addEventListener("change", function (event) {
        var t = event.target;
        if (t.matches && t.matches("[data-share-target]")) toggleWho(t, t.form.querySelector("[data-share-who]"));
        if (t.matches && t.matches("[data-share-mode]")) {
            var box = t.closest("[data-share-new]");
            toggleWho(t, box && box.querySelector("[data-share-new-who]"));
        }
        if (t.matches && t.matches("[data-share-filter]")) t.form.submit();           // Shared with me's filters
        if (t.matches && t.matches("[data-share-autosave]")) {
            if (t.form.requestSubmit) t.form.requestSubmit(); else t.form.dispatchEvent(new Event("submit", {cancelable: true}));
        }
    });

    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches || !form.matches("[data-share-form]")) return;
        event.preventDefault();
        var box = form.closest("[data-share]");
        fetch(form.action, {
            method: "POST", body: new FormData(form), credentials: "same-origin",
            headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (data.html && box) {
                var holder = document.createElement("div");
                holder.innerHTML = data.html;
                var fresh = holder.firstElementChild;
                box.replaceWith(fresh);
                box = fresh;
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
