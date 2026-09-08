(function () {
  "use strict";

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  document.querySelectorAll("[data-menu-toggle]").forEach((control) => {
    control.addEventListener("click", () => {
      const open = document.body.classList.toggle("menu-open");
      document.querySelector(".mobile-menu")?.setAttribute("aria-expanded", String(open));
    });
  });

  document.querySelectorAll("[data-dismiss]").forEach((button) => {
    button.addEventListener("click", () => button.closest(".alert")?.remove());
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm || "Continue?")) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-table-search]").forEach((input) => {
    const table = document.getElementById(input.dataset.tableSearch);
    if (!table) return;
    input.addEventListener("input", () => {
      const query = input.value.trim().toLocaleLowerCase();
      table.querySelectorAll("tbody tr").forEach((row) => {
        row.hidden = Boolean(query) && !row.textContent.toLocaleLowerCase().includes(query);
      });
    });
  });

  document.querySelectorAll(".js-date").forEach((element) => {
    const raw = element.textContent.trim();
    if (!raw || raw === "Never") return;
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.valueOf())) return;
    element.title = raw;
    element.textContent = parsed.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  });

  function base64UrlToBytes(value) {
    const padding = "=".repeat((4 - value.length % 4) % 4);
    const binary = window.atob((value + padding).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  }

  async function pushSetup() {
    const panel = document.querySelector("[data-push-panel]");
    if (!panel) return;
    const enable = panel.querySelector("[data-push-enable]");
    const disable = panel.querySelector("[data-push-disable]");
    const status = panel.querySelector("[data-push-status]");
    const supported = window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
    if (!supported || panel.dataset.pushAvailable !== "true" || panel.dataset.pushSecure !== "true") {
      status.textContent = "Push alerts are not available in this browser or secure connection.";
      enable.disabled = true;
      return;
    }
    const registration = await navigator.serviceWorker.register("/service-worker.js");

    async function refresh() {
      const subscription = await registration.pushManager.getSubscription();
      enable.classList.toggle("hidden", Boolean(subscription));
      disable.classList.toggle("hidden", !subscription);
      status.textContent = subscription ? "Alerts are enabled on this device." : (Notification.permission === "denied" ? "Notifications are blocked in browser settings." : "Alerts are not enabled on this device yet.");
    }

    enable.addEventListener("click", async () => {
      try {
        enable.disabled = true;
        const permission = await Notification.requestPermission();
        if (permission !== "granted") throw new Error("Notification permission was not granted. Check this site's browser permissions.");
        const configResponse = await fetch("/api/push/config", { headers: { "Accept": "application/json" } });
        if (!configResponse.ok) throw new Error("Could not load notification settings.");
        const config = await configResponse.json();
        let subscription = await registration.pushManager.getSubscription();
        if (!subscription) subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64UrlToBytes(config.public_key) });
        const saveResponse = await fetch("/api/push/subscriptions", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify(subscription.toJSON()) });
        if (!saveResponse.ok) throw new Error("The browser subscribed, but the server could not save this device.");
        await refresh();
      } catch (error) {
        status.textContent = error.message || "Could not enable alerts.";
      } finally {
        enable.disabled = false;
      }
    });

    disable.addEventListener("click", async () => {
      try {
        disable.disabled = true;
        const subscription = await registration.pushManager.getSubscription();
        if (subscription) {
          const response = await fetch("/api/push/subscriptions", { method: "DELETE", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify({ endpoint: subscription.endpoint }) });
          if (!response.ok) throw new Error("The server could not remove this device.");
          await subscription.unsubscribe();
        }
        await refresh();
      } catch (error) {
        status.textContent = error.message || "Could not disable alerts.";
      } finally {
        disable.disabled = false;
      }
    });
    await refresh();
  }

  function refreshUnread() {
    if (!document.querySelector("[data-unread-count]")) return;
    fetch("/api/notifications/unread", { headers: { "Accept": "application/json" } })
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then(({ count }) => {
        document.querySelectorAll("[data-unread-count]").forEach((element) => { element.textContent = count; element.classList.toggle("hidden", !count); });
        document.querySelectorAll("[data-unread-pip]").forEach((element) => element.classList.toggle("hidden", !count));
      })
      .catch(() => {});
  }

  pushSetup().catch(() => {});
  window.setInterval(refreshUnread, 60000);
})();
