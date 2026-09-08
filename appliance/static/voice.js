(function () {
  "use strict";
  const form = document.querySelector("[data-voice-form]");
  if (!form) return;
  const select = form.querySelector("[data-voice-select]");
  const speed = form.querySelector("[data-speed-range]");
  const speedOutput = form.querySelector("[data-speed-output]");
  const name = form.querySelector("[data-voice-name]");
  const meta = form.querySelector("[data-voice-meta]");
  const preview = document.querySelector("[data-voice-preview]");
  const previewText = document.querySelector("[data-preview-text]");
  const status = document.querySelector("[data-preview-status]");
  const audio = document.querySelector("[data-voice-audio]");
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  let objectUrl = "";

  function refresh() {
    const option = select.selectedOptions[0];
    speedOutput.value = Number(speed.value).toFixed(2);
    name.textContent = option ? option.textContent.split(" — ")[0] : "Unavailable";
    meta.textContent = option ? `${option.dataset.accent} · ${option.dataset.gender} · ${speedOutput.value}× speed` : "No installed voices found";
  }

  async function generatePreview() {
    try {
      preview.disabled = true;
      preview.textContent = "Generating…";
      status.textContent = "Creating the preview locally on the appliance…";
      const response = await fetch("/api/voice/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ voice: select.value, speed: Number(speed.value), text: previewText.value.trim() })
      });
      if (!response.ok) {
        let message = "The preview could not be generated.";
        try { message = (await response.json()).detail || message; } catch (_) {}
        throw new Error(message);
      }
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = URL.createObjectURL(await response.blob());
      audio.src = objectUrl;
      audio.classList.remove("hidden");
      status.textContent = "Preview ready. This has not changed the live-call voice.";
      await audio.play().catch(() => {});
    } catch (error) {
      status.textContent = error.message || "The preview could not be generated.";
    } finally {
      preview.disabled = false;
      preview.textContent = "▶ Generate preview";
    }
  }

  select.addEventListener("change", refresh);
  speed.addEventListener("input", refresh);
  preview.addEventListener("click", generatePreview);
  window.addEventListener("beforeunload", () => { if (objectUrl) URL.revokeObjectURL(objectUrl); });
  refresh();
})();
