# Design system

The single token file is [`handprinter/static/css/tokens.css`](../handprinter/static/css/tokens.css). Components use only those variables. Shared controls are in `base.css`, the app shell in `layout.css`, and per-view layout in `views.css`.

`frontend-design` was not available as a skill in the build session. These rules come from the brief (§10) and were applied by hand.

## Direction

An understated editorial and technical workbench. The page is a warm off-white canvas (`--c-canvas`), with crisp white panels, near-black text and fine 1 px borders. Handwriting images sit on a slightly darker workspace tone (`--c-workspace`), so the paper stays distinguishable.

Page titles use a book serif (`--font-display`). Everything else uses the system UI stack. Monospace is used for IDs and paths.

## Colour meaning

| Token | Meaning | Use |
| --- | --- | --- |
| `--c-accent` (forest) | Primary action | One primary button per area, approved state, focus ring |
| `--c-review*` (amber) | Needs review | Flags, `needs_review` badges, flagged outlines on images |
| `--c-error*` (red) | Error | Failed jobs, rejected files, offline banner |
| neutrals | Everything else | Status like proposed, unprocessed, excluded and blocked metadata |

A state never relies on colour alone. Badges always carry a word, and usually an icon too. Region outlines differ by dash pattern as well as colour (solid words, dashed lines, dotted marks, short-dashed blocks).

**Avoid:** gradients, glassmorphism, oversized pill cards, emoji, decorative shadows (only popovers and dialogs use `--shadow-pop`), rainbow palettes and filler statistics.

## Type scale

| Token | Size |
| --- | --- |
| `--fs-xs` | 12 px |
| `--fs-sm` | 13 px |
| `--fs-md` | 14 px (UI base) |
| `--fs-lg` | 16 px |
| `--fs-xl` | 20 px |
| `--fs-2xl` | 26 px (page titles) |

Weights are 400, 550 and 650.

## Spacing, radii and controls

- **Spacing:** a 4 px rhythm, `--sp-1` (4) to `--sp-7` (48).
- **Radii:** 3 px and 5 px only.
- **Controls:** 32 px high (`--control-h`). Navigation targets are at least 44 px (`--control-h-touch`).
- **Focus:** `--focus-ring` is a 2 px panel-coloured gap and then a 2 px accent ring, and it shows on every focusable element via `:focus-visible`.

## Components

| Class | Purpose |
| --- | --- |
| `.btn` with `--primary`, `--quiet`, `--danger`, `--sm` | Buttons. Shortcuts are shown with `<kbd>` inside the button. |
| `.badge` with `--review`, `--error`, `--ok`, `--outline` | Status and flags. |
| `.panel` with `__head` and `__body` | Every grouped area. |
| `.table` | Dense data, with tabular numbers. |
| `.segmented` | Radio groups shown as segmented controls. |
| `.callout` with `--review` and `--error` | Inline explanations. |
| `.progress` with `--review` and `--neutral` | Progress bars. |
| `.state`, from `components/states.js` | Empty, loading (skeleton), error (with retry) and setup-required states, all built from one helper so they share the same polish. |
| `.dialog` | Native `<dialog>` with the `::backdrop` token. |

## Layout

- **Desktop:** a 76 px navigation rail plus the main area. Review uses three columns (queue 280, workspace, inspector 360), and Studio uses library, canvas and properties.
- **≤ 1100 px:** Review and Studio stack, with the image workspace kept first and the inspector directly below it.
- **≤ 900 px:** the rail becomes a top bar.
- **≤ 600 px:** secondary toolbar items are hidden.
- **Landing page:** there is none. The app opens on Sources.

## Motion and accessibility

- The only motion is short colour transitions (`--dur-fast`) and skeleton shimmer. Both are disabled under `prefers-reduced-motion`.
- Keyboard shortcuts never fire while focus is in an input, a select or a textarea (`isTyping()` in `dom.js`).
- Canvas and SVG views always have equivalent controls outside them: the element list and numeric fields in Studio, and the inspector and shortcuts in Review.
