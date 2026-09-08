self.addEventListener("push", (event) => {
  let data = { title: "Floodman call update", body: "Open the secure dashboard for details.", url: "/notifications", kind: "update" };
  try { data = { ...data, ...event.data.json() }; } catch (_) {}
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: "/static/icon.svg",
    badge: "/static/icon.svg",
    tag: `floodman-${data.kind}-${data.notification_id || "event"}`,
    renotify: data.kind === "emergency",
    data: { url: data.url || "/notifications" }
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/notifications", self.location.origin).href;
  event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
    for (const client of windows) {
      if (client.url.startsWith(self.location.origin) && "focus" in client) {
        client.navigate(target);
        return client.focus();
      }
    }
    return clients.openWindow ? clients.openWindow(target) : undefined;
  }));
});
