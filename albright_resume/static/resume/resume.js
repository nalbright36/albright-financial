// Public resume chatbot. Replies are inserted with textContent only, so no
// HTML from the server or the model is ever interpreted.
(function () {
  "use strict";
  var form = document.getElementById("rs-chat-form");
  if (!form) return;
  var log = document.getElementById("rs-chat-log");
  var input = document.getElementById("rs-chat-input");
  var send = document.getElementById("rs-chat-send");
  var count = document.getElementById("rs-chat-count");
  var starters = document.getElementById("rs-starters");
  var max = parseInt(form.getAttribute("data-max"), 10) || 500;
  var endpoint = form.getAttribute("data-endpoint");
  var csrf = form.querySelector("input[name=csrfmiddlewaretoken]").value;
  var honeypot = form.querySelector("input[name=website]");
  var busy = false, closed = false;

  function add(text, cls) {
    var el = document.createElement("div");
    el.className = "rs-msg " + cls;
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }
  function updateCount() { count.textContent = input.value.length + "/" + max; }
  function lock(on) { busy = on; send.disabled = on || closed; input.disabled = closed; }
  function closeChat(text) { closed = true; lock(false); if (starters) starters.hidden = true; if (text) add(text, "rs-msg--note"); }

  function ask(question) {
    question = (question || "").trim();
    if (!question || busy || closed) return;
    if (question.length > max) { add("Please keep questions under " + max + " characters.", "rs-msg--note"); return; }
    add(question, "rs-msg--me");
    input.value = ""; updateCount();
    if (starters) starters.hidden = true;
    var typing = add("", "rs-msg--bot rs-msg--typing");
    lock(true);
    fetch(endpoint, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify({ message: question, website: honeypot ? honeypot.value : "" })
    }).then(function (r) {
      return r.json().catch(function () { return { error: r.status === 403 ? "Please refresh the page and try again." : "Something went wrong. Please try again." }; });
    }).then(function (data) {
      typing.remove();
      if (data.reply) add(data.reply, data.ok === false ? "rs-msg--note" : "rs-msg--bot");
      else add(data.error || "Something went wrong. Please try again.", "rs-msg--note");
      if (data.unavailable) closeChat();
      else if (data.ok && data.remaining === 0) closeChat("That's the question limit for this conversation. Please use Contact me to reach Nathan directly.");
      else if (data.limit && data.remaining === 0) closeChat();
    }).catch(function () {
      typing.remove();
      add("Couldn't reach the assistant. Check your connection and try again.", "rs-msg--note");
    }).then(function () { lock(false); if (!closed) input.focus(); });
  }

  form.addEventListener("submit", function (e) { e.preventDefault(); ask(input.value); });
  input.addEventListener("input", updateCount);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input.value); }
  });
  if (starters) starters.addEventListener("click", function (e) {
    var b = e.target.closest(".rs-starter"); if (b) ask(b.getAttribute("data-q"));
  });
  updateCount();
})();
