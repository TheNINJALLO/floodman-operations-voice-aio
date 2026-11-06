(function () {
  "use strict";
  let callId = "";
  let running = false;
  let busy = false;
  const chat = document.getElementById("chat");
  const form = document.getElementById("sim-form");
  const input = document.getElementById("sim-input");
  const submit = form.querySelector('button[type="submit"]');
  const start = document.getElementById("reset");
  const status = document.querySelector("[data-sim-status]");
  const stage = document.querySelector("[data-sim-stage]");
  const error = document.querySelector("[data-sim-error]");
  const record = document.querySelector("[data-sim-record]");
  const scenarios = [...document.querySelectorAll("[data-sim-scenario]")];
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  function controls() {
    input.disabled = !running || busy;
    submit.disabled = !running || busy;
    start.disabled = busy;
    scenarios.forEach((button) => { button.disabled = !running || busy; });
    form.setAttribute("aria-busy", String(busy));
  }

  function add(role, text) {
    if (!text) return;
    const element = document.createElement("div");
    element.className = `message ${role}`;
    const label = document.createElement("strong");
    label.textContent = role === "caller" ? "Test caller" : "Avery";
    const body = document.createElement("p");
    body.textContent = text;
    element.append(label, body);
    chat.appendChild(element);
    chat.scrollTop = chat.scrollHeight;
  }

  async function send(text = "", reset = false) {
    if (busy) return;
    busy = true;
    error.hidden = true;
    status.textContent = text ? "Avery is responding…" : "Starting test…";
    controls();
    try {
      const response = await fetch("/api/simulate", {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ call_uuid: callId, text, reset }),
        signal: AbortSignal.timeout(30000),
      });
      if (!response.ok) {
        const failure = await response.json().catch(() => ({}));
        throw new Error(typeof failure.detail === "string" ? failure.detail : "The simulator could not respond. Start a new test to try again.");
      }
      const data = await response.json();
      const ended = Boolean(data.end_call || data.transfer_number);
      callId = ended ? "" : data.call_uuid;
      running = !ended;
      if (!text) chat.replaceChildren();
      else add("caller", text);
      input.value = "";
      add("assistant", data.reply);
      status.textContent = data.transfer_number ? "Test ended: would transfer to the emergency contact" : data.end_call ? "Test ended: conversation complete" : data.wait_for_caller ? "Waiting for the caller to finish" : "Ready for the caller";
      stage.textContent = `Current step: ${data.stage.replaceAll("_", " ")}`;
      document.querySelectorAll("[data-sim-field]").forEach((element) => {
        element.textContent = data.snapshot[element.dataset.simField] || "Not captured";
      });
      record.href = `/calls/${data.call_id}`;
      record.hidden = false;
      start.textContent = "Start new test";
    } catch (failure) {
      error.textContent = failure.name === "TimeoutError" ? "The test took too long. Start a new test before retrying." : failure.message || "The test is unavailable. Start a new test to try again.";
      error.hidden = false;
      status.textContent = "Test needs a restart";
      running = false;
      callId = "";
      start.textContent = "Start new test";
    } finally {
      busy = false;
      controls();
      if (running) input.focus();
      else start.focus();
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (running && value) send(value);
  });
  start.addEventListener("click", () => send("", Boolean(callId)));
  scenarios.forEach((button) => button.addEventListener("click", () => {
    input.value = button.dataset.simScenario;
    input.focus();
  }));
  controls();
})();
