// Smoke test: notes.js + the editor bundle on a minimal workspace page (node wiring.test.mjs).
import { JSDOM } from "jsdom";
import fs from "fs";
const html = `<!doctype html><body><div class="nt" id="notes" data-leave-url="/notes/leave/">
<div class="nt-notes" id="nt-notes"></div>
<article id="note" data-note-id="5" data-version="3" data-can-edit="1" data-save-url="/notes/5/save/" data-list-url="/notes/" data-groceries-url="/notes/5/groceries/" data-groceries-remove-url="/notes/5/groceries/remove/" data-planner-url="/notes/5/planner/" data-planner-delete-url="/notes/5/planner/0/delete/" data-state-url="/notes/5/state/" data-settings-url="/notes/settings/" data-planner-item-url="/planner/item/0/">
 <div class="nt-toolbar"><button data-fmt="styles">Aa</button><button data-fmt="check" aria-pressed="false">c</button><button data-fmt="bullet">b</button><button data-fmt="image">i</button></div>
 <input id="nt-title" value="T"><input id="nt-tags" value=""><input type="checkbox" id="nt-sort-checked">
 <div id="nt-conflict" hidden><b id="nt-conflict-who"></b><span id="nt-conflict-when"></span><button id="nt-conflict-load"></button><button id="nt-conflict-keep"></button></div>
 <div id="nt-doc"><div id="nt-preview">x</div><div id="nt-editor-mount" hidden></div>
 <script id="nt-source" type="application/json">"Hello **world**\\n\\n- [ ] milk\\n- [x] eggs"</script></div>
 <script id="nt-smart" type="application/json">{"link_state": {}, "people": [{"id": 2, "name": "Sam"}], "groceries": true, "planner": true, "auto_planner": true, "today": "2026-11-21"}</script>
 <div id="nt-chippop" hidden><div data-chippop="date"><input data-chip-date><input data-chip-time><button data-chip-save></button><button data-chip-planner></button><a data-chip-open-task></a><button data-chip-remove></button></div><div data-chippop="grocery"><p data-chip-grocery-text></p><button data-chip-grocery-remove></button></div></div>
 <div id="nt-bubble" hidden><button data-fmt="bold">B</button></div><div id="nt-blockmenu" hidden></div>
 <div id="nt-linkbox" hidden><input id="nt-linkbox-url"><button data-link-save></button><button data-link-remove></button></div>
 <div id="nt-mbar" hidden></div><input type="file" id="nt-image-input">
 <span id="nt-saved"></span><time id="nt-updated" datetime="2026-11-21T10:00:00Z"></time><span id="nt-words"></span>
</article><div id="nt-toast" hidden><span class="nt-toast__text"></span><button class="nt-toast__action"></button></div></div></body>`;
const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true, url: "http://localhost/notes/5/" });
const w = dom.window;
w.matchMedia = () => ({ matches: false, addEventListener() {} });
const sent = [];
w.fetch = (url, opts) => {
  sent.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  const reply = /groceries\/$/.test(url) ? { ok: true, ids: [45, 46], added: [45, 46], already: false, names: ["milk", "eggs"] }
    : /planner\/$/.test(url) ? { ok: true, task_id: 77, created: true, url: "/planner/item/77/" }
    : { ok: true, version: 4, updated_iso: new Date().toISOString(), link_state: {}, sync: [] };
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(reply) });
};
w.eval(fs.readFileSync("../../albright_labs/static/labs/text.js", "utf8"));   // plural(), loaded by index.html
w.eval(fs.readFileSync("../static/albright_notes/vendor/note-editor.js", "utf8"));
w.eval(fs.readFileSync("../static/albright_notes/notes.js", "utf8"));
const mount = w.document.getElementById("nt-editor-mount");
console.log("mounted:", !mount.hidden, "preview hidden:", w.document.getElementById("nt-preview").hidden);
console.log("words:", w.document.getElementById("nt-words").textContent);
const pm = mount.querySelector(".ProseMirror");
console.log("has editor:", !!pm, "checklist items:", pm.querySelectorAll('ul[data-type="taskList"] > li').length);
// Click the checklist button with the cursor in the first paragraph -> it becomes a checklist item, autosave fires.
w.document.querySelector('[data-fmt="bullet"]').click();
await new Promise((r) => setTimeout(r, 1200));
console.log("saved:", JSON.stringify(sent.map((s) => s.body)));
console.log("status:", w.document.getElementById("nt-saved").textContent, "| edited:", w.document.getElementById("nt-updated").textContent);
// @grocery: a pending chip on a line, then leave the line -> sent, chip gets the ids, Undo toast.
const NE = w.NoteEditor;
const editor = w.document.getElementById("nt-editor-mount").querySelector(".ProseMirror").editor;
console.log("editor handle:", !!editor);
sent.length = 0;
// Put the cursor at the end of the first checklist item ("milk"), tag it, then move to the first paragraph.
const items = NE.findChips(editor);
let milkEnd = null;
editor.state.doc.descendants((node, pos) => { if (node.isText && node.text === "milk" && milkEnd === null) milkEnd = pos + node.nodeSize; });
editor.commands.setTextSelection(milkEnd);
NE.insertChip(editor, { kind: "grocery" }, null);
editor.commands.setTextSelection(1);                 // leave the line
await new Promise((r) => setTimeout(r, 400));
const chip = NE.findChips(editor)[0];
console.log("grocery request:", JSON.stringify(sent.filter((s) => /groceries/.test(s.url)).map((s) => s.body)));
console.log("chip after:", JSON.stringify({ ids: chip && chip.attrs.ids, key: chip && chip.key, fresh: chip && chip.attrs.fresh }));
console.log("toast:", w.document.querySelector(".nt-toast__text").textContent, "|", w.document.querySelector(".nt-toast__action").textContent);
await new Promise((r) => setTimeout(r, 1200));
const saves = sent.filter((s) => /save/.test(s.url));
console.log("saved body:", JSON.stringify(saves.length && saves[saves.length - 1].body.body));
console.log("@ items for 'fri 5pm':", JSON.stringify(NE.atItems("fri 5pm", { groceries: true }).map((i) => i.label)));
// "@today call mom" typed on a new line: the date turns into a chip once more words follow, the cursor stays at the
// end, and leaving the line sends it to the Planner (no click) with an Undo toast; leaving again sends nothing.
sent.length = 0;
editor.commands.setTextSelection(editor.state.doc.content.size - 1);
editor.commands.splitBlock();
for (const ch of "Pay rent @today call mom") editor.view.dispatch(editor.state.tr.insertText(ch));
const dated = NE.findChips(editor).filter((c) => c.attrs.kind === "date");
console.log("date chip:", dated.length, JSON.stringify(dated[0] && { date: dated[0].attrs.date, fresh: dated[0].attrs.fresh, text: dated[0].text }));
console.log("cursor at line end:", editor.state.selection.$from.parentOffset === editor.state.selection.$from.parent.content.size);
editor.commands.setTextSelection(1);
await new Promise((r) => setTimeout(r, 400));
const plannerReqs = () => sent.filter((s) => /planner\/$/.test(s.url)).map((s) => s.body);
console.log("planner request:", JSON.stringify(plannerReqs()));
console.log("date chip after:", JSON.stringify(NE.findChips(editor).filter((c) => c.attrs.kind === "date").map((c) => c.key)));
console.log("toast:", w.document.querySelector(".nt-toast__text").textContent, "|", w.document.querySelector(".nt-toast__action").textContent);
editor.commands.setTextSelection(editor.state.doc.content.size - 2);
editor.commands.setTextSelection(1);
await new Promise((r) => setTimeout(r, 1500));
console.log("planner requests after leaving again:", plannerReqs().length);
const saves2 = sent.filter((s) => /save/.test(s.url));
console.log("saved body:", JSON.stringify(saves2.length && saves2[saves2.length - 1].body.body));
for (const proto of [w.Range.prototype, w.Text.prototype, w.Element.prototype]) {   // jsdom has no layout
  if (!proto.getClientRects) proto.getClientRects = () => [];
  if (!proto.getBoundingClientRect) proto.getBoundingClientRect = () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 });
}
// "@fri 5pm water plants": waits through the time, then the chip carries both; "@tom says hi" stays text.
editor.commands.setTextSelection(editor.state.doc.content.size - 1);
editor.commands.splitBlock();
for (const ch of "@fri 5pm water plants") editor.view.dispatch(editor.state.tr.insertText(ch));
editor.commands.splitBlock();
for (const ch of "@tom says hi") editor.view.dispatch(editor.state.tr.insertText(ch));
const last = NE.findChips(editor).filter((c) => c.attrs.kind === "date").pop();
console.log("fri chip:", JSON.stringify({ time: last.attrs.time, text: last.text }), "| tom line:", JSON.stringify(editor.state.selection.$from.parent.textContent));
process.exit(0);
