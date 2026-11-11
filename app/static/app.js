// CSP forbids inline scripts/handlers, so everything lives here.
// Loaded in <head> (synchronously) so the saved theme applies before first paint.

// ---- theme: auto (follows OS) -> manual light/dark, remembered in localStorage ----
(function () {
  var root = document.documentElement;
  function stored() { try { return localStorage.getItem("theme"); } catch (e) { return null; } }
  function save(v) { try { localStorage.setItem("theme", v); } catch (e) {} }
  var t = stored();
  if (t === "light" || t === "dark") root.setAttribute("data-theme", t);

  function isDark() {
    var t = root.getAttribute("data-theme");
    if (t) return t === "dark";
    return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function paint(btn) {
    var icon = isDark() ? "☀" : "☾";  // sun / moon
    if (btn.hasAttribute("data-label")) {
      btn.innerHTML = "";
      var i = document.createElement("span"); i.textContent = icon; i.style.cssText = "width:18px;text-align:center;font-size:17px;line-height:1";
      var l = document.createElement("span"); l.className = "lbl"; l.textContent = isDark() ? "Light mode" : "Dark mode";
      btn.appendChild(i); btn.appendChild(l);
    } else btn.textContent = icon;
    var label = isDark() ? "Switch to light mode" : "Switch to dark mode";
    btn.title = label; btn.setAttribute("aria-label", label);
  }
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    var created = !btn;
    if (created) { btn = document.createElement("button"); btn.id = "theme-toggle"; btn.type = "button"; btn.className = "floating"; }
    paint(btn);
    btn.addEventListener("click", function () {
      var next = isDark() ? "light" : "dark";
      root.setAttribute("data-theme", next);
      save(next);
      paint(btn);
    });
    if (created) document.body.appendChild(btn);
  });
})();

// ---- confirmation prompts ----
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !confirm(msg)) e.preventDefault();
});

// ---- live filter for tables: <input data-filter="#tableid"> ----
document.addEventListener("input", function (e) {
  var sel = e.target.getAttribute && e.target.getAttribute("data-filter");
  if (!sel) return;
  var q = e.target.value.trim().toLowerCase();
  document.querySelectorAll(sel + " tbody tr").forEach(function (tr) {
    tr.style.display = tr.textContent.toLowerCase().indexOf(q) === -1 ? "none" : "";
  });
});

// ---- passkeys (WebAuthn) ----
(function () {
  function toBuf(s) {  // base64url -> ArrayBuffer
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var bin = atob(s), a = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
    return a.buffer;
  }
  function toB64u(buf) {  // ArrayBuffer -> base64url
    var a = new Uint8Array(buf), s = "";
    for (var i = 0; i < a.length; i++) s += String.fromCharCode(a[i]);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function csrf() {
    var el = document.querySelector("[data-csrf]");
    return el ? el.getAttribute("data-csrf") : "";
  }
  function post(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    });
  }
  function say(text, isErr) {
    var m = document.getElementById("pk-msg");
    if (!m) return;
    m.textContent = text || "";
    m.className = "msg" + (isErr ? " err" : "");
  }
  function fail(e) {
    if (e && (e.name === "NotAllowedError" || e.name === "AbortError")) say("Cancelled or timed out.", true);
    else if (e && e.name === "InvalidStateError") say("This device already has a passkey for this site.", true);
    else say((e && e.message) || "Something went wrong.", true);
  }
  function supported() {
    if (window.PublicKeyCredential && navigator.credentials) return true;
    say("This browser or page can't use passkeys. Open the site over HTTPS on its real domain.", true);
    return false;
  }

  function register() {
    if (!supported()) return;
    var nameEl = document.getElementById("pk-name");
    say("Follow the prompt on your device…");
    post("/passkeys/register/options").then(function (o) {
      o.challenge = toBuf(o.challenge);
      o.user.id = toBuf(o.user.id);
      (o.excludeCredentials || []).forEach(function (c) { c.id = toBuf(c.id); });
      return navigator.credentials.create({ publicKey: o });
    }).then(function (c) {
      var r = c.response;
      return post("/passkeys/register/verify", {
        name: nameEl ? nameEl.value : "",
        credential: {
          id: c.id, rawId: toB64u(c.rawId), type: c.type,
          authenticatorAttachment: c.authenticatorAttachment || null,
          response: {
            clientDataJSON: toB64u(r.clientDataJSON),
            attestationObject: toB64u(r.attestationObject),
            transports: r.getTransports ? r.getTransports() : []
          }
        }
      });
    }).then(function () { location.reload(); }).catch(fail);
  }

  function login() {
    if (!supported()) return;
    say("Follow the prompt on your device…");
    post("/passkeys/login/options").then(function (o) {
      o.challenge = toBuf(o.challenge);
      (o.allowCredentials || []).forEach(function (c) { c.id = toBuf(c.id); });
      return navigator.credentials.get({ publicKey: o });
    }).then(function (c) {
      var r = c.response;
      return post("/passkeys/login/verify", {
        credential: {
          id: c.id, rawId: toB64u(c.rawId), type: c.type,
          authenticatorAttachment: c.authenticatorAttachment || null,
          response: {
            clientDataJSON: toB64u(r.clientDataJSON),
            authenticatorData: toB64u(r.authenticatorData),
            signature: toB64u(r.signature),
            userHandle: r.userHandle ? toB64u(r.userHandle) : null
          }
        }
      });
    }).then(function (j) { location.href = j.next || "/"; }).catch(fail);
  }

  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-pk-register]")) register();
    else if (e.target.closest("[data-pk-login]")) login();
  });
})();

// ---- password generator ----
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-genpw]");
  if (!b) return;
  var f = document.getElementById(b.getAttribute("data-genpw"));
  var a = new Uint32Array(20);
  crypto.getRandomValues(a);
  var cs = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_!@#%";
  f.value = Array.from(a, function (n) { return cs[n % cs.length]; }).join("");
  f.type = "text";
});
