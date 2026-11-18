/* The logged-out pages (templates/base_public_form.html): show/hide password buttons. Each button is in the
   markup, hidden, with aria-controls naming its input; this shows it and flips the input between password and
   text. Without JavaScript the buttons stay hidden and the fields work as plain password fields. */
(function () {
    "use strict";
    var buttons = document.querySelectorAll("[data-password-toggle]");
    Array.prototype.forEach.call(buttons, function (button) {
        var input = document.getElementById(button.getAttribute("aria-controls"));
        if (!input) return;
        button.hidden = false;
        button.addEventListener("click", function () {
            var show = input.type === "password";
            input.type = show ? "text" : "password";
            button.textContent = show ? "Hide" : "Show";
            button.setAttribute("aria-label", show ? "Hide password" : "Show password");
            input.focus();
        });
        if (input.form) {                       // never submit (or let the browser remember) the field as text
            input.form.addEventListener("submit", function () { input.type = "password"; });
        }
    });
})();
