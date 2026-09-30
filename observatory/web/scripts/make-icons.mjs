// Generates the PWA icons into public/ with no image dependencies: four
// concentric ring arcs in the ring colours on the terminal background.
// Run: npm run icons   (the output is committed; the build does not run this)
import { deflateSync } from "node:zlib";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const OUT = join(dirname(fileURLToPath(import.meta.url)), "..", "public");

const BG = [8, 11, 16];
const RING_COLORS = [
  [244, 114, 182], // ring 0
  [56, 189, 248], // ring 1
  [167, 139, 250], // ring 2
  [45, 212, 191], // ring 3
];

// ---- PNG encoding -----------------------------------------------------------
const CRC_TABLE = new Uint32Array(256).map((_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
function crc32(buf) {
  let c = 0xffffffff;
  for (const byte of buf) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}
function png(size, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  const raw = Buffer.alloc(size * (size * 4 + 1));
  for (let y = 0; y < size; y++) {
    raw[y * (size * 4 + 1)] = 0; // filter: none
    rgba.copy(raw, y * (size * 4 + 1) + 1, y * size * 4, (y + 1) * size * 4);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

// ---- drawing (4x4 supersampling) ----------------------------------------------
// `scale` shrinks the artwork (maskable icons keep it inside the safe zone);
// `rounded` gives a transparent-cornered tile, otherwise the square is full bleed.
function draw(size, { scale = 1, rounded = true } = {}) {
  const rgba = Buffer.alloc(size * size * 4);
  const S = 4;
  const c = size / 2;
  const unit = (size / 2) * scale;
  const rings = [0.2, 0.42, 0.64, 0.86].map((r) => r * unit);
  const width = 0.11 * unit;
  const gapStart = [-0.2, 0.9, 2.0, 3.1]; // radians where each ring's gap sits
  const corner = size * 0.22;

  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let r = 0, g = 0, b = 0, a = 0;
      for (let sy = 0; sy < S; sy++) {
        for (let sx = 0; sx < S; sx++) {
          const px = x + (sx + 0.5) / S;
          const py = y + (sy + 0.5) / S;
          if (rounded) {
            const qx = Math.max(corner - px, px - (size - corner), 0);
            const qy = Math.max(corner - py, py - (size - corner), 0);
            if (qx * qx + qy * qy > corner * corner) continue; // outside the tile
          }
          let col = BG;
          const dx = px - c;
          const dy = py - c;
          const d = Math.hypot(dx, dy);
          const ang = Math.atan2(dy, dx);
          for (let i = 0; i < 4; i++) {
            if (Math.abs(d - rings[i]) <= width / 2) {
              let delta = ang - gapStart[i];
              delta = ((delta % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
              if (delta > 0.9) col = RING_COLORS[i]; // leave a 0.9 rad gap per ring
            }
          }
          if (d <= width * 0.55) col = [230, 233, 239]; // the core
          r += col[0]; g += col[1]; b += col[2]; a += 255;
        }
      }
      const n = S * S;
      const o = (y * size + x) * 4;
      rgba[o] = Math.round(r / n);
      rgba[o + 1] = Math.round(g / n);
      rgba[o + 2] = Math.round(b / n);
      rgba[o + 3] = Math.round(a / n);
    }
  }
  return png(size, rgba);
}

function ico(pngBuf, size) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2); // icon
  header.writeUInt16LE(1, 4); // one image
  const entry = Buffer.alloc(16);
  entry[0] = size;
  entry[1] = size;
  entry.writeUInt16LE(1, 4); // planes
  entry.writeUInt16LE(32, 6); // bpp
  entry.writeUInt32LE(pngBuf.length, 8);
  entry.writeUInt32LE(6 + 16, 12);
  return Buffer.concat([header, entry, pngBuf]);
}

function write(name, buf) {
  const path = join(OUT, name);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, buf);
  console.log(`${name}  ${buf.length} bytes`);
}

write("icons/icon-192.png", draw(192));
write("icons/icon-512.png", draw(512));
write("icons/icon-maskable-512.png", draw(512, { scale: 0.78, rounded: false }));
write("apple-touch-icon.png", draw(180, { rounded: false }));
write("favicon.ico", ico(draw(32), 32));
