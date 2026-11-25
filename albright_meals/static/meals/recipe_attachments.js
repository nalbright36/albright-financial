/* Recipe photos (Option C). The swipe viewer, multi-file upload, HEIC conversion and delete-confirm
   live in labs/attachments.js (the recipe article is [data-att]). This file closes the Change cover picker. */
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
})();
