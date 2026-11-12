/* The add-place field: open it from the chip row, focus the input. */
(function () {
    var form = document.getElementById("wx-add");
    var open = document.getElementById("wx-add-open");
    var input = form ? form.querySelector("input[name=q]") : null;
    if (!form || !open) return;
    open.addEventListener("click", function () {
        form.hidden = false;
        open.setAttribute("hidden", "");
        if (input) input.focus();
    });
})();
