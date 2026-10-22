"""Hand-made unit vectors with exact cosines, for the retrieval tests.

The question is always `Q`, the first axis (1, 0, 0, ...). The cosine of any vector with `Q` is
then simply that vector's first component, so each expected score can be read off below. Every
component is a multiple of 1/4: float32 (how pgvector stores a `vector`) and float64 hold those
exactly, so the sums Postgres computes are exact too and no expected score is rounded.
"""

from typing import Final

from vectapilot_api.embeddings.base import STORED_DIMENSION

DIM: Final = STORED_DIMENSION


def vector(*components: float) -> tuple[float, ...]:
    """`components`, then zeros up to `DIM`."""
    return tuple(float(x) for x in components) + (0.0,) * (DIM - len(components))


Q: Final = vector(1.0)
"""The question's vector in every test."""

_TOWARD: Final[dict[float, tuple[float, ...]]] = {
    1.0: vector(1.0),  # Q itself: length 1
    0.75: vector(0.75, *[0.25] * 7),  # 0.5625 + 7 * 0.0625 = 1
    0.5: vector(0.5, 0.5, 0.5, 0.5),  # 4 * 0.25 = 1
    0.25: vector(*[0.25] * 16),  # 16 * 0.0625 = 1
    0.0: vector(0.0, 1.0),  # the second axis: at right angles to Q
    -0.5: vector(-0.5, 0.5, 0.5, 0.5),  # 4 * 0.25 = 1
    -1.0: vector(-1.0),  # the opposite direction
}


def toward(score: float) -> tuple[float, ...]:
    """A unit vector whose cosine with `Q` is exactly `score` (one of the keys above)."""
    return _TOWARD[score]
