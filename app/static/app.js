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
    // sign-in pages: Auto / Light / Dark selector
    var seg = document.querySelectorAll("[data-theme-set]");
    if (seg.length) {
      var mark = function () {
        var cur = root.getAttribute("data-theme") || "auto";
        seg.forEach(function (b) {
          var on = b.getAttribute("data-theme-set") === cur;
          b.setAttribute("aria-checked", on ? "true" : "false");
          b.tabIndex = on ? 0 : -1;
        });
      };
      seg.forEach(function (b, i) {
        b.addEventListener("click", function () {
          var v = b.getAttribute("data-theme-set");
          if (v === "auto") { root.removeAttribute("data-theme"); try { localStorage.removeItem("theme"); } catch (e) {} }
          else { root.setAttribute("data-theme", v); save(v); }
          mark();
        });
        b.addEventListener("keydown", function (e) {
          var d = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
          if (!d) return;
          e.preventDefault();
          var n = seg[(i + d + seg.length) % seg.length]; n.focus(); n.click();
        });
      });
      mark();
      return;
    }
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

// ---- after "Restart Authelia": live status card (page URL has ?restarting=flag|socket) ----
(function () {
  var mode = new URLSearchParams(location.search).get("restarting");
  if (mode !== "flag" && mode !== "socket") return;
  document.addEventListener("DOMContentLoaded", function () {
    var holder = document.getElementById("toasts") || document.querySelector("main");
    if (!holder) return;
    var card = document.createElement("div");
    card.className = "flash restart-status";
    card.setAttribute("role", "status");
    holder.insertBefore(card, holder.firstChild);   // newest toast first
    // drop the parameter so a reload doesn't start watching again
    history.replaceState(null, "", location.pathname);

    var start = Date.now(), pickedAt = mode === "socket" ? start : null, sawDown = false, downSince = null;
    function secs(t) { return Math.round((Date.now() - t) / 1000); }
    function show(cls, text) { card.className = "flash restart-status " + cls; card.textContent = text; }
    var done = false;
    function finish(cls, text) {
      show(cls, text);
      done = true;
      if (cls === "ok") autoDismiss(card, 10000);   // all good: let it go after a while
      else addClose(card);                      // a problem: stays until dismissed
    }
    // let the dashboard's Authelia card follow along
    function tell(state) { document.dispatchEvent(new CustomEvent("authelia:status", { detail: state })); }
    show("wait", mode === "flag" ? "Restart requested. Waiting for the host service to pick it up…" : "Authelia container restarted. Checking that it comes back…");

    function poll() {
      fetch("/restart/status", { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
        .then(function (s) {
          if (mode === "flag" && !pickedAt) {
            if (s.flag === "pending") {
              if (secs(start) >= 20) {
                done = true;   // stop polling while the request is withdrawn
                var csrfEl = document.querySelector('input[name="csrf"]');
                fetch("/restart/withdraw", {
                  method: "POST", credentials: "same-origin",
                  headers: { "X-CSRF-Token": csrfEl ? csrfEl.value : "" }
                }).then(function (r) { return r.json(); }).then(function (j) {
                  if (j.ok && j.withdrawn === false) {   // the host took it at the last moment: keep watching
                    done = false; pickedAt = Date.now(); setTimeout(poll, 400);
                    return;
                  }
                  finish("error", "The restart request wasn't picked up after 20 seconds, so it was withdrawn (Authelia won't restart later by surprise). Check on the host that the authelia-restart.path unit is enabled and running: systemctl status authelia-restart.path");
                }).catch(function () {
                  finish("error", "The restart request wasn't picked up after 20 seconds. Check on the host that the authelia-restart.path unit is enabled and running: systemctl status authelia-restart.path");
                });
              }
              return;
            }
            pickedAt = Date.now();
          }
          if (s.authelia === null) {
            finish("ok", (mode === "socket" ? "Authelia container restarted." : "Restart request picked up by the host.") + " (Set AUTHELIA_URL to also check that Authelia comes back up.)");
            return;
          }
          if (s.authelia) tell(s.authelia);
          if (s.authelia === "down") {
            sawDown = true;
            if (!downSince) downSince = Date.now();
            if (secs(downSince) >= 60) finish("error", "Authelia hasn't come back after 60 seconds. Check its logs on the host: docker logs " + s.container);
            else show("wait", "Authelia is restarting… (" + secs(pickedAt) + " s)");
            return;
          }
          // up
          if (sawDown || mode === "socket") finish("ok", "Authelia is up again (" + secs(pickedAt) + " s).");
          else if (secs(pickedAt) >= 15) finish("ok", "Authelia is responding. It never looked down, so the restart was either very quick or didn't happen; check docker ps on the host if in doubt.");
        })
        .catch(function () { /* this app itself unreachable for a moment: keep trying */ })
        .then(function () {
          if (!done && secs(start) > 120) finish("error", "Stopped checking after 2 minutes.");
          // Authelia restarts in about a second, so check often until it has been seen going down;
          // one check at a time (a check can take up to 2.5 s while Authelia's container is stopped).
          if (!done) setTimeout(poll, sawDown ? 1500 : 400);
        });
    }
    poll();
  });
})();

// ---- dashboard: Authelia health and configuration validation, refreshed while the page is open ----
document.addEventListener("DOMContentLoaded", function () {
  var dash = document.getElementById("dash");
  if (!dash) return;
  function set(id, cls, text) {
    var el = document.getElementById(id);
    if (el) { el.className = "tag " + cls; el.textContent = text; }
  }
  function hhmm() { var d = new Date(); return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2); }
  function showAuthelia(state, checked) {
    if (state === "up") set("dash-authelia", "ok", "up");
    else if (state === "down") set("dash-authelia", "bad", "down");
    var sub = document.getElementById("dash-authelia-sub");
    if (sub && state) sub.textContent = (state === "up" ? "Responding" : "Not responding") + " · checked " + (checked || hhmm());
  }
  var busy = false;
  function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    fetch(dash.getAttribute("data-status"), { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        showAuthelia(s.authelia, s.checked);
        if (s.config_ok === true) set("dash-config", "ok", "valid");
        else if (s.config_ok === false) set("dash-config", "bad", s.config_problems + (s.config_problems === 1 ? " problem" : " problems"));
        else set("dash-config", "mut", "not checked");
        var cfg = document.getElementById("dash-config");
        if (cfg && s.version) cfg.title = "Checked with Authelia " + s.version;
        // "up to date" in green while the built-in Authelia (used for config checks) is the latest release;
        // a notice only when a newer release is out; nothing when it can't be checked
        var ver = document.getElementById("dash-version");
        if (ver) {
          ver.hidden = !(s.version && s.latest && !s.update);
          ver.textContent = "";
          if (!ver.hidden) {
            var tag = document.createElement("span");
            tag.className = "tag ok";
            tag.textContent = "Authelia version up to date";
            tag.title = "Config checks use Authelia " + s.version + ", the latest release";
            ver.appendChild(tag);
          }
        }
        var upd = document.getElementById("dash-update");
        if (upd) {
          upd.hidden = !s.update;
          if (s.update) upd.textContent = "Authelia " + s.latest + " is out, but config checks here still use " + s.version + ". Pull the latest admin image (or rebuild it) to update them.";
        }
      })
      .catch(function () {
        set("dash-authelia", "mut", "unknown");
        set("dash-config", "mut", "unknown");
      })
      .then(function () { busy = false; });
  }
  refresh();
  setInterval(refresh, 15000);                                  // paused while the tab is in the background
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
  // during a restart, follow the restart watcher's checks (every second or so) instead of waiting
  document.addEventListener("authelia:status", function (e) { showAuthelia(e.detail); });
});

// ---- messages (toasts): success messages leave by themselves; errors stay, with a close button ----
function fadeOut(el, delay) {
  setTimeout(function () {
    el.classList.add("leaving");
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 400);
  }, delay);
}
// leave after `delay`, but not while the mouse is over the toast (time to finish reading)
function autoDismiss(el, delay) {
  var timer = setTimeout(go, delay);
  function go() { fadeOut(el, 0); }
  el.addEventListener("mouseenter", function () { clearTimeout(timer); });
  el.addEventListener("mouseleave", function () { clearTimeout(timer); timer = setTimeout(go, 2500); });
}
function addClose(el) {
  if (el.querySelector(".flash-x")) return;
  var b = document.createElement("button");
  b.type = "button";
  b.className = "flash-x";
  b.setAttribute("aria-label", "Dismiss");
  b.textContent = "×";
  b.addEventListener("click", function () { fadeOut(el, 0); });
  el.appendChild(b);
}
document.addEventListener("DOMContentLoaded", function () {
  // only one-time messages (data-flash); permanent notices about a state that needs fixing stay put
  document.querySelectorAll(".flash[data-flash]").forEach(function (el) {
    if (el.classList.contains("ok")) autoDismiss(el, 6000);
    else addClose(el);
  });
});


// ---- idle sign-out: report real activity, warn a minute before the session ends ----
document.addEventListener("DOMContentLoaded", function () {
  if (!document.querySelector('form[action$="/logout"]')) return;   // only when signed in
  var csrfEl = document.querySelector('input[name="csrf"]');
  var lastPing = Date.now(), warn = null, deadline = null, tick = null;

  function ping() {
    lastPing = Date.now();
    fetch("/session/ping", { method: "POST", credentials: "same-origin",
                             headers: { "X-CSRF-Token": csrfEl ? csrfEl.value : "" } })
      .then(function (r) { if (r.status === 401) location.href = "/login"; else hideWarn(); })
      .catch(function () {});
  }
  ["keydown", "mousedown", "mousemove", "touchstart", "wheel"].forEach(function (ev) {
    document.addEventListener(ev, function () {
      if (Date.now() - lastPing > 60000) ping();   // at most once a minute
    }, { passive: true });
  });

  function hideWarn() {
    if (warn) { fadeOut(warn, 0); warn = null; }
    if (tick) { clearInterval(tick); tick = null; }
    deadline = null;
  }
  function showWarn(seconds) {
    deadline = Date.now() + seconds * 1000;
    if (!warn) {
      warn = document.createElement("div");
      warn.className = "flash error idle-warn";
      warn.setAttribute("role", "alert");
      var text = document.createElement("span");
      var btn = document.createElement("button");
      btn.type = "button"; btn.className = "btn"; btn.textContent = "Stay signed in";
      btn.addEventListener("click", ping);
      warn.appendChild(text); warn.appendChild(btn);
      var holder = document.getElementById("toasts");
      if (holder) holder.insertBefore(warn, holder.firstChild);
      tick = setInterval(render, 1000);
    }
    render();
  }
  function render() {
    if (!warn || !deadline) return;
    var s = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    warn.firstChild.textContent = "You'll be signed out in " + s + " s because there was no activity. ";
    if (s <= 0 && !ending) { ending = true; setTimeout(check, 1500); }
  }
  // The server decides: the page goes to the sign-in page only once the server reports the session ended
  // (activity in another tab may have kept it alive).
  var ending = false;
  function check() {
    fetch("/session/status", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        ending = false;
        if (!s.signed_in) { location.href = "/login"; return; }
        if (s.left === null || s.left === undefined) return;   // idle sign-out is off
        if (s.left <= 0) { ending = true; setTimeout(check, 1500); }   // the next check signs out
        else if (s.left <= 60) showWarn(s.left);
        else hideWarn();
      })
      .catch(function () { ending = false; });
  }
  setInterval(check, 15000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });
});

// ---- account menu (the initials button in the phone top bar) ----
document.addEventListener("DOMContentLoaded", function () {
  var btn = document.getElementById("acct-btn"), menu = document.getElementById("acct-menu");
  if (!btn || !menu) return;
  function setOpen(open) {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { var first = menu.querySelector("a, button"); if (first) first.focus(); }
  }
  btn.addEventListener("click", function (e) { e.stopPropagation(); setOpen(menu.hidden); });
  document.addEventListener("click", function (e) { if (!menu.hidden && !menu.contains(e.target)) setOpen(false); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !menu.hidden) { setOpen(false); btn.focus(); }
  });
});
