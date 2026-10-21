// Bundles each React mock into a single self-contained HTML page in mocks/dist.
import { build } from "esbuild";
import { mkdirSync, readFileSync, writeFileSync, copyFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, "mocks", "src");
const out = join(here, "mocks", "dist");
mkdirSync(out, { recursive: true });

const TITLES = {
  greenhouse: "Job Application for Software Engineer, New Grad at Mock Co",
  ashby: "Software Engineer – New Grad @ Mock Co",
  workday: "Software Engineer, New Grad - Mock Co Careers",
};

for (const name of ["greenhouse", "ashby", "workday"]) {
  const res = await build({
    entryPoints: [join(src, name + ".jsx")], bundle: true, write: false, minify: true, format: "iife",
    jsx: "automatic", define: { "process.env.NODE_ENV": '"production"' }, logLevel: "warning",
  });
  const js = res.outputFiles[0].text.replace(/<\/script/gi, "<\\/script");
  writeFileSync(join(out, name + ".html"), `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${TITLES[name]}</title>
<style>body{font:14px system-ui;margin:24px;max-width:760px} label{display:block;margin-top:10px} .visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)} input,textarea{min-width:300px}</style>
</head><body><div class="job__description"><h1>Software Engineer, New Grad</h1><p>Mock Co builds developer tools. You will work on TypeScript and Python services, ship features end to end and talk to customers.</p></div><div id="root"></div>
<script>${js}</script></body></html>`);
}
copyFileSync(join(src, "lever.html"), join(out, "lever.html"));
// A company careers page embedding the Greenhouse form in an iframe.
writeFileSync(join(out, "embed.html"), `<!doctype html><html><head><meta charset="utf-8"><title>Careers at Mock Co</title></head>
<body><h1>Careers at Mock Co</h1><iframe id="grnhse_iframe" src="https://job-boards.greenhouse.io/embed/job_app?for=mockco&token=1001" width="100%" height="2600" style="border:0"></iframe></body></html>`);
console.log("built mocks into", out);
