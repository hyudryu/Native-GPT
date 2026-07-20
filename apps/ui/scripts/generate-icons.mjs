/**
 * Generates the PWA/app icons in public/icons/ — no native deps, pure JS
 * via pngjs. Renders a rounded graphite square with a warm-white "N"
 * (Apple Messages blue diagonal), supersampled 4x for anti-aliasing.
 *
 *   node scripts/generate-icons.mjs
 */
import { PNG } from "pngjs";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const outDir = join(dirname(fileURLToPath(import.meta.url)), "..", "public", "icons");
mkdirSync(outDir, { recursive: true });

const GRAPHITE = [0x1c, 0x1c, 0x1f];
const WARM_WHITE = [0xf7, 0xf4, 0xee];
const MESSAGE_BLUE = [0x00, 0x7a, 0xff];

const SS = 4; // supersampling factor

// distance from point p to segment ab
function sdSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  let t = len2 === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  const cx = ax + t * dx;
  const cy = ay + t * dy;
  return Math.hypot(px - cx, py - cy);
}

// signed distance to a rounded box centered at (cx, cy), half-size (hx, hy)
function sdRoundBox(px, py, cx, cy, hx, hy, r) {
  const qx = Math.abs(px - cx) - (hx - r);
  const qy = Math.abs(py - cy) - (hy - r);
  const ox = Math.max(qx, 0);
  const oy = Math.max(qy, 0);
  return Math.hypot(ox, oy) + Math.min(Math.max(qx, qy), 0) - r;
}

/**
 * @param {number} size output px
 * @param {{rounded: boolean, glyphScale: number}} opts
 */
function renderIcon(size, { rounded, glyphScale }) {
  const n = size * SS;
  const png = new PNG({ width: size, height: size, colorType: 6 });
  const acc = new Float64Array(size * size * 4);

  // "N" glyph strokes in normalized [0,1] space, glyphScale=1 fills nicely
  const s = glyphScale;
  const cx0 = 0.5;
  const cy0 = 0.52;
  const pt = (x, y) => [cx0 + (x - cx0) * s, cy0 + (y - cy0) * s];
  const [ltx, lty] = pt(0.32, 0.28); // left stem top
  const [lbx, lby] = pt(0.32, 0.76); // left stem bottom
  const [rtx, rty] = pt(0.68, 0.28); // right stem top
  const [rbx, rby] = pt(0.68, 0.76); // right stem bottom
  const stroke = 0.048 * s;

  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      const u = (x + 0.5) / n;
      const v = (y + 0.5) / n;

      let r = 0;
      let g = 0;
      let b = 0;
      let a = 0;

      const inBg = rounded
        ? sdRoundBox(u, v, 0.5, 0.5, 0.5, 0.5, 0.22) <= 0
        : true;
      if (inBg) {
        [r, g, b, a] = [...GRAPHITE, 255];
        const dLeft = sdSegment(u, v, ltx, lty, lbx, lby);
        const dRight = sdSegment(u, v, rtx, rty, rbx, rby);
        const dDiag = sdSegment(u, v, ltx, lty, rbx, rby);
        if (Math.min(dLeft, dRight) <= stroke) {
          [r, g, b] = WARM_WHITE;
        } else if (dDiag <= stroke * 0.85) {
          [r, g, b] = MESSAGE_BLUE;
        }
      }

      const ox = Math.min(x, size * SS - 1);
      const oy = Math.min(y, size * SS - 1);
      const di = (Math.floor(oy / SS) * size + Math.floor(ox / SS)) * 4;
      acc[di] += r;
      acc[di + 1] += g;
      acc[di + 2] += b;
      acc[di + 3] += a;
    }
  }

  const cells = SS * SS;
  for (let i = 0; i < size * size; i++) {
    const di = i * 4;
    png.data[di] = acc[di] / cells;
    png.data[di + 1] = acc[di + 1] / cells;
    png.data[di + 2] = acc[di + 2] / cells;
    png.data[di + 3] = acc[di + 3] / cells;
  }
  return png;
}

const targets = [
  // regular icons: rounded square with transparent corners
  { name: "icon-192.png", size: 192, rounded: true, glyphScale: 1 },
  { name: "icon-512.png", size: 512, rounded: true, glyphScale: 1 },
  { name: "favicon-32.png", size: 32, rounded: true, glyphScale: 1 },
  // maskable: full-bleed (the platform applies the mask), glyph inside safe zone
  { name: "icon-maskable-512.png", size: 512, rounded: false, glyphScale: 0.78 },
  // iOS touch icon: full-bleed, iOS rounds the corners itself
  { name: "apple-touch-icon.png", size: 180, rounded: false, glyphScale: 1 },
];

for (const t of targets) {
  const png = renderIcon(t.size, t);
  writeFileSync(join(outDir, t.name), PNG.sync.write(png));
  console.log(`wrote public/icons/${t.name} (${t.size}x${t.size})`);
}
