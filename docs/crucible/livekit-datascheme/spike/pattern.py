"""Known-answer test pattern for the webrtc:// frame path.

The Fold's claim E said "publish a known pattern, subscribe, assert pixels match"
(the self-referential-test-blindness discipline: never assert a codec against its own
inverse). That discipline is right, but the assertion it proposed is not achievable:
WebRTC carries video over VP8/H264, which are LOSSY. A byte-equality assert on a
round-tripped frame fails by construction, no matter how correct the adapter is.

So the pattern is built to be MEASURABLE THROUGH a lossy codec rather than identical
across it:

  * Large flat colour blocks, not fine detail — a block codec preserves block means
    almost exactly, so a channel-order bug (RGB vs BGR) shows up as a huge error while
    ordinary compression noise stays small. This is what makes the test discriminate.
  * A per-quadrant colour signature that is UNIQUE under permutation: if the adapter
    swaps R and B, or flips rows, the quadrant means move to the wrong quadrant and the
    error explodes. A symmetric pattern would be blind to exactly the bugs we care about.
  * A coarse binary sequence marker in the bottom strip, so a frame can be identified
    after transport reorders or drops frames.

`compare()` returns per-quadrant mean absolute error in 0-255 units. Interpretation
thresholds live with the caller, not here.
"""
from __future__ import annotations

import numpy as np

# Four quadrant colours, chosen so that ANY channel permutation or spatial flip moves at
# least one quadrant far from its expected value. Deliberately not grey, not symmetric.
QUADRANT_RGB = [
    (220, 30, 30),    # top-left     red
    (30, 200, 60),    # top-right    green
    (40, 60, 210),    # bottom-left  blue
    (230, 200, 40),   # bottom-right yellow
]

MARKER_ROWS = 16   # bottom strip height reserved for the sequence marker
MARKER_BITS = 8


def make(width: int, height: int, seq: int = 0) -> np.ndarray:
    """Build the HxWx3 uint8 RGB test frame for sequence number `seq`."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    mid_y, mid_x = height // 2, width // 2
    img[:mid_y, :mid_x] = QUADRANT_RGB[0]
    img[:mid_y, mid_x:] = QUADRANT_RGB[1]
    img[mid_y:, :mid_x] = QUADRANT_RGB[2]
    img[mid_y:, mid_x:] = QUADRANT_RGB[3]

    # Sequence marker: MARKER_BITS wide cells across the bottom strip, white = 1, black = 0.
    strip = img[height - MARKER_ROWS:, :]
    cell = max(1, width // MARKER_BITS)
    for bit in range(MARKER_BITS):
        on = (seq >> bit) & 1
        x0 = bit * cell
        x1 = width if bit == MARKER_BITS - 1 else (bit + 1) * cell
        strip[:, x0:x1] = 255 if on else 0
    return img


def read_seq(img: np.ndarray) -> int:
    """Recover the sequence number from a (possibly lossily compressed) frame."""
    height, width = img.shape[:2]
    strip = img[height - MARKER_ROWS:, :]
    cell = max(1, width // MARKER_BITS)
    seq = 0
    for bit in range(MARKER_BITS):
        x0 = bit * cell
        x1 = width if bit == MARKER_BITS - 1 else (bit + 1) * cell
        # Sample the centre of the cell; codec ringing is worst at block edges.
        pad = max(1, (x1 - x0) // 4)
        mean = float(strip[:, x0 + pad:x1 - pad].mean())
        if mean > 127:
            seq |= 1 << bit
    return seq


def compare(expected: np.ndarray, actual: np.ndarray) -> dict:
    """Per-quadrant mean absolute error, ignoring the marker strip.

    Quadrant means are the discriminating statistic: they survive lossy compression but
    NOT a channel swap, a row flip, or a stride mis-read.
    """
    if expected.shape != actual.shape:
        return {"shape_mismatch": [list(expected.shape), list(actual.shape)]}
    height, width = expected.shape[:2]
    body = slice(0, height - MARKER_ROWS)
    mid_y, mid_x = height // 2, width // 2
    quads = {
        "top_left": (slice(0, mid_y), slice(0, mid_x)),
        "top_right": (slice(0, mid_y), slice(mid_x, width)),
        "bottom_left": (slice(mid_y, height - MARKER_ROWS), slice(0, mid_x)),
        "bottom_right": (slice(mid_y, height - MARKER_ROWS), slice(mid_x, width)),
    }
    out = {}
    for name, (ys, xs) in quads.items():
        e = expected[ys, xs].astype(np.int16)
        a = actual[ys, xs].astype(np.int16)
        out[name] = round(float(np.abs(e - a).mean()), 2)
    whole_e = expected[body].astype(np.int16)
    whole_a = actual[body].astype(np.int16)
    out["overall_mae"] = round(float(np.abs(whole_e - whole_a).mean()), 2)
    out["max_quadrant_mae"] = round(max(v for k, v in out.items() if k.startswith(("top", "bottom"))), 2)
    return out
