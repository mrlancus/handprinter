"""Align a line transcription to detected word boxes.

Dynamic programming over (tokens, boxes) with explicit moves:

- 1:1   token ↔ box, cost = |log(box width / expected width)|
- 2:1   two tokens in one box (words written together, or under-segmentation)
- 1:2   one token over two boxes (wide gap inside a word, or over-segmentation)
- 1:0   token with no box (missed detection)
- 0:1   box with no token (stray mark, noise, or an unread word)

Expected width comes from the line's own average character width, so slant and spacing
are never normalised. Any move other than a well-fitting 1:1 is flagged for review.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MERGE_PENALTY = 1.2
SPLIT_PENALTY = 1.2
SKIP_TOKEN_PENALTY = 2.5
SKIP_BOX_PENALTY = 2.5
POOR_FIT = math.log(2.2)


@dataclass
class BoxAssignment:
    box_index: int
    text: str | None
    move: str  # "1:1" | "2:1" | "1:2" | "0:1"
    ambiguous: bool
    reason: str | None = None


@dataclass
class Alignment:
    boxes: list[BoxAssignment]
    unplaced_tokens: list[str]
    ambiguous: bool
    cost: float


def tokenize(text: str) -> list[str]:
    return [t for t in text.split() if t]


def align(text: str, box_widths: list[float]) -> Alignment:
    tokens = tokenize(text)
    n, m = len(tokens), len(box_widths)
    if n == 0 or m == 0:
        return Alignment(
            boxes=[BoxAssignment(j, None, "0:1", True, "no transcription to align") for j in range(m)],
            unplaced_tokens=tokens, ambiguous=bool(tokens or box_widths), cost=float("inf") if (n or m) else 0.0,
        )
    chars = sum(len(t) for t in tokens)
    char_w = max(1e-6, sum(box_widths) / max(1, chars))

    def fit(token_chars: float, width: float) -> float:
        return abs(math.log(max(width, 1e-6) / max(token_chars * char_w, 1e-6)))

    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int, str] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            base = cost[i][j]
            if base == inf:
                continue
            moves = []
            if i < n and j < m:
                moves.append((i + 1, j + 1, "1:1", fit(len(tokens[i]), box_widths[j])))
            if i + 1 < n and j < m:
                moves.append((i + 2, j + 1, "2:1", MERGE_PENALTY + fit(len(tokens[i]) + len(tokens[i + 1]) + 0.5, box_widths[j])))
            if i < n and j + 1 < m:
                moves.append((i + 1, j + 2, "1:2", SPLIT_PENALTY + fit(len(tokens[i]), box_widths[j] + box_widths[j + 1])))
            if i < n:
                moves.append((i + 1, j, "1:0", SKIP_TOKEN_PENALTY))
            if j < m:
                moves.append((i, j + 1, "0:1", SKIP_BOX_PENALTY))
            for ni, nj, move, c in moves:
                if base + c < cost[ni][nj]:
                    cost[ni][nj] = base + c
                    back[ni][nj] = (i, j, move)

    steps = []
    i, j = n, m
    while (i, j) != (0, 0):
        prev = back[i][j]
        assert prev is not None
        steps.append((prev[0], prev[1], prev[2]))
        i, j = prev[0], prev[1]
    steps.reverse()

    boxes: list[BoxAssignment] = []
    unplaced: list[str] = []
    for ti, bj, move in steps:
        if move == "1:1":
            poor = fit(len(tokens[ti]), box_widths[bj]) > POOR_FIT
            boxes.append(BoxAssignment(bj, tokens[ti], move, poor, "word width fits the text poorly" if poor else None))
        elif move == "2:1":
            boxes.append(BoxAssignment(bj, f"{tokens[ti]} {tokens[ti + 1]}", move, True, "box seems to hold two words; split it?"))
        elif move == "1:2":
            reason = f"'{tokens[ti]}' seems to span two boxes; merge them?"
            boxes.append(BoxAssignment(bj, None, move, True, reason))
            boxes.append(BoxAssignment(bj + 1, None, move, True, reason))
        elif move == "1:0":
            unplaced.append(tokens[ti])
        else:
            boxes.append(BoxAssignment(bj, None, move, True, "no text aligned to this box"))
    ambiguous = n != m or bool(unplaced) or any(b.ambiguous for b in boxes)
    return Alignment(sorted(boxes, key=lambda b: b.box_index), unplaced, ambiguous, cost[n][m])
