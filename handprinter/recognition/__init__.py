"""Recognition via Claude Code: package building, result validation and import."""

PROMPT_VERSION = "v2"
SCHEMA_VERSION = "1"
# Claude vision guidance: ≤1568 px long edge avoids downscaling; ≤8000 px hard limit
# (2000 px when many images share a request). See docs/architecture.md.
MAX_LONG_EDGE = 1568
MAX_PIXELS = 1_150_000
