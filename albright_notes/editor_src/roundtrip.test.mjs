// Round-trip check: Markdown -> editor -> Markdown (node roundtrip.test.mjs). Uses the built bundle in jsdom.
import { JSDOM } from "jsdom";
import fs from "fs";
const dom = new JSDOM("<!doctype html><div id=e></div>", { runScripts: "outside-only", pretendToBeVisual: true });
dom.window.eval(fs.readFileSync("../static/albright_notes/vendor/note-editor.js", "utf8"));
const NE = dom.window.NoteEditor;
const cases = [
  "Hello world",
  "# Title\n\nPara one\n\n\nPara after one empty\n\n\n\nTwo empties",
  "Shopping:\n- milk\n- eggs\n    - large\n1. one\n2. two",
  "- [ ] pack\n- [x] done\n    - [ ] nested",
  "> quote line\n> second\n\n---\n\n```py\nprint('a')\n\n\nx = 1\n```",
  "**bold** *it* ++under++ ~~strike~~ ==mark== `code` [link](https://example.com/a b)",
  "-# small print\nnext para",
  ":::callout 💡\nWatch out **here**\n- a\n:::\n\n:::toggle More *info*\nHidden text\n:::",
  "![photo.jpg](/attachments/12/)",
  "price is 5 * 3 and a_b and C++ and [x] and #tag\n1. real\n\n1\. not a list",
  "| a | b |\n| --- | --- |\n| 1 | 2 |",
  "line one\nline two (hard break)",
  "Trip\n- x\nafter",
];
let bad = 0;
for (const c of cases) {
  const doc = dom.window.document.createElement("div");
  dom.window.document.body.appendChild(doc);
  const ed = NE.create({ element: doc, markdown: c });
  const out = ed.getMarkdown();
  const ed2 = NE.create({ element: dom.window.document.createElement("div"), markdown: out });
  const out2 = ed2.getMarkdown();
  const stable = out === out2;
  if (!stable) bad++;
  console.log("IN :", JSON.stringify(c));
  console.log("OUT:", JSON.stringify(out), stable ? "" : "\n UNSTABLE 2nd: " + JSON.stringify(out2));
  console.log();
}
process.exit(bad ? 1 : 0);
