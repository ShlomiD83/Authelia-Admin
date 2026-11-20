// Configuration editor: replaces <textarea data-editor> with CodeMirror 6.
// Bundled with esbuild into /static/editor.js during the Docker build. If the bundle is missing,
// the page keeps working with the plain textarea.
import { EditorState, Compartment } from "@codemirror/state";
import {
  EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter,
  highlightSpecialChars, drawSelection, rectangularSelection, crosshairCursor
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import {
  indentUnit, foldGutter, foldKeymap, indentOnInput, bracketMatching,
  syntaxHighlighting, HighlightStyle
} from "@codemirror/language";
import { yaml } from "@codemirror/lang-yaml";
import { tags as t } from "@lezer/highlight";
import { indentationMarkers } from "@replit/codemirror-indentation-markers";
import { linter, lintGutter, openLintPanel, forEachDiagnostic, diagnosticCount } from "@codemirror/lint";
import { parseDocument, isMap, isSeq, isScalar } from "yaml";
import { autocompletion, completionKeymap } from "@codemirror/autocomplete";
import { forceLinting } from "@codemirror/lint";
import { makeSchema, completionSource, schemaHover, schemaDiagnostics } from "./schema.js";
import { hideSecrets } from "./secrets.js";

// ---- problems: live YAML syntax check + Authelia's validate-config messages mapped to lines ----

// Pull the "- message" items out of `authelia validate-config` output, with their severity.
function parseAutheliaOutput(out) {
  const items = [];
  let sev = null;
  for (const raw of (out || "").split(/\r?\n/)) {
    if (/loaded with errors/i.test(raw)) { sev = "error"; continue; }
    if (/loaded with warnings/i.test(raw)) { sev = "warning"; continue; }
    const m = /^\s*-\s+(.*\S)\s*$/.exec(raw);
    if (m && sev) items.push({ severity: sev, message: m[1] });
  }
  return items;
}

// Turn a message into a path of steps: key names, "*" (every list item) or {index}.
//   "configuration key not expected: session.cookies[].unknown_opt"
//   "log: option 'level' must be one of ..."
//   "access_control: rule #2 (domain 'x'): option 'policy' must be ..."
function messagePath(msg) {
  const steps = [];
  const unexpected = /key not expected:\s*'?([\w.\[\]-]+)'?/.exec(msg);
  if (unexpected) {
    for (const part of unexpected[1].split(".")) {
      const m = /^([^\[]+)((?:\[\d*\])*)$/.exec(part);
      if (!m) break;
      steps.push(m[1]);
      for (const ix of m[2].match(/\[\d*\]/g) || []) {
        const n = ix.slice(1, -1);
        steps.push(n === "" ? "*" : { index: +n });
      }
    }
    return steps;
  }
  for (const seg of msg.split(/:\s+/)) {
    let m;
    if ((m = /^option '([^']+)'/.exec(seg))) { steps.push(...m[1].split(".")); break; }
    // "rule #2 (...)" -> item 2 of `rules`; the word before # names the list (plural), if it exists
    if ((m = /(?:^|\s)(\w+)\s+#(\d+)/.exec(seg))) { steps.push({ index: +m[2] - 1, hint: m[1] + "s" }); continue; }
    if (/^[a-z_][\w.-]*$/i.test(seg)) { steps.push(...seg.split(".")); continue; }
    break;
  }
  return steps;
}

const keyName = (k) => (isScalar(k) ? String(k.value) : String(k));

// Find the source offsets the path points at; stops at the deepest part that exists.
function resolvePath(doc, steps) {
  let cur = [{ node: doc.contents, pos: null }];
  let found = [];
  for (const step of steps) {
    const next = [];
    for (const { node } of cur) {
      if (typeof step === "string" && isMap(node)) {
        const p = node.items.find((it) => keyName(it.key) === step);
        if (p) next.push({ node: p.value, pos: p.key && p.key.range ? p.key.range[0] : null });
      } else if (step === "*" && isSeq(node)) {
        node.items.forEach((it) => next.push({ node: it, pos: it && it.range ? it.range[0] : null }));
      } else if (typeof step === "object") {
        // a numbered item: in a list directly, or in the first list inside this section (rules, cookies, ...)
        let seq = isSeq(node) ? node : null;
        if (!seq && isMap(node)) {
          const p = node.items.find((it) => isSeq(it.value) && keyName(it.key) === step.hint) ||
                    node.items.find((it) => isSeq(it.value));
          seq = p && p.value;
        }
        const it = seq && seq.items[step.index];
        if (it) next.push({ node: it, pos: it.range ? it.range[0] : null });
      }
    }
    if (!next.length) break;
    cur = next;
    found = next.filter((x) => x.pos != null).map((x) => x.pos);
  }
  return found;
}

function makeLinter(serverItems, getSchema) {
  return linter((view) => {
    const state = view.state, text = state.doc.toString(), out = [];
    const lineDiag = (pos, severity, message, source) => {
      const line = state.doc.lineAt(Math.min(Math.max(pos, 0), state.doc.length));
      const from = line.from + (/^\s*/.exec(line.text)[0].length);
      out.push({ from, to: Math.max(line.to, from), severity, message, source });
    };
    let doc;
    try { doc = parseDocument(text, { prettyErrors: false, uniqueKeys: true }); }
    catch (e) { lineDiag(0, "error", String(e.message || e), "YAML"); return out; }
    for (const e of doc.errors) {
      const [s, t] = e.pos || [0, 0];
      const from = Math.min(s, state.doc.length), to = Math.min(Math.max(t, s + 1), state.doc.length);
      out.push({ from, to: to > from ? to : from, severity: "error", message: e.message.split("\n")[0], source: "YAML" });
    }
    for (const w of doc.warnings) lineDiag(w.pos ? w.pos[0] : 0, "warning", w.message.split("\n")[0], "YAML");
    if (!doc.errors.length && doc.contents && !isMap(doc.contents))
      lineDiag(0, "error", "The top level of the configuration must be a mapping (key: value).", "YAML");
    if (!doc.errors.length) {
      for (const it of serverItems) {
        for (const pos of resolvePath(doc, messagePath(it.message))) lineDiag(pos, it.severity, it.message, "Authelia (last check)");
      }
      const S = getSchema();
      if (S && isMap(doc.contents)) {
        try { out.push(...schemaDiagnostics(S, doc)); } catch (e) { /* never let the schema break editing */ }
      }
    }
    return out;
  }, { delay: 400 });
}

// ---- per-browser preferences ----
const PREFS_KEY = "editorPrefs";
const DEFAULTS = { lineNumbers: true, wrap: false, guides: true, secrets: true };
function loadPrefs() {
  try { return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(PREFS_KEY) || "{}")); }
  catch (e) { return Object.assign({}, DEFAULTS); }
}
function savePrefs(p) { try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch (e) {} }

// ---- look: follows the page's light/dark CSS variables ----
const theme = EditorView.theme({
  "&": { backgroundColor: "var(--bg)", color: "var(--fg)", border: "1px solid var(--bd)", borderRadius: "9px", fontSize: "13px" },
  "&.cm-focused": { outline: "2px solid var(--pri)", outlineOffset: "-1px" },
  ".cm-scroller": { fontFamily: "ui-monospace,Consolas,monospace", lineHeight: "1.55" },
  ".cm-content": { caretColor: "var(--fg)", padding: ".5rem 0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--fg)" },
  ".cm-gutters": { backgroundColor: "var(--card)", color: "var(--mut)", borderRight: "1px solid var(--bd)", borderRadius: "9px 0 0 9px" },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--pri) 7%, transparent)" },
  ".cm-activeLineGutter": { backgroundColor: "color-mix(in srgb, var(--pri) 12%, transparent)", color: "var(--fg)" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, ::selection":
    { backgroundColor: "color-mix(in srgb, var(--pri) 28%, transparent) !important" },
  ".cm-selectionMatch": { backgroundColor: "color-mix(in srgb, var(--pri) 15%, transparent)" },
  ".cm-matchingBracket": { backgroundColor: "color-mix(in srgb, var(--ok) 25%, transparent)", outline: "none" },
  ".cm-foldPlaceholder": { backgroundColor: "var(--pri-soft)", color: "var(--pri-fg)", border: "none", padding: "0 .4rem" },
  ".cm-panels": { backgroundColor: "var(--card)", color: "var(--fg)" },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--bd)" },
  ".cm-panels.cm-panels-bottom": { borderTop: "1px solid var(--bd)" },
  ".cm-textfield": { backgroundColor: "var(--bg)", color: "var(--fg)", border: "1px solid var(--bd)", borderRadius: "6px" },
  ".cm-button": { backgroundImage: "none", backgroundColor: "var(--bg)", color: "var(--fg)", border: "1px solid var(--bd)", borderRadius: "6px" },
  ".cm-searchMatch": { backgroundColor: "color-mix(in srgb, #f5b400 35%, transparent)" },
  ".cm-tooltip": { backgroundColor: "var(--card)", color: "var(--fg)", border: "1px solid var(--bd)", borderRadius: "8px", boxShadow: "0 4px 16px rgba(0,0,0,.18)" },
  ".cm-tooltip-lint": { padding: "2px" },
  ".cm-diagnostic": { padding: ".35rem .6rem", maxWidth: "60ch", whiteSpace: "pre-wrap" },
  ".cm-diagnostic-error": { borderLeftColor: "var(--bad)" },
  ".cm-diagnosticSource": { color: "var(--mut)", fontSize: ".8em" },
  ".cm-lintRange-error": { backgroundColor: "color-mix(in srgb, var(--bad) 12%, transparent)" },
  ".cm-lintRange-warning": { backgroundColor: "color-mix(in srgb, #f59e0b 12%, transparent)" },
  ".cm-panel.cm-panel-lint ul [aria-selected]": { backgroundColor: "var(--pri-soft)", color: "var(--pri-fg)" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { backgroundColor: "var(--pri)", color: "#fff" },
  ".cm-tooltip-autocomplete .cm-completionDetail": { color: "var(--mut)", fontStyle: "normal", marginLeft: ".8em" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected] .cm-completionDetail": { color: "rgba(255,255,255,.8)" },
  ".cm-completionMatchedText": { textDecoration: "none", fontWeight: "700" },
  ".cm-tooltip.cm-completionInfo": { padding: ".5rem .7rem", maxWidth: "26rem" },
  ".cm-schema-info": { padding: ".45rem .65rem", maxWidth: "28rem", fontFamily: "system-ui,sans-serif", fontSize: "13px", lineHeight: "1.45" },
  ".cm-schema-info > div": { marginTop: ".25rem" },
  ".cm-schema-meta": { color: "var(--mut)", fontSize: "12px" },
  ".cm-secret": { filter: "blur(4.5px)", cursor: "pointer" },
  ".cm-searchMatch.cm-searchMatch-selected": { backgroundColor: "color-mix(in srgb, #f5b400 65%, transparent)" }
});

const highlight = HighlightStyle.define([
  { tag: [t.propertyName, t.definition(t.propertyName)], color: "var(--hl-key)" },
  { tag: [t.string, t.special(t.string), t.content], color: "var(--hl-str)" },
  { tag: [t.number, t.bool, t.null, t.atom], color: "var(--hl-num)" },
  { tag: [t.comment, t.lineComment, t.blockComment], color: "var(--mut)", fontStyle: "italic" },
  { tag: [t.keyword, t.meta, t.labelName, t.typeName], color: "var(--hl-kw)" },
  { tag: [t.punctuation, t.separator, t.squareBracket, t.brace], color: "var(--mut)" }
]);

// Indent guides coloured from the page palette (--ig / --ig-on are defined in base.html for both themes).
const guides = () => indentationMarkers({
  highlightActiveBlock: true,
  markerType: "fullScope",
  colors: { light: "var(--ig)", activeLight: "var(--ig-on)", dark: "var(--ig)", activeDark: "var(--ig-on)" }
});

function el(tag, attrs, text) {
  const e = document.createElement(tag);
  for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  return e;
}

function setup(ta) {
  const prefs = loadPrefs();
  const numbersC = new Compartment(), wrapC = new Compartment(), guidesC = new Compartment(), secretsC = new Compartment();
  const numbersExt = (on) => on ? [lineNumbers(), foldGutter(), highlightActiveLineGutter()] : [];

  // toolbar with toggles
  const bar = el("div", { class: "ed-bar" });
  const problems = el("button", { type: "button", class: "ed-problems", hidden: "" });
  const modified = el("span", { class: "ed-modified", hidden: "", title: "The editor differs from the file on disk" }, "● Modified");
  const toggles = el("div", { class: "ed-toggles" });
  bar.appendChild(problems);
  bar.appendChild(modified);
  bar.appendChild(toggles);
  // The file as it is on disk: the editor content on a fresh load, otherwise sent by the server.
  const diskText = ta.hasAttribute("data-ondisk") ? ta.value : (ta.getAttribute("data-disk") ?? ta.value);
  let leaving = false;
  const serverItems = parseAutheliaOutput(ta.getAttribute("data-validation"));
  let jumped = false;
  let draftTimer = null;
  // Authelia's configuration schema (autocomplete, hover docs, live checks); loaded after the editor appears
  let schema = null;
  const getSchema = () => schema;

  const host = el("div", { class: "ed-host" });
  ta.parentNode.insertBefore(bar, ta);
  ta.parentNode.insertBefore(host, ta);
  ta.style.display = "none";

  const view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc: ta.value,
      extensions: [
        numbersC.of(numbersExt(prefs.lineNumbers)),
        wrapC.of(prefs.wrap ? EditorView.lineWrapping : []),
        guidesC.of(prefs.guides ? guides() : []),
        secretsC.of(prefs.secrets ? hideSecrets() : []),
        highlightSpecialChars(), history(), drawSelection(),
        EditorState.allowMultipleSelections.of(true),
        indentOnInput(), bracketMatching(), rectangularSelection(), crosshairCursor(),
        highlightActiveLine(), highlightSelectionMatches(),
        indentUnit.of("  "), EditorState.tabSize.of(2),
        yaml(), syntaxHighlighting(highlight), theme,
        autocompletion({ override: [completionSource(getSchema)], icons: false }),
        schemaHover(getSchema),
        keymap.of([...completionKeymap, ...defaultKeymap, ...historyKeymap, ...searchKeymap, ...foldKeymap, indentWithTab]),
        EditorView.contentAttributes.of({ spellcheck: "false", "aria-label": "configuration.yml" }),
        lintGutter(), makeLinter(serverItems, getSchema),
        EditorView.updateListener.of((u) => {
          updateProblems(u.state);
          if (!u.docChanged) return;
          ta.value = u.state.doc.toString();
          modified.hidden = ta.value === diskText;
          clearTimeout(draftTimer);
          draftTimer = setTimeout(syncDraft, 500);
        })
      ]
    })
  });

  // "N problems" button: opens the list (also Ctrl+Shift+M); on load, jump to the first problem
  function firstProblem(state) {
    let first = null;
    forEachDiagnostic(state, (d, from) => { if (first == null || from < first) first = from; });
    return first;
  }
  function updateProblems(state) {
    const n = diagnosticCount(state);
    let errors = 0;
    forEachDiagnostic(state, (d) => { if (d.severity === "error") errors++; });
    problems.hidden = n === 0;
    problems.className = "ed-problems" + (errors ? " err" : "");
    problems.textContent = (errors ? "✖ " : "⚠ ") + n + (n === 1 ? " problem" : " problems");
    if (n && !jumped && serverItems.length) {
      jumped = true;
      const pos = firstProblem(state);
      if (pos != null) setTimeout(() => view.dispatch({
        selection: { anchor: pos }, effects: EditorView.scrollIntoView(pos, { y: "center" })
      }), 0);
    }
  }
  problems.addEventListener("click", () => {
    const pos = firstProblem(view.state);
    if (pos != null) view.dispatch({ selection: { anchor: pos }, effects: EditorView.scrollIntoView(pos, { y: "center" }) });
    openLintPanel(view);
  });

  function toggle(label, key, apply) {
    const id = "ed-" + key;
    const wrap = el("label", { class: "ed-toggle", for: id });
    const cb = el("input", { type: "checkbox", id });
    cb.checked = !!prefs[key];
    cb.addEventListener("change", () => {
      prefs[key] = cb.checked;
      savePrefs(prefs);
      view.dispatch({ effects: apply(cb.checked) });
    });
    wrap.appendChild(cb);
    wrap.appendChild(document.createTextNode(" " + label));
    toggles.appendChild(wrap);
  }
  toggle("Line numbers", "lineNumbers", (on) => numbersC.reconfigure(numbersExt(on)));
  toggle("Wrap lines", "wrap", (on) => wrapC.reconfigure(on ? EditorView.lineWrapping : []));
  toggle("Indent guides", "guides", (on) => guidesC.reconfigure(on ? guides() : []));
  toggle("Hide secrets", "secrets", (on) => secretsC.reconfigure(on ? hideSecrets() : []));

  // ---- unsaved edits: kept in this browser so they survive an expired session or a closed tab ----
  const DRAFT_KEY = "configDraft";
  const getDraft = () => { try { return JSON.parse(localStorage.getItem(DRAFT_KEY) || "null"); } catch (e) { return null; } };
  const dropDraft = () => { try { localStorage.removeItem(DRAFT_KEY); } catch (e) {} };
  const keepDraft = (text) => { try { localStorage.setItem(DRAFT_KEY, JSON.stringify({ text, ts: Date.now() })); } catch (e) {} };
  function syncDraft() {
    const text = view.state.doc.toString();
    if (text === diskText) dropDraft(); else keepDraft(text);
  }

  if (ta.hasAttribute("data-saved")) dropDraft();            // just saved: nothing pending
  const draft = getDraft();
  if (draft && draft.text !== ta.value) {
    const note = el("div", { class: "ed-draft", role: "status" });
    const when = new Date(draft.ts).toLocaleString();
    note.appendChild(el("span", null, "You have unsaved edits from " + when + "."));
    const restore = el("button", { type: "button", class: "btn" }, "Restore them");
    const discard = el("button", { type: "button", class: "btn sec" }, "Discard");
    restore.addEventListener("click", () => {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: draft.text } });
      note.remove();
      view.focus();
    });
    discard.addEventListener("click", () => { dropDraft(); note.remove(); });
    note.appendChild(restore);
    note.appendChild(discard);
    bar.parentNode.insertBefore(note, bar);
  } else if (draft && draft.text === diskText) {
    dropDraft();
  }

  // ---- notice changes made elsewhere (e.g. over SSH) while the page is open, not only on Save ----
  const baseInput = ta.form && ta.form.querySelector('input[name="base"]');
  const fpUrl = ta.getAttribute("data-fingerprint");
  if (baseInput && fpUrl && !document.querySelector(".conflict")) {
    const notice = el("div", { class: "ed-outside", role: "alert", hidden: "" });
    notice.appendChild(el("span", null, "configuration.yml was changed outside this page (for example over SSH) after you opened it. If you save, you'll be asked whether to overwrite that change."));
    const showDiff = el("button", { type: "button", class: "btn sec" }, "Show difference");
    const load = el("a", { class: "btn sec", href: location.pathname, "data-leave-ok": "" }, "Load the current file");
    showDiff.addEventListener("click", () => {
      const b = ta.form.querySelector('button[value="diff"]');
      if (b) { if (ta.form.requestSubmit) ta.form.requestSubmit(b); else b.click(); }
    });
    notice.appendChild(showDiff);
    notice.appendChild(load);
    bar.parentNode.insertBefore(notice, bar);
    let checking = false;
    const check = () => {
      if (checking || document.hidden) return;
      checking = true;
      fetch(fpUrl, { credentials: "same-origin", cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => { if (j && j.fp) notice.hidden = j.fp === baseInput.value; })
        .catch(() => {})   // session expired or offline: Save handles it
        .finally(() => { checking = false; });
    };
    check();
    setInterval(check, 20000);
    document.addEventListener("visibilitychange", check);
    window.addEventListener("focus", check);
  }

  // "● Modified" marker, and the browser's "Leave site?" prompt while edits are unsaved
  modified.hidden = ta.value === diskText;
  window.addEventListener("beforeunload", (e) => {
    if (leaving || view.state.doc.toString() === diskText) return;
    e.preventDefault();
    e.returnValue = "";
  });
  // links that deliberately leave (e.g. "Load the current file"); the draft keeps the edits
  document.addEventListener("click", (e) => { if (e.target.closest("[data-leave-ok]")) leaving = true; });

  // load the schema; if it's missing (e.g. a dev run without the image build) the editor works without it
  const schemaUrl = ta.getAttribute("data-schema");
  if (schemaUrl) {
    fetch(schemaUrl, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null))
      .then((raw) => { if (raw) { schema = makeSchema(raw); forceLinting(view); } })
      .catch(() => {});
  }

  // Ctrl+S / Cmd+S = "Validate & save" (anywhere on the page)
  const saveBtn = ta.form && ta.form.querySelector('button[value="save"]');
  if (saveBtn) {
    saveBtn.title = "Ctrl+S";
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (ta.form.requestSubmit) ta.form.requestSubmit(saveBtn); else saveBtn.click();
      }
    });
  }

  // make sure the latest text is posted, and kept in case the session has expired
  const form = ta.form;
  if (form) form.addEventListener("submit", () => {
    leaving = true;
    ta.value = view.state.doc.toString();
    clearTimeout(draftTimer);
    syncDraft();
  });
}

function init() { document.querySelectorAll("textarea[data-editor]").forEach(setup); }
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
