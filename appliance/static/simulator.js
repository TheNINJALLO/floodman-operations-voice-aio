(function () {
  "use strict";
  let callId = "";
  const chat = document.getElementById("chat");
  const form = document.getElementById("sim-form");
  const input = document.getElementById("sim-input");
  const submit = form.querySelector('button[type="submit"]');
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  function add(role, text) {
    const element = document.createElement("div");
    element.className = `message ${role}`;
    const label = document.createElement("strong");
    label.textContent = role === "caller" ? "Caller" : "AI assistant";
    const body = document.createElement("p");
    body.textContent = text;
    element.append(label, body);
    chat.appendChild(element);
    chat.scrollTop = chat.scrollHeight;
  }

  async function send(text = "", reset = false) {
    submit.disabled = true;
    try {
      const response = await fetch("/api/simulate", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify({ call_uuid: callId, text, reset }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "The simulator could not respond.");
      callId = data.call_uuid;
      if (text) add("caller", text);
      add("assistant", data.reply);
    } catch (error) {
      add("assistant", error.message || "The simulator is temporarily unavailable.");
    } finally {
      submit.disabled = false;
      input.focus();
    }
  }

  form.addEventListener("submit", (event) => { event.preventDefault(); const value = input.value.trim(); if (!value) return; input.value = ""; send(value); });
  document.getElementById("reset").addEventListener("click", () => { chat.innerHTML = ""; send("", true); });
  send();
})();
