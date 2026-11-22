# Screenshots and visual review

The screenshots were rendered by `scripts/screenshots.js` (Playwright, headless Chromium) from **synthetic, non-personal fixture pages**. The text on them is set in a system font and is not handwriting. The coloured squares on some fixture pages are deliberate: they make the fixtures visually distinct.

## Coverage matrix

| Screen | Populated (1440 · 1280 · 834 · 390) | Empty (1440 · 390) | Loading (1440) | Error (1440) |
| --- | --- | --- | --- | --- |
| Sources: pages | ✓ | ✓ | ✓ | ✓ |
| Sources: page detail | ✓ | n/a | n/a | n/a |
| Sources: recognition packages | ✓ | n/a | n/a | n/a |
| Review | ✓ | ✓ | ✓ | offline banner ✓ |
| Dataset | ✓ | ✓ | ✓ | ✓ |
| Experiments (setup-required state) | ✓ | n/a | n/a | ✓ |
| Studio | ✓ | ✓ | n/a | n/a |
| Settings | ✓ | n/a | n/a | n/a |

Shared controls (buttons, badges, panels, states) appear in every screen, so they are not shot separately. File names follow `<screen>-<state>-<viewport>.jpg`. `coverage-report.json` lists every shot, the browser console errors (none) and the keyboard results.

## Keyboard checks (browser-verified)

| Check | Result |
| --- | --- |
| `J` / `K` move through the review queue | pass |
| `E` focuses the transcription field | pass |
| Typing `jjax` in the field does not trigger shortcuts | pass |
| `?` opens the shortcut help dialog | pass |
| Tab shows a visible focus ring | pass |
| `prefers-reduced-motion` sets `--dur-fast` to 0 ms | pass |

## Review rounds

Both reviews were done by independent Sonnet subagents that had not written the UI.

**Round 1: fail on 4 screens.** The failures were:
- Experiments overflowed horizontally at 390.
- Review had no way to reach the queue on narrow screens, the Approve button was not sticky, and the empty and loading inspector was blank.
- The Studio library and toolbar were cramped at 390.
- The Sources error state lost the page shell, Settings had a column gap, the Dataset header wrapped, and the card meta rows were misaligned.

All of these were fixed.

**Round 2: pass on all 8 screens.** The only notes were optional polish:
- Two primary buttons on page detail. This was fixed.
- At 834 on Experiments, the badge placement differed between the two cards.
- At 834 on Studio, the library strip was clipped.
