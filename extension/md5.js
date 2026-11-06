// Incremental MD5, so a finished upload can be checked against Drive's md5Checksum.
// (WebCrypto has no MD5, and Drive reports no other hash for regular uploads.)

const S = [7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21];
const K = Int32Array.from({ length: 64 }, (_, i) => Math.floor(Math.abs(Math.sin(i + 1)) * 2 ** 32) | 0);
const G = Uint8Array.from({ length: 64 }, (_, i) =>
  i < 16 ? i : i < 32 ? (5 * i + 1) % 16 : i < 48 ? (3 * i + 5) % 16 : (7 * i) % 16);

export class MD5 {
  constructor() {
    this.h = Int32Array.of(0x67452301, 0xefcdab89 | 0, 0x98badcfe | 0, 0x10325476);
    this.buf = new Uint8Array(64);
    this.bufLen = 0;
    this.length = 0;
    this.m = new Int32Array(16);
  }

  update(bytes) {
    this.length += bytes.length;
    let i = 0;
    if (this.bufLen) {
      const take = Math.min(64 - this.bufLen, bytes.length);
      this.buf.set(bytes.subarray(0, take), this.bufLen);
      this.bufLen += take;
      i = take;
      if (this.bufLen < 64) return this;
      this.block(new DataView(this.buf.buffer), 0);
      this.bufLen = 0;
    }
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    for (; i + 64 <= bytes.length; i += 64) this.block(view, i);
    this.buf.set(bytes.subarray(i));
    this.bufLen = bytes.length - i;
    return this;
  }

  block(view, off) {
    const m = this.m, h = this.h;
    for (let j = 0; j < 16; j++) m[j] = view.getInt32(off + 4 * j, true);
    let a = h[0], b = h[1], c = h[2], d = h[3];
    for (let i = 0; i < 64; i++) {
      const f = i < 16 ? (b & c) | (~b & d)
        : i < 32 ? (d & b) | (~d & c)
        : i < 48 ? b ^ c ^ d
        : c ^ (b | ~d);
      const s = S[(i >> 4) * 4 + (i & 3)];
      const x = (a + f + K[i] + m[G[i]]) | 0;
      a = d; d = c; c = b;
      b = (b + ((x << s) | (x >>> (32 - s)))) | 0;
    }
    h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0; h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
  }

  hex() {
    const bits = this.length * 8;
    const padLen = ((this.bufLen < 56 ? 56 : 120) - this.bufLen);
    const pad = new Uint8Array(padLen + 8);
    pad[0] = 0x80;
    const tail = new DataView(pad.buffer, padLen);
    tail.setUint32(0, bits % 2 ** 32, true);
    tail.setUint32(4, Math.floor(bits / 2 ** 32), true);
    this.update(pad);
    const out = new DataView(this.h.buffer);
    let hex = "";
    for (let i = 0; i < 16; i++) hex += out.getUint8(i).toString(16).padStart(2, "0");
    return hex;
  }
}
