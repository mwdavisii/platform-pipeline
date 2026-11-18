// The agent's chat page: the conversation lives here (the runtime keeps no state), sent whole to POST /chat on each turn.
// Text is written with textContent only, never as HTML.
(function () {
  "use strict";
  var history = [];
  function add(role, text, tools) {
    var item = document.createElement("li");
    item.className = role;
    var p = document.createElement("p");
    p.textContent = text;
    item.appendChild(p);
    if (tools && tools.length) {
      var used = document.createElement("p");
      used.className = "tools";
      used.textContent = "Tools: " + tools.map(function (t) { return t.tool + (t.ok ? "" : " (failed)"); }).join(", ");
      item.appendChild(used);
    }
    document.getElementById("messages").appendChild(item);
  }
  function busy(on) {
    Array.prototype.forEach.call(document.querySelectorAll("button"), function (button) { button.disabled = on; });
  }
  function send(text) {
    history.push({role: "user", content: text});
    add("user", text);
    busy(true);
    fetch("/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({messages: history})})
      .then(function (r) { return r.json().then(function (doc) { return {ok: r.ok, doc: doc}; }); })
      .then(function (answer) {
        if (answer.ok) {
          history.push({role: "assistant", content: answer.doc.reply});
          add("assistant", answer.doc.reply, answer.doc.tool_calls);
        } else {
          history.pop();
          add("error", answer.doc.error || "Something went wrong.");
        }
      })
      .catch(function () { history.pop(); add("error", "The agent did not answer. Try again."); })
      .then(function () { busy(false); });
  }
  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("ask"), text = document.getElementById("text");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (text.value.trim()) { send(text.value.trim()); text.value = ""; }
    });
    Array.prototype.forEach.call(document.querySelectorAll("button.starter"), function (button) {
      button.addEventListener("click", function () { send(button.textContent); });
    });
  });
}());
