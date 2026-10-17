/*
 * Lot photo lightbox (_lightbox.html): clicking any .js-lightbox element
 * (a thumbnail, or the review queue's large photo) opens its
 * data-full-src in a native <dialog>. Esc closes it (the browser's own
 * dialog behavior), as does a click anywhere on it. Delegated from the
 * document, so markup swapped in later works without re-wiring. Vanilla
 * JS, no libraries.
 */
(function () {
    "use strict";

    var dialog = document.getElementById("lightbox");
    if (!dialog || typeof dialog.showModal !== "function") return;
    var img = document.getElementById("lightbox-img");
    var caption = document.getElementById("lightbox-caption");

    function open(src, text) {
        img.src = src;
        img.alt = text ? "Photo of " + text : "Lot photo";
        caption.textContent = text || "";
        if (!dialog.open) dialog.showModal();
    }

    document.addEventListener("click", function (event) {
        var trigger = event.target.closest(".js-lightbox");
        if (!trigger || !trigger.dataset.fullSrc) return;
        event.preventDefault();
        open(trigger.dataset.fullSrc, trigger.dataset.caption);
    });
    dialog.addEventListener("click", function () { dialog.close(); });
    dialog.addEventListener("close", function () { img.removeAttribute("src"); });

    window.lotLightbox = {open: open, isOpen: function () { return dialog.open; }};
})();
