// Tiny DOM builder. Text is always inserted as text nodes, never parsed as HTML.

/**
 * @typedef {Record<string, any>} Attrs
 * @typedef {Node | string | number | null | undefined | false | any[]} Child
 */

/**
 * Create an element.
 * @template {keyof HTMLElementTagNameMap} K
 * @param {K} tag
 * @param {Attrs} [attrs]
 * @param {...Child} children
 * @returns {HTMLElementTagNameMap[K]}
 */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.className = String(value);
    else if (key === 'text') el.textContent = String(value);
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, String(value));
  }
  append(el, children);
  return el;
}

/** @param {Node} parent @param {Child[]} children */
export function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** Replace all children. @param {Element} el @param {...Child} children */
export function mount(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

/** SVG element in the SVG namespace. @param {string} tag @param {Attrs} [attrs] @param {...Node} children */
export function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'text') el.textContent = String(value);
    else el.setAttribute(key, String(value));
  }
  for (const c of children) el.appendChild(c);
  return el;
}

/** True when keyboard shortcuts must not fire (user is typing). @param {EventTarget | null} target */
export function isTyping(target) {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

/** Format a number with thousands separators. @param {number} n */
export const fmt = (n) => Number(n || 0).toLocaleString();

/** Debounce. @template {(...a: any[]) => void} F @param {F} fn @param {number} ms */
export function debounce(fn, ms) {
  /** @type {number | undefined} */
  let t;
  return /** @type {F} */ ((...args) => { clearTimeout(t); t = window.setTimeout(() => fn(...args), ms); });
}
