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
  "- [ ] 2 pounds of sausage\n\n    ![sausage-1.jpeg](/attachments/101/)\n\n    ![sausage-2.jpeg](/attachments/102/)\n- [ ] \n- [ ] 3 white or yellow onions",
  "> quote line\n> second\n\n---\n\n```py\nprint('a')\n\n\nx = 1\n```",
  "**bold** *it* ++under++ ~~strike~~ ==mark== `code` [link](https://example.com/a b)",
  "-# small print\nnext para",
  ":::callout 💡\nWatch out **here**\n- a\n:::\n\n:::toggle More *info*\nHidden text\n:::",
  "![photo.jpg](/attachments/12/)",
  "price is 5 * 3 and a_b and C++ and [x] and #tag\n1. real\n\n1\. not a list",
  "| a | b |\n| --- | --- |\n| 1 | 2 |",
  "line one\nline two (hard break)",
  "Trip\n- x\nafter",
  "- [ ] call mom @[date:2026-12-03T17:00|task:12]\n- [x] milk, eggs @[grocery:45,46]\nok @[grocery:7|already] @[grocery] hi @[person:3|Sam Lee]",
  "**due @[date:2026-12-03]** done",
  "Gift list\n\n@[budget:9]\n\nWrapping paper",
  "inline @[budget:9] still a chip",
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
// Natural dates, from Saturday Nov 21 2026 at 10:00.
const now = new Date(2026, 10, 21, 10, 0);
const when = [["tomorrow", "2026-11-22", null], ["fri", "2026-11-27", null], ["next fri", "2026-11-27", null], ["next mon", "2026-11-23", null], ["dec 3", "2026-12-03", null],
  ["3 dec", "2026-12-03", null], ["12/3", "2026-12-03", null], ["next week 5pm", "2026-11-23", "17:00"], ["in 3 days", "2026-11-24", null],
  ["5pm", "2026-11-21", "17:00"], ["9am", "2026-11-22", "09:00"], ["jan 2", "2027-01-02", null], ["today at noon", "2026-11-21", "12:00"],
  ["feb 30", null, null], ["sam", null, null]];
for (const [text, date, time] of when) {
  const got = NE.parseWhen(text, now);
  const ok = date === null ? got === null : got && got.date === date && got.time === time;
  if (!ok) bad++;
  console.log(ok ? "ok  " : "BAD ", JSON.stringify(text), "->", JSON.stringify(got));
}
console.log(JSON.stringify(NE.atItems("gro", { groceries: true, now }).map((i) => i.label)), JSON.stringify(NE.atItems("", { now, people: [{ id: 1, name: "Sam" }] }).map((i) => i.label)));
const budgetItem = NE.atItems("budget Christmas Gifts", { budget: true, now })[0];
if (!budgetItem || budgetItem.label !== "Create budget" || budgetItem.name !== "Christmas Gifts") {
  bad++; console.log("BAD  atItems budget", JSON.stringify(budgetItem));
} else console.log("ok   Create budget", budgetItem.name);
process.exit(bad ? 1 : 0);
