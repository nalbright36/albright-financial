// Builds Albright Notes' editor into one vendored file the page loads from static (no CDN; the CSP stays
// script-src 'self'):  cd albright_notes/editor_src && npm ci && npm run build
// Output: albright_notes/static/albright_notes/vendor/note-editor.js (commit it; node_modules is ignored).
import { build } from "esbuild";

await build({
    entryPoints: ["src/entry.js"],
    bundle: true,
    minify: true,
    format: "iife",
    target: ["es2019"],
    legalComments: "eof",
    outfile: "../static/albright_notes/vendor/note-editor.js",
    banner: { js: "/* Albright Notes editor - TipTap (MIT) + markdown-it (MIT), built by albright_notes/editor_src/build.mjs */" },
});
console.log("built ../static/albright_notes/vendor/note-editor.js");
