import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import MarkdownIt from "markdown-it";
import markdownItKatex from "@vscode/markdown-it-katex";
import hljs from "highlight.js";
import { chromium } from "playwright";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const katexPlugin = markdownItKatex.default?.default ?? markdownItKatex.default ?? markdownItKatex;

if (process.argv.includes("--check")) {
  console.log(JSON.stringify({ ok: true }));
  process.exit(0);
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", chunk => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

function buildMarkdown() {
  const md = new MarkdownIt({
    html: false,
    linkify: true,
    typographer: true,
    highlight(code, lang) {
      if (lang && lang.toLowerCase() === "mermaid") {
        return `<pre class="mermaid">${escapeHtml(code)}</pre>`;
      }
      if (lang && hljs.getLanguage(lang)) {
        return `<pre class="hljs"><code>${hljs.highlight(code, { language: lang, ignoreIllegals: true }).value}</code></pre>`;
      }
      return `<pre class="hljs"><code>${md.utils.escapeHtml(code)}</code></pre>`;
    },
  });
  md.use(katexPlugin);
  return md;
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function rewriteCssUrls(css, baseDir) {
  return css.replace(/url\((['"]?)(?!data:|https?:|file:)([^)'"]+)\1\)/g, (_match, _quote, assetPath) => {
    const fileUrl = pathToFileURL(path.resolve(baseDir, assetPath)).href;
    return `url("${fileUrl}")`;
  });
}

async function buildHtml(markdown) {
  const md = buildMarkdown();
  const rendered = md.render(markdown);
  const css = await fs.readFile(path.join(__dirname, "template.css"), "utf8");
  const katexDist = path.join(__dirname, "node_modules", "katex", "dist");
  const katexCss = rewriteCssUrls(await fs.readFile(path.join(katexDist, "katex.min.css"), "utf8"), katexDist);
  const highlightCss = await fs.readFile(
    path.join(__dirname, "node_modules", "highlight.js", "styles", "github-dark.min.css"),
    "utf8",
  );
  const mermaidScript = await fs.readFile(fileURLToPath(import.meta.resolve("mermaid/dist/mermaid.min.js")), "utf8");
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>${katexCss}</style>
  <style>${highlightCss}</style>
  <style>${css}</style>
</head>
<body>
  <main class="markdown-body">${rendered}</main>
  <script>${mermaidScript}</script>
  <script>
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'default' });
    mermaid.run({ querySelector: '.mermaid' })
      .then(() => { document.body.dataset.renderReady = 'true'; })
      .catch(error => {
        document.body.dataset.renderError = error.message || String(error);
        document.body.dataset.renderReady = 'true';
      });
  </script>
</body>
</html>`;
}

async function main() {
  const input = await readStdin();
  const payload = JSON.parse(input);
  const markdown = String(payload.markdown || "");
  const outputDir = String(payload.outputDir || "data/generated/markdown");
  const viewportWidth = Number(payload.viewportWidth || 960);
  const maxHeight = Number(payload.maxHeight || 12000);

  if (!markdown.trim()) {
    throw new Error("markdown image content is empty");
  }

  await fs.mkdir(outputDir, { recursive: true });
  const id = crypto.createHash("sha256").update(markdown).update(String(Date.now())).digest("hex").slice(0, 16);
  const outputFile = path.resolve(outputDir, `markdown-${id}.png`);
  const html = await buildHtml(markdown);

  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: viewportWidth, height: 800 }, deviceScaleFactor: 2 });
    await page.setContent(html, { waitUntil: "networkidle" });
    await page.waitForFunction(() => document.body.dataset.renderReady === "true", null, { timeout: 10000 });
    const element = page.locator(".markdown-body");
    const box = await element.boundingBox();
    if (!box) {
      throw new Error("markdown body was not rendered");
    }
    if (box.height > maxHeight) {
      throw new Error(`rendered markdown is too tall: ${Math.ceil(box.height)}px > ${maxHeight}px`);
    }
    await element.screenshot({ path: outputFile });
  } finally {
    await browser.close();
  }

  console.log(JSON.stringify({ ok: true, file: outputFile }));
}

main().catch(error => {
  console.log(JSON.stringify({ ok: false, error: error.message || String(error) }));
  process.exitCode = 1;
});
