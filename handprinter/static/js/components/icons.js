// Stroke icons (24×24 grid, 1.6 px strokes), drawn for this app. No emoji, no icon font.
import { svg } from '../dom.js';

/** @type {Record<string, string[]>} */
const PATHS = {
  brand: ['M4 18c3-1 4-9 7-9s2 6 4 6 2-8 5-8', 'M3 21h18'],
  sources: ['M4 7h12v13H4z', 'M8 3h12v13', 'M7 15l3-3 2 2 2-2 2 2'],
  review: ['M4 5h16v11H4z', 'M8 20h8', 'M8 9h5', 'M8 12h8'],
  dataset: ['M4 5h16v4H4z', 'M4 11h16v4H4z', 'M4 17h16v3H4z'],
  experiments: ['M9 3h6', 'M10 3v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3', 'M7 15h10'],
  studio: ['M6 3h12v18H6z', 'M9 8h6', 'M9 12h4', 'M9 16h5'],
  settings: ['M4 7h10', 'M18 7h2', 'M4 17h4', 'M12 17h8', 'M14 5v4', 'M8 15v4'],
  upload: ['M12 16V4', 'M7 9l5-5 5 5', 'M4 20h16'],
  check: ['M5 12l5 5 9-10'],
  x: ['M6 6l12 12', 'M18 6L6 18'],
  defer: ['M12 7v5l3 2', 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z'],
  undo: ['M9 14L4 9l5-5', 'M4 9h11a5 5 0 0 1 0 10h-3'],
  redo: ['M15 14l5-5-5-5', 'M20 9H9a5 5 0 0 0 0 10h3'],
  zoomIn: ['M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z', 'M20 20l-4-4', 'M8 11h6', 'M11 8v6'],
  zoomOut: ['M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z', 'M20 20l-4-4', 'M8 11h6'],
  fit: ['M4 9V4h5', 'M15 4h5v5', 'M20 15v5h-5', 'M9 20H4v-5'],
  alert: ['M12 4l9 16H3z', 'M12 10v4', 'M12 17v.5'],
  info: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 11v6', 'M12 7.5v.5'],
  split: ['M12 3v18', 'M4 7h5v10H4', 'M20 7h-5v10h5'],
  merge: ['M4 7h16v10H4z', 'M9 10l3 2-3 2', 'M15 10l-3 2 3 2'],
  plus: ['M12 5v14', 'M5 12h14'],
  trash: ['M4 7h16', 'M9 7V4h6v3', 'M6 7l1 13h10l1-13'],
  download: ['M12 4v12', 'M7 11l5 5 5-5', 'M4 20h16'],
  eye: ['M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z', 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z'],
  flag: ['M5 21V4', 'M5 4h11l-2 4 2 4H5'],
  left: ['M15 5l-7 7 7 7'],
  right: ['M9 5l7 7-7 7'],
  play: ['M7 5l12 7-12 7z'],
  draw: ['M4 20h4L19 9l-4-4L4 16z', 'M13 7l4 4'],
  move: ['M12 3v18', 'M3 12h18', 'M9 6l3-3 3 3', 'M9 18l3 3 3-3', 'M6 9l-3 3 3 3', 'M18 9l3 3-3 3'],
  person: ['M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M4 21c1-4 4-6 8-6s7 2 8 6'],
  save: ['M5 4h11l3 3v13H5z', 'M8 4v5h7V4', 'M8 20v-6h8v6'],
  open: ['M4 6h6l2 2h8v11H4z'],
  layout: ['M4 5h16', 'M4 10h10', 'M4 15h16', 'M4 20h7'],
  link: ['M10 14a4 4 0 0 0 6 0l3-3a4 4 0 0 0-6-6l-1 1', 'M14 10a4 4 0 0 0-6 0l-3 3a4 4 0 0 0 6 6l1-1'],
  external: ['M14 4h6v6', 'M20 4l-9 9', 'M18 14v6H4V6h6'],
  refresh: ['M20 11a8 8 0 0 0-14-5l-2 2', 'M4 4v4h4', 'M4 13a8 8 0 0 0 14 5l2-2', 'M20 20v-4h-4'],
  send: ['M4 12l16-8-6 16-3-7z', 'M11 13l9-9'],
  wifiOff: ['M3 3l18 18', 'M8.5 16.5a5 5 0 0 1 7 0', 'M5 13a10 10 0 0 1 5-2.7', 'M19 13a10 10 0 0 0-3-2', 'M12 20v.5'],
};

/**
 * @param {string} name
 * @param {{label?: string, class?: string}} [opts]
 * @returns {SVGSVGElement}
 */
export function icon(name, opts = {}) {
  const el = /** @type {SVGSVGElement} */ (svg('svg', {
    viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.6,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round', class: opts.class,
    'aria-hidden': opts.label ? undefined : 'true', role: opts.label ? 'img' : undefined, 'aria-label': opts.label,
  }, ...(PATHS[name] || PATHS.info).map((d) => svg('path', { d }))));
  return el;
}
