// Authelia configuration schema support: autocomplete, hover docs and live checks.
// The schema is Authelia's own JSON schema (docs/static/schemas/<version>/json-schema/configuration.json),
// copied into the image from the same Authelia release that is compiled there, and fetched at runtime.
import { hoverTooltip } from "@codemirror/view";
import { startCompletion } from "@codemirror/autocomplete";
import { isMap, isSeq, isScalar } from "yaml";

// ---------- schema navigation ----------
export function makeSchema(raw) {
  const defs = raw.$defs || raw.definitions || {};
  function deref(n) {
    let guard = 0;
    while (n && n.$ref && guard++ < 30) {
      const target = defs[n.$ref.split("/").pop()];
      if (!target) return null;
      // keep the more specific title/description written at the place of use
      const site = {};
      if (n.title) site.title = n.title;
      if (n.description) site.description = n.description;
      n = Object.assign({}, target, site);
    }
    return n || null;
  }
  // a schema and its oneOf alternatives
  const branches = (n) => (n ? [n, ...(n.oneOf || []).map(deref).filter(Boolean)] : []);
  function props(n) {
    const out = {};
    for (const b of branches(n)) for (const k in b.properties || {}) if (!(k in out)) out[k] = deref(b.properties[k]);
    return out;
  }
  function child(n, key) {
    for (const b of branches(n)) {
      if (b.properties && b.properties[key]) return deref(b.properties[key]);
      for (const re in b.patternProperties || {}) {
        try { if (new RegExp(re).test(key)) return deref(b.patternProperties[re]); } catch (e) { /* bad pattern */ }
      }
      if (b.additionalProperties && typeof b.additionalProperties === "object") return deref(b.additionalProperties);
    }
    return null;
  }
  function items(n) {
    for (const b of branches(n)) if (b.items && !Array.isArray(b.items)) return deref(b.items);
    return null;
  }
  const types = (n) => {
    const t = new Set();
    for (const b of branches(n)) [].concat(b.type || []).forEach((x) => t.add(x));
    return t;
  };
  const hasProps = (n) => branches(n).some((b) => b.properties || b.patternProperties);
  // objects that list every allowed key (unknown keys are worth flagging)
  const closed = (n) => !!n && hasProps(n) && branches(n).every((b) => !b.patternProperties &&
    !(b.additionalProperties && typeof b.additionalProperties === "object") && b.additionalProperties !== true);
  function enumValues(n) {
    const v = [];
    for (const b of branches(n)) for (const e of b.enum || []) if (!v.includes(String(e))) v.push(String(e));
    if (!v.length && types(n).has("boolean") && types(n).size === 1) v.push("true", "false");
    return v;
  }
  // true when the option accepts ONLY its listed values (no free-form alternative such as a duration)
  function strictEnum(n) {
    if (!n) return false;
    const alts = n.oneOf ? n.oneOf.map(deref).filter(Boolean) : [];
    if (n.type || n.enum) alts.push(n);
    if (!alts.length) return false;
    return alts.every((b) => (b.enum && b.enum.length) || [].concat(b.type || []).every((t) => t === "boolean"));
  }
  const isArrayOfObjects = (n) => { const it = items(n); return !!it && hasProps(it); };
  const isObject = (n) => hasProps(n) && !types(n).has("string");
  const root = deref(raw);
  function at(path) {
    let n = root;
    for (const step of path) {
      if (!n) return null;
      n = step === "[]" ? items(n) : child(n, step);
    }
    return n;
  }
  return { root, deref, props, child, items, types, closed, enumValues, strictEnum, isArrayOfObjects, isObject, at };
}

// ---------- where is the cursor? (indentation based, so it works while the YAML is half-typed) ----------
function lineInfo(text) {
  const m = /^(\s*)(-\s+)?(.*)$/.exec(text);
  const indent = m[1].length, dash = !!m[2];
  const keyIndent = indent + (m[2] ? m[2].length : 0);
  const rest = m[3];
  const km = /^("[^"]*"|'[^']*'|[^\s:#'"][^:#]*?)\s*:(\s|$)/.exec(rest);
  return {
    indent, dash, keyIndent,
    key: km ? km[1].replace(/^["']|["']$/g, "") : null,
    keyLen: km ? km[1].length : 0,
    skip: (!rest.trim() && !dash) || (!dash && rest.startsWith("#")) || /^\s*(---|\.\.\.)\s*$/.test(text)
  };
}

// Path of the mapping that holds a key at column `col` of line `lineNo`.
function containerPath(doc, lineNo, col, selfDash, selfIndent) {
  const path = [];
  let target = col;
  if (selfDash) { path.push("[]"); target = selfIndent; }
  for (let l = lineNo - 1; l >= 1 && target > 0; l--) {
    const li = lineInfo(doc.line(l).text);
    if (li.skip) continue;
    if (li.dash && li.indent < target && li.keyIndent >= target) {   // a sibling inside the same list item
      path.push("[]"); target = li.indent; continue;
    }
    if (li.key && li.keyIndent < target) {                           // the parent key
      path.push(li.key); target = li.keyIndent;
      if (li.dash) { path.push("[]"); target = li.indent; }
      continue;
    }
    if (li.dash && !li.key && li.indent < target) { path.push("[]"); target = li.indent; }
  }
  return path.reverse();
}

function describe(S, n) {
  const parts = [];
  const t = [...S.types(n)];
  const ev = S.enumValues(n);
  if (S.isObject(n)) parts.push("section");
  else if (S.isArrayOfObjects(n)) parts.push("list");
  else if (t.length) parts.push(t.join(" or "));
  if (n.default !== undefined && typeof n.default !== "object") parts.push("default: " + n.default);
  return { detail: parts.join(", "), values: ev };
}

// ---------- autocomplete ----------
export function completionSource(getSchema) {
  return (ctx) => {
    const S = getSchema();
    if (!S) return null;
    const line = ctx.state.doc.lineAt(ctx.pos);
    const before = line.text.slice(0, ctx.pos - line.from);
    if (/#/.test(before.replace(/(['"]).*?\1/g, ""))) return null;   // inside a comment

    // value position: "key: val|"
    let m = /^(\s*)(-\s+)?("[^"]*"|'[^']*'|[^\s:#'"][^:#]*?)\s*:\s+([\w.-]*)$/.exec(before);
    if (m) {
      const key = m[3].replace(/^["']|["']$/g, "");
      const n = S.child(S.at(containerPath(ctx.state.doc, line.number, m[1].length + (m[2] || "").length, !!m[2], m[1].length)) , key);
      const vals = n ? S.enumValues(n) : [];
      if (!vals.length) return null;
      return {
        from: ctx.pos - m[4].length,
        options: vals.map((v) => ({ label: v, type: "enum", boost: String(n.default) === v ? 1 : 0, detail: String(n.default) === v ? "default" : undefined })),
        validFor: /^[\w.-]*$/
      };
    }

    // key position: "  ke|" or "  - ke|"
    m = /^(\s*)(-\s+)?([\w.-]*)$/.exec(before);
    if (!m) return null;
    if (!ctx.explicit && !m[3] && !m[2]) return null;   // don't pop up on every new line; Ctrl+Space works
    const col = m[1].length + (m[2] || "").length;
    const path = containerPath(ctx.state.doc, line.number, col, !!m[2], m[1].length);
    const parent = S.at(path);
    if (!parent) return null;
    const props = S.props(parent);
    const names = Object.keys(props);
    if (!names.length) {
      const vals = S.enumValues(parent);   // a list of plain values, e.g. methods
      if (!vals.length) return null;
      return { from: ctx.pos - m[3].length, options: vals.map((v) => ({ label: v, type: "enum" })), validFor: /^[\w.-]*$/ };
    }
    const present = siblingKeys(ctx.state.doc, line.number, col, !!m[2]);
    const indent = " ".repeat(col);
    return {
      from: ctx.pos - m[3].length,
      validFor: /^[\w.-]*$/,
      options: names.filter((k) => !present.has(k)).map((k) => {
        const n = props[k];
        const d = describe(S, n);
        return {
          label: k,
          type: S.isObject(n) || S.isArrayOfObjects(n) ? "namespace" : "property",
          detail: d.detail,
          info: () => infoDom(k, n, d),
          apply: (view, completion, from, to) => {
            let insert;
            if (S.isObject(n)) insert = k + ":\n" + indent + "  ";
            else if (S.isArrayOfObjects(n)) insert = k + ":\n" + indent + "  - ";
            else insert = k + ": ";
            view.dispatch({ changes: { from, to, insert }, selection: { anchor: from + insert.length }, userEvent: "input.complete" });
            if (d.values.length) setTimeout(() => startCompletion(view), 0);
          }
        };
      })
    };
  };
}

// keys already used in the mapping the cursor is in (so they aren't suggested twice)
function siblingKeys(doc, lineNo, col, selfDash) {
  const out = new Set();
  // upwards, until the start of this mapping (a line starting a new list item belongs to a different item)
  if (!selfDash) {
    for (let l = lineNo - 1; l >= 1; l--) {
      const li = lineInfo(doc.line(l).text);
      if (li.skip) continue;
      if (li.keyIndent < col) break;
      if (li.dash && li.indent < col) { if (li.keyIndent === col && li.key) out.add(li.key); break; }  // this item's first key
      if (li.keyIndent === col && li.key) out.add(li.key);
    }
  }
  // downwards, until the mapping (or this list item) ends
  for (let l = lineNo + 1; l <= doc.lines; l++) {
    const li = lineInfo(doc.line(l).text);
    if (li.skip) continue;
    if (li.keyIndent < col || (li.dash && li.indent < col)) break;
    if (li.keyIndent === col && li.key && !li.dash) out.add(li.key);
  }
  return out;
}

function infoDom(key, n, d) {
  const box = document.createElement("div");
  box.className = "cm-schema-info";
  const h = document.createElement("strong");
  h.textContent = n.title && n.title !== key ? n.title : key;
  box.appendChild(h);
  if (n.description) { const p = document.createElement("div"); p.textContent = n.description; box.appendChild(p); }
  const meta = [];
  if (d.detail) meta.push(d.detail);
  if (d.values.length && d.values.length <= 12) meta.push("values: " + d.values.join(", "));
  if (meta.length) { const p = document.createElement("div"); p.className = "cm-schema-meta"; p.textContent = meta.join(" · "); box.appendChild(p); }
  return box;
}

// ---------- hover docs on keys ----------
export function schemaHover(getSchema) {
  return hoverTooltip((view, pos) => {
    const S = getSchema();
    if (!S) return null;
    const line = view.state.doc.lineAt(pos);
    const li = lineInfo(line.text);
    if (!li.key || li.skip) return null;
    const start = line.from + li.keyIndent, end = start + li.keyLen;
    if (pos < start || pos > end) return null;
    const n = S.child(S.at(containerPath(view.state.doc, line.number, li.keyIndent, li.dash, li.indent)), li.key);
    if (!n) return null;
    return { pos: start, end, above: true, create: () => ({ dom: infoDom(li.key, n, describe(S, n)) }) };
  }, { hoverTime: 350 });
}

// ---------- live checks: unknown options and invalid values (warnings; Authelia's validation decides) ----------
const keyName = (k) => (isScalar(k) ? String(k.value) : String(k));

export function schemaDiagnostics(S, doc) {
  const out = [];
  function walk(node, n, label) {
    if (!node || !n) return;
    if (isMap(node)) {
      for (const pair of node.items) {
        const k = keyName(pair.key);
        const c = S.child(n, k);
        if (!c) {
          if (S.closed(n) && pair.key && pair.key.range) {
            out.push({ from: pair.key.range[0], to: pair.key.range[1], severity: "warning", source: "Authelia schema",
              message: "Unknown option '" + k + "'" + (label ? " in " + label : "") + "." });
          }
          continue;
        }
        walk(pair.value, c, label ? label + "." + k : k);
      }
    } else if (isSeq(node)) {
      const it = S.items(n);
      if (it) node.items.forEach((x) => walk(x, it, label + "[]"));
    } else if (isScalar(node) && node.value !== null && node.range) {
      const vals = S.enumValues(n);
      const v = String(node.value);
      const known = vals.some((x) => x.toLowerCase() === v.toLowerCase());   // Authelia ignores case here
      if (vals.length && S.strictEnum(n) && !known && !/\{\{|\$\{/.test(v)) {
        out.push({ from: node.range[0], to: node.range[1], severity: "warning", source: "Authelia schema",
          message: "'" + v + "' is not a valid value for " + (label || "this option") + ". Allowed: " + vals.join(", ") + "." });
      }
    }
  }
  walk(doc.contents, S.root, "");
  return out;
}
