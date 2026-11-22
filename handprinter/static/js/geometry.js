// Viewport ↔ processed-page mapping for the review workspace.
// Server-side frames and transforms live in handprinter/geometry.py; the browser only
// needs this similarity transform (uniform scale + translation) over the processed frame.

/** @typedef {{x: number, y: number, w: number, h: number}} Box */

export class Viewport {
  /** @param {number} frameW @param {number} frameH */
  constructor(frameW, frameH) {
    this.frameW = frameW;
    this.frameH = frameH;
    this.scale = 1;
    this.tx = 0;
    this.ty = 0;
  }

  /** Fit the whole page into a container. @param {number} cw @param {number} ch @param {number} [pad] */
  fit(cw, ch, pad = 16) {
    this.scale = Math.min((cw - 2 * pad) / this.frameW, (ch - 2 * pad) / this.frameH);
    this.tx = (cw - this.frameW * this.scale) / 2;
    this.ty = (ch - this.frameH * this.scale) / 2;
  }

  /** Viewport (CSS px in the container) → processed frame. @param {number} vx @param {number} vy */
  toFrame(vx, vy) {
    return { x: (vx - this.tx) / this.scale, y: (vy - this.ty) / this.scale };
  }

  /** Processed frame → viewport. @param {number} fx @param {number} fy */
  toViewport(fx, fy) {
    return { x: fx * this.scale + this.tx, y: fy * this.scale + this.ty };
  }

  /** Zoom by a factor around a viewport point, keeping that point fixed. @param {number} factor @param {number} vx @param {number} vy */
  zoomAt(factor, vx, vy) {
    const next = Math.min(40, Math.max(0.02, this.scale * factor));
    const f = this.toFrame(vx, vy);
    this.scale = next;
    this.tx = vx - f.x * next;
    this.ty = vy - f.y * next;
  }

  /** Centre a frame box in the container at a readable zoom. @param {Box} box @param {number} cw @param {number} ch */
  focus(box, cw, ch) {
    const target = Math.min(cw / (box.w * 1.6), ch / (box.h * 4), 3);
    this.scale = Math.max(target, Math.min(cw / this.frameW, ch / this.frameH));
    this.tx = cw / 2 - (box.x + box.w / 2) * this.scale;
    this.ty = ch / 2 - (box.y + box.h / 2) * this.scale;
  }

  css() {
    return `translate(${this.tx}px, ${this.ty}px) scale(${this.scale})`;
  }
}

/** Normalise a drag rectangle. @param {{x:number,y:number}} a @param {{x:number,y:number}} b @returns {Box} */
export function rectFrom(a, b) {
  return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) };
}

/** Clamp a box into the frame. @param {Box} box @param {number} fw @param {number} fh @returns {Box} */
export function clampBox(box, fw, fh) {
  const x = Math.max(0, Math.min(box.x, fw - 1));
  const y = Math.max(0, Math.min(box.y, fh - 1));
  return { x, y, w: Math.max(1, Math.min(box.w, fw - x)), h: Math.max(1, Math.min(box.h, fh - y)) };
}
