/* Albright service worker: offline fallback for navigations, and Web Push banners. */
{% load static %}
const CACHE = "albright-offline-v1";
const OFFLINE_URL = "{% url 'offline' %}";
const PRECACHE = [
  OFFLINE_URL,
  "{% static 'style.css' %}",
  "{% static 'labs/launcher.css' %}",
  "{% static 'icons/logo-mark-white.png' %}",
  "{% static 'icons/logo-mark-dark.png' %}",
  "{% static 'icons/icon-192.png' %}"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) { return cache.addAll(PRECACHE); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(function () {
        return caches.match(OFFLINE_URL).then(function (cached) { return cached || caches.match(request); });
      })
    );
    return;
  }
  var url = new URL(request.url);
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(request).then(function (cached) {
        return cached || fetch(request).then(function (response) {
          return response;
        });
      })
    );
  }
});

self.addEventListener("push", function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) {}
  var title = data.title || "Albright";
  var options = {
    body: data.body || "",
    data: { url: data.url || "/" },
    icon: "{% static 'icons/icon-192.png' %}",
    badge: "{% static 'icons/favicon-32.png' %}"
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (clients) {
      for (var i = 0; i < clients.length; i++) {
        if ("focus" in clients[i]) return clients[i].focus().then(function (c) { return c && c.navigate ? c.navigate(url) : c; });
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
