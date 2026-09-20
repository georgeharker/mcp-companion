#!/usr/bin/env node
// Regenerates assets/preview.png — the pi.dev package-gallery card (1280x800, 16:10).
// The committed PNG is what ships (npm + cdn.jsdelivr.net/npm/<pkg>/assets/preview.png).
// Re-run after changing the package name, spec, or hero art:
//   node scripts/make-preview.mjs
// Needs one of: rsvg-convert | resvg | magick on PATH.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync, unlinkSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

// ---------------- card spec (edit here, then re-run) ----------------
const SPEC = { accent: "#a428f0", hero: { image: "../../docs/assets/logo.png" } };
// accent: band rule + chip color
// hero: { image: "path/relative/to/package/root" }
//   or { word: "...", size: 200, underline: { y: 490, width: 210 } }
// ---------------------------------------------------------------------

const W = 1280, H = 800, BAND_Y = 720;
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function pngSize(p) {
  const b = readFileSync(p);
  if (b.readUInt32BE(12) !== 0x49484452) throw new Error(`${p}: not a PNG (IHDR expected)`);
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) };
}

function pngDataUri(p) {
  return `data:image/png;base64,${readFileSync(p).toString("base64")}`;
}

let hero = "";
if (SPEC.hero.image) {
  const p = join(root, SPEC.hero.image);
  const { w, h } = pngSize(p);
  const hh = 720, ww = Math.round((720 * w) / h);
  hero = `<image x="${(W - ww) / 2}" y="0" width="${ww}" height="${hh}" href="${pngDataUri(p)}"/>`;
} else {
  const { word, size, underline } = SPEC.hero;
  const baseline = 360 + Math.round(0.35 * size);
  const ul = underline ?? { y: 490, width: Math.round(size * word.length * 0.21) };
  hero =
    `<text x="640" y="${baseline}" text-anchor="middle" font-family="Source Code Pro" font-weight="700" font-size="${size}" fill="${SPEC.accent}">${esc(word)}</text>` +
    `<rect x="${(W - ul.width) / 2}" y="${ul.y}" width="${ul.width}" height="5" fill="${SPEC.accent}"/>`;
}

const nameSize = pkg.name.length > 28 ? 48 : 54;
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
<rect width="${W}" height="${H}" fill="#000000"/>
${hero}
<rect x="0" y="${BAND_Y}" width="${W}" height="${H - BAND_Y}" fill="#0b0b0e"/>
<rect x="0" y="${BAND_Y - 3}" width="${W}" height="3" fill="${SPEC.accent}"/>
<text x="40" y="770" font-family="Source Code Pro" font-weight="600" font-size="${nameSize}" fill="#ececec">${esc(pkg.name)}</text>
<rect x="1046" y="736" width="194" height="48" rx="24" fill="#17171c" stroke="${SPEC.accent}" stroke-width="2"/>
<text x="1143" y="767" text-anchor="middle" font-family="Source Code Pro" font-size="26" fill="${SPEC.accent}">pi-package</text>
</svg>`;

mkdirSync(join(root, "assets"), { recursive: true });
const svgPath = join(root, "assets", ".preview.tmp.svg");
const outPath = join(root, "assets", "preview.png");
writeFileSync(svgPath, svg);
const renderers = [
  ["rsvg-convert", ["-w", String(W), "-h", String(H), svgPath, "-o", outPath]],
  ["resvg", ["--width", String(W), svgPath, outPath]],
  ["magick", ["-background", "none", svgPath, outPath]],
];
for (const [cmd, args] of renderers) {
  try {
    execFileSync(cmd, args, { stdio: "pipe" });
    unlinkSync(svgPath);
    console.log(`preview.png rendered with ${cmd} -> assets/preview.png`);
    process.exit(0);
  } catch (e) {
    if (e.code !== "ENOENT") { console.error(`${cmd} failed:`, e.stderr?.toString() || e.message); }
  }
}
unlinkSync(svgPath);
console.error("no SVG rasterizer found (install rsvg-convert, resvg, or imagemagick)");
process.exit(1);
