/* Recipe photos (Option C). The swipe viewer, multi-file upload, HEIC conversion and delete-confirm
   live in labs/attachments.js (the recipe article is [data-att]). This file closes the Change cover
   picker, and posts a photo when someone uses the dashed plus beside an ingredient or a step.
   Those plus controls are not <form>s: the grocery list form already wraps the ingredient lines. */
(function () {
    function closeCoverPickers(except) {
        document.querySelectorAll(".ra-cover-details[open]").forEach(function (el) {
            if (el !== except) el.removeAttribute("open");
        });
    }
    document.addEventListener("click", function (event) {
        var details = event.target.closest && event.target.closest(".ra-cover-details");
        if (!details) closeCoverPickers();
    });
    document.addEventListener("toggle", function (event) {
        if (event.target.matches && event.target.matches(".ra-cover-details") && event.target.open) {
            closeCoverPickers(event.target);
        }
    }, true);

    document.addEventListener("click", function (event) {
        var plus = event.target.closest && event.target.closest("[data-ra-plus]");
        if (!plus) return;
        event.preventDefault();
        var box = plus.closest(".ing__addform, .ra-step__form") || plus.parentElement;
        var input = box && box.querySelector("[data-ra-file]");
        if (input) input.click();
    });
    document.addEventListener("change", function (event) {
        var input = event.target.closest && event.target.closest("[data-ra-file]");
        if (!input || !input.files || !input.files.length) return;
        var url = input.getAttribute("data-ra-url");
        if (!url) {
            var form = input.closest("form");
            if (form && form.id !== "ingredient-form") {
                if (form.requestSubmit) form.requestSubmit();
                else form.submit();
            }
            return;
        }
        var body = new FormData();
        body.append("file", input.files[0]);
        body.append("csrfmiddlewaretoken", input.getAttribute("data-ra-csrf") || "");
        body.append("next", window.location.pathname + window.location.search);
        if (input.getAttribute("data-ra-ingredient")) body.append("ingredient", input.getAttribute("data-ra-ingredient"));
        if (input.getAttribute("data-ra-step")) body.append("step", input.getAttribute("data-ra-step"));
        input.disabled = true;
        fetch(url, {method: "POST", body: body, credentials: "same-origin",
                    headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (res) { if (!res.ok) throw new Error("upload failed"); return res.json(); })
            .then(function () { window.location.reload(); })
            .catch(function () { input.disabled = false; input.value = ""; });
    });
})();
