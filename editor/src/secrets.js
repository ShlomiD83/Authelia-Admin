// Blurs secret values (jwt_secret, encryption_key, client_secret, password, ...) on screen.
// The text itself is unchanged; a value is shown while the cursor or selection is inside it.
import { ViewPlugin, Decoration } from "@codemirror/view";
import { RangeSetBuilder } from "@codemirror/state";
import { parseDocument, isMap, isSeq, isScalar } from "yaml";

const SECRET_KEY = /(secret|password|token|(^|_)key)$/i;   // jwt_secret, encryption_key, key, password, ...
const NOT_SECRET = /(_length|_size|_policy|public)/i;       // key_length, public_key, ...

export function secretRanges(text) {
  const out = [];
  let doc;
  try { doc = parseDocument(text, { prettyErrors: false }); } catch (e) { return out; }
  (function walk(n) {
    if (isMap(n)) {
      for (const p of n.items) {
        const k = isScalar(p.key) ? String(p.key.value) : "";
        const v = p.value;
        if (isScalar(v) && v.range && v.value !== null && v.value !== "" &&
            SECRET_KEY.test(k) && !NOT_SECRET.test(k)) out.push([v.range[0], v.range[1]]);
        else walk(v);
      }
    } else if (isSeq(n)) n.items.forEach(walk);
  })(doc.contents);
  return out.sort((a, b) => a[0] - b[0]);
}

const blur = Decoration.mark({ class: "cm-secret", attributes: { title: "Secret hidden on screen: click to show" } });

export const hideSecrets = () => ViewPlugin.fromClass(class {
  constructor(view) {
    this.ranges = secretRanges(view.state.doc.toString());
    this.decorations = this.build(view);
  }
  update(u) {
    if (u.docChanged) this.ranges = secretRanges(u.state.doc.toString());
    if (u.docChanged || u.selectionSet) this.decorations = this.build(u.view);
  }
  build(view) {
    const b = new RangeSetBuilder();
    const len = view.state.doc.length;
    for (const [f, t] of this.ranges) {
      const to = Math.min(t, len);
      if (to <= f) continue;
      // show the value you're working in
      if (view.state.selection.ranges.some((r) => r.from <= to && r.to >= f)) continue;
      b.add(f, to, blur);
    }
    return b.finish();
  }
}, { decorations: (v) => v.decorations });
