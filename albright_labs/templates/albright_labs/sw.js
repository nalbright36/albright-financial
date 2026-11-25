/* Albright service worker (push_views.service_worker, served at /sw.js with Service-Worker-Allowed: /).
 *
 * Offline viewing, read-only (albright_labs.offline has the server's half):
 *   shell     the app's CSS/JS/icons and the offline page, saved at install - one cache per version (a hash of every
 *             static file), so a deploy installs a new worker that drops the old caches
 *   static    /static/ and Google Fonts: cache-first (a URL with ?v= never changes; a missing version falls back to
 *             any saved copy of the same file)
 *   pages     signed-in HTML pages: network-first; each one the server marks "cache <user>" (X-Albright-Offline) is
 *             saved; offline, the saved copy is shown, else the offline page (labs/offline_view.js draws Today, Week,
 *             Groceries, Recipes and Notes from the IndexedDB snapshot, or says what's available)
 *   files     attachments up to 5 MB, network-first
 *   never     POSTs, sign-in/out, Admin, API keys, the assistant, any other page the server marks "nocache"
 *   wipe      "anon" (signed out), "off" (My account > Offline data) or a different user id: every saved page, file and
 *             the snapshot are deleted - before anything of the new person's is saved
 *   update    the first worker takes over at once; a newer one waits until the page's "New version available -
 *             Refresh" (labs/offline.js) asks it to (SKIP_WAITING), then the page reloads
 *   edit      offline, the sections that can be changed offline (push_views.edit_paths: Today, Week, Meals, Groceries,
 *             Recipes, Notes, Fitness) always get the offline page - it draws them from the snapshot with every
 *             offline change and lets you make more - rather than a saved copy of the page
 *   sync      Background Sync ("albright-sync", registered by labs/sync.js after a change): an open page is asked to
 *             send the outbox; with none open the worker sends it itself (labs/sync_core.js)
 * And Web Push banners (albright_labs.push).
 */
{% load static %}
try { importScripts("{{ sync_core|escapejs }}"); } catch (e) {}   // the outbox (labs/sync_core.js), for Background Sync
const VERSION = "{{ version }}";
const SHELL = "albright-shell-" + VERSION;
const STATIC = "albright-static-" + VERSION;
const PAGES = "albright-pages";
const FILES = "albright-files";
const META = "albright-meta";
const KEEP = [SHELL, STATIC, PAGES, FILES, META];
const OFFLINE_URL = "{% url 'offline' %}";
const META_URL = "/__albright/offline-meta";
const DB_NAME = "albright-offline";
const MAX_FILE = 5 * 1024 * 1024;
const MAX_PAGES = 200;
const SHELL_FILES = [{% for url in shell %}"{{ url|escapejs }}"{% if not forloop.last %}, {% endif %}{% endfor %}];
const NEVER = ["/user_login", "/user_logout", "/admin/", "/control/", "/account/", "/assistant/", "/bot-api/", "/api/",
               "/oauth", "/.well-known/", "/reset", "/offline/", "/sw.js", "/invite", "/signup", "/__albright/"];

const EDIT = {{ edit_paths_json|safe }};

function never(path) { return NEVER.some(function (p) { return path.indexOf(p) === 0; }); }
function editable(path) {
  return EDIT.exact.indexOf(path) >= 0 || EDIT.prefix.some(function (p) { return path.indexOf(p) === 0; });
}

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(SHELL).then(function (cache) {
    // The offline page without cookies: it's shared by whoever signs in on this device, so it must hold nobody's data.
    var page = cache.add(new Request(OFFLINE_URL, { credentials: "omit" }));
    var files = SHELL_FILES.map(function (url) { return cache.add(url).catch(function () {}); });   // one missing file never blocks it
    return Promise.all([page].concat(files));
  }).then(function () {
    if (!self.registration.active) return self.skipWaiting();       // the first install: nothing to replace
  }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return KEEP.indexOf(k) < 0; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

// ---------- whose data this is ----------
function getMeta() {
  return caches.open(META).then(function (c) { return c.match(META_URL); })
    .then(function (r) { return r ? r.json() : {}; }).catch(function () { return {}; });
}
function setMeta(data) {
  return caches.open(META).then(function (c) {
    return c.put(META_URL, new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } }));
  });
}
function wipe() {
  return Promise.all([caches.delete(PAGES), caches.delete(FILES), caches.delete(META),
    new Promise(function (resolve) {
      try {
        var req = indexedDB.deleteDatabase(DB_NAME);
        req.onsuccess = req.onerror = req.onblocked = function () { resolve(); };
      } catch (e) { resolve(); }
    })
  ]).then(function () {
    return self.clients.matchAll({ includeUncontrolled: true }).then(function (clients) {
      clients.forEach(function (c) { c.postMessage({ type: "offline-wiped" }); });
    });
  });
}
// The server's word on a page (X-Albright-Offline): wipe, switch person, and whether this page may be kept.
function handleTag(tag) {
  var parts = (tag || "").split(" "), user = parts[1] || "";
  if (parts[0] === "anon" || parts[0] === "off") return getMeta().then(function (m) { return m.user ? wipe() : null; }).then(function () { return false; });
  if (!user) return Promise.resolve(false);
  return getMeta().then(function (m) {
    if (m.user && m.user !== user) return wipe().then(function () { return setMeta({ user: user }); });
    if (!m.user) return setMeta({ user: user });
  }).then(function () { return parts[0] === "cache"; });
}

function trim(cache) {
  return cache.keys().then(function (keys) {
    return Promise.all(keys.slice(0, Math.max(0, keys.length - MAX_PAGES)).map(function (k) { return cache.delete(k); }));
  });
}
function pageKey(url) { var u = new URL(url); u.hash = ""; return u.href; }

// A saved page, marked so labs/offline.js shows the banner even when the device thinks it's online (the site is down).
function marked(response) {
  return response.text().then(function (html) {
    html = html.replace(/<body\b/i, '<body data-offline-copy="1"');
    var headers = new Headers(response.headers);
    headers.delete("Content-Length");
    return new Response(html, { status: 200, headers: headers });
  });
}

function offlinePage(request) {
  var edit = editable(new URL(request.url).pathname);
  return caches.open(PAGES).then(function (c) { return edit ? null : c.match(pageKey(request.url), { ignoreVary: true }); })
    .then(function (saved) {
      if (saved) return marked(saved);
      return caches.match(OFFLINE_URL, { ignoreVary: true }).then(function (page) {
        return page ? marked(page) : new Response("<h1>You're offline</h1>", { status: 503, headers: { "Content-Type": "text/html" } });
      });
    });
}

function onPage(event) {
  var request = event.request;
  event.respondWith(fetch(request).then(function (response) {
    var copy = response.clone();
    event.waitUntil(handleTag(response.headers.get("X-Albright-Offline")).then(function (keep) {
      var cc = (copy.headers.get("Cache-Control") || "").toLowerCase();
      var type = copy.headers.get("Content-Type") || "";
      if (!keep || never(new URL(request.url).pathname) || copy.status !== 200 || copy.type !== "basic" ||
          type.indexOf("text/html") !== 0) return;
      // no-store: the server says "cache" only for Django's never_cache (Back-button freshness, not privacy).
      if (cc.indexOf("no-store") >= 0 && cc.indexOf("must-revalidate") < 0) return;
      return caches.open(PAGES).then(function (c) { return c.put(pageKey(request.url), copy).then(function () { return trim(c); }); });
    }).catch(function () {}));
    return response;
  }).catch(function () { return offlinePage(request); }));
}

function onStatic(event) {
  var request = event.request;
  event.respondWith(caches.match(request).then(function (hit) {
    if (hit) return hit;
    return fetch(request).then(function (response) {
      if (response.ok || response.type === "opaque") {
        var copy = response.clone();
        event.waitUntil(caches.open(STATIC).then(function (c) { return c.put(request, copy); }).catch(function () {}));
      }
      return response;
    }).catch(function () {
      return caches.match(request, { ignoreSearch: true }).then(function (any) { return any || Response.error(); });
    });
  }));
}

function onFile(event) {
  var request = event.request;
  event.respondWith(fetch(request).then(function (response) {
    var size = parseInt(response.headers.get("Content-Length") || "0", 10);
    var cc = (response.headers.get("Cache-Control") || "").toLowerCase();
    if (response.status === 200 && size > 0 && size <= MAX_FILE && cc.indexOf("no-store") < 0) {
      var copy = response.clone();
      event.waitUntil(getMeta().then(function (m) {
        if (m.user) return caches.open(FILES).then(function (c) { return c.put(request, copy); });
      }).catch(function () {}));
    }
    return response;
  }).catch(function () {
    return caches.open(FILES).then(function (c) { return c.match(request); }).then(function (hit) { return hit || Response.error(); });
  }));
}

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;                     // changes always go to the network, never cached
  var url = new URL(request.url);
  if (url.origin === self.location.origin) {
    if (request.mode === "navigate") return onPage(event);
    if (url.pathname.indexOf("/static/") === 0) return onStatic(event);
    if (/^\/attachments\/\d+\/(thumb\/)?$/.test(url.pathname)) return onFile(event);
    return;                                                 // JSON and the rest: straight to the network
  }
  if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") return onStatic(event);
});

self.addEventListener("message", function (event) {
  var data = event.data || {};
  if (data.type === "SKIP_WAITING") { self.skipWaiting(); return; }
  if (data.type === "CLEAR") { event.waitUntil(wipe()); return; }
  if (data.type === "USER" && data.user) {                  // a page says who's signed in (labs/offline.js)
    event.waitUntil(getMeta().then(function (m) {
      if (m.user && m.user !== String(data.user)) return wipe().then(function () { return setMeta({ user: String(data.user) }); });
      if (!m.user) return setMeta({ user: String(data.user) });
    }));
  }
});

// ---------- Background Sync: the offline changes ----------
self.addEventListener("sync", function (event) {
  if (event.tag !== "albright-sync") return;
  event.waitUntil(self.clients.matchAll({ type: "window" }).then(function (clients) {
    if (clients.length) { clients[0].postMessage({ type: "SYNC" }); return; }       // the page sends it and shows it
    if (!self.AlbrightSyncCore) return;
    return getMeta().then(function (m) {
      if (!m.user) return;
      return self.AlbrightSyncCore.flush({ user: m.user }).then(function (out) {
        if (out && out.error) throw new Error("try again later");                   // the browser retries the sync
      });
    });
  }));
});

// ---------- Web Push ----------
self.addEventListener("push", function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) {}
  var title = data.title || "Albright";
  var options = {
    body: data.body || "",
    data: { url: data.url || "/" },
    tag: data.tag || undefined,          // the same event again replaces its banner instead of stacking
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
