// The screenshot of a screen with a red frame around each difference (the failure image of a case whose screen is far
// from the design). box of a difference: { x, y, w, h } in percent of the image.
// The PNG is read and written here (zlib only): a browser's screenshot is 8-bit RGB or RGBA, not interlaced. Any other
// PNG comes back as it is, without frames.
const zlib = require('zlib');

const SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const CRC = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; }
  return t;
})();
const crc32 = (buf) => { let c = 0xffffffff; for (let i = 0; i < buf.length; i++) c = CRC[(c ^ buf[i]) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };

/** PNG bytes -> { width, height, data: RGBA }, or null when it is not an 8-bit RGB / RGBA image without interlacing. */
function decode(png) {
  if (!Buffer.isBuffer(png) || png.length < 33 || !png.subarray(0, 8).equals(SIGNATURE)) return null;
  let width = 0, height = 0, channels = 0;
  const idat = [];
  for (let p = 8; p + 12 <= png.length;) {
    const len = png.readUInt32BE(p), type = png.toString('latin1', p + 4, p + 8), body = png.subarray(p + 8, p + 8 + len);
    if (type === 'IHDR') {
      width = body.readUInt32BE(0); height = body.readUInt32BE(4);
      const depth = body[8], colour = body[9], interlace = body[12];
      if (depth !== 8 || (colour !== 2 && colour !== 6) || interlace !== 0) return null;
      channels = colour === 2 ? 3 : 4;
    } else if (type === 'IDAT') idat.push(body);
    else if (type === 'IEND') break;
    p += 12 + len;
  }
  if (!width || !height || !channels || !idat.length) return null;
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = width * channels;
  if (raw.length < (stride + 1) * height) return null;
  const data = Buffer.alloc(width * height * 4);
  const line = Buffer.alloc(stride);
  let prev = Buffer.alloc(stride);
  for (let y = 0; y < height; y++) {
    const off = y * (stride + 1), filter = raw[off];
    for (let i = 0; i < stride; i++) {
      const x = raw[off + 1 + i];
      const a = i >= channels ? line[i - channels] : 0, b = prev[i], c = i >= channels ? prev[i - channels] : 0;
      let v;
      if (filter === 0) v = x;
      else if (filter === 1) v = x + a;
      else if (filter === 2) v = x + b;
      else if (filter === 3) v = x + ((a + b) >> 1);
      else if (filter === 4) { const q = a + b - c, pa = Math.abs(q - a), pb = Math.abs(q - b), pc = Math.abs(q - c); v = x + (pa <= pb && pa <= pc ? a : pb <= pc ? b : c); }
      else return null;
      line[i] = v & 0xff;
    }
    for (let x = 0, o = y * width * 4; x < width; x++, o += 4) {
      data[o] = line[x * channels]; data[o + 1] = line[x * channels + 1]; data[o + 2] = line[x * channels + 2];
      data[o + 3] = channels === 4 ? line[x * channels + 3] : 255;
    }
    line.copy(prev);
  }
  return { width, height, data };
}

/** { width, height, data: RGBA } -> PNG bytes. */
function encode({ width, height, data }) {
  const chunk = (type, body) => {
    const out = Buffer.alloc(12 + body.length);
    out.writeUInt32BE(body.length, 0); out.write(type, 4, 'latin1'); body.copy(out, 8);
    out.writeUInt32BE(crc32(out.subarray(4, 8 + body.length)), 8 + body.length);
    return out;
  };
  const head = Buffer.alloc(13);
  head.writeUInt32BE(width, 0); head.writeUInt32BE(height, 4); head[8] = 8; head[9] = 6;
  const raw = Buffer.alloc((width * 4 + 1) * height);
  for (let y = 0; y < height; y++) data.copy(raw, y * (width * 4 + 1) + 1, y * width * 4, (y + 1) * width * 4);
  return Buffer.concat([SIGNATURE, chunk('IHDR', head), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}

/**
 * The screenshot with a frame around each difference. opts.viewportWidth: the width of the page in CSS pixels, so the
 * frame keeps its thickness on a screenshot taken at a higher pixel density.
 */
function annotate(png, diffs, opts = {}) {
  let img = null;
  try { img = decode(png); } catch { img = null; }
  if (!img) return png;
  const { width: W, height: H, data } = img;
  const scale = opts.viewportWidth > 0 ? Math.max(1, Math.round(W / opts.viewportWidth)) : 1;
  const dot = (x, y) => { if (x < 0 || y < 0 || x >= W || y >= H) return; const i = (y * W + x) * 4; data[i] = 220; data[i + 1] = 30; data[i + 2] = 30; data[i + 3] = 255; };
  for (const d of diffs || []) {
    if (!d || !d.box) continue;
    const x0 = Math.round(d.box.x / 100 * W) - 3 * scale, y0 = Math.round(d.box.y / 100 * H) - 3 * scale;
    const x1 = x0 + Math.round(d.box.w / 100 * W) + 6 * scale, y1 = y0 + Math.round(d.box.h / 100 * H) + 6 * scale;
    for (let t = 0; t < 2 * scale; t++) {
      for (let x = x0; x <= x1; x++) { dot(x, y0 + t); dot(x, y1 - t); }
      for (let y = y0; y <= y1; y++) { dot(x0 + t, y); dot(x1 - t, y); }
    }
  }
  return encode(img);
}

module.exports = { annotate, decode, encode };
