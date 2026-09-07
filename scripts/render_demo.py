"""Render `docs/assets/demo.svg`, the animated terminal walkthrough in the README.

An animated SVG rather than a GIF: it is a tenth of the size, stays crisp at any
zoom, renders as text in a diff, and needs no recording tool to regenerate.

    python scripts/render_demo.py

Nothing in the package imports this. It exists so the asset can be changed by
editing the script rather than by re-recording a terminal.
"""

from __future__ import annotations

from pathlib import Path

# -- theme --------------------------------------------------------------------

BG = "#0d1117"
CHROME = "#161b22"
BORDER = "#30363d"
FG = "#c9d1d9"
DIM = "#7d8590"
GREEN = "#3fb950"
PURPLE = "#a371f7"
BLUE = "#58a6ff"
YELLOW = "#d29922"
WHITE = "#f0f6fc"

CHAR_W = 9.0
LINE_H = 26.0
PAD_X = 26.0
TOP = 84.0
COLS = 92
CYCLE = 26.0  # seconds for one full loop
HOLD = 3.2  # seconds the finished frame stays up before restarting

# -- content ------------------------------------------------------------------
# Each line is a list of (text, colour) spans, or () for a blank line.

Span = tuple[str, str]
Line = tuple[Span, ...]

LINES: tuple[Line, ...] = (
    (("$ ", GREEN), ("pip install emalia", WHITE)),
    (("$ ", GREEN), ("emalia init", WHITE)),
    (("Wrote emalia.toml", GREEN),),
    (("Wrote .env", GREEN),),
    ((),),
    (("$ ", GREEN), ("emalia check", WHITE)),
    (("Mail servers", WHITE),),
    (("  imap: ", FG), ("ok", GREEN)),
    (("  smtp: ", FG), ("ok", GREEN)),
    (("Model", WHITE),),
    (("  anthropic / claude-sonnet-4-6   credentials: ", FG), ("ok", GREEN)),
    (("Policy", WHITE),),
    (("  ", FG), ("valid", GREEN), ("   senders: you@example.com   tools: email, file_read", FG)),
    (("Everything checks out.", GREEN),),
    ((),),
    (("$ ", GREEN), ("emalia run", WHITE)),
    (("Emalia is watching assistant@gmail.com. Ctrl-C to stop.", DIM),),
    ((),),
    (("mail  ", PURPLE), ('you@example.com  "quarterly numbers"', FG)),
    (("  gate  ", BLUE), ("accepted", GREEN), ("  allowlisted sender", DIM)),
    (("  tool  ", BLUE), ("list_directory", YELLOW), ("('~/reports')", FG)),
    (("  tool  ", BLUE), ("read_file", YELLOW), ("('2026-Q3-summary.xlsx')", FG)),
    (("  send  ", BLUE), ("replied in thread", GREEN), ("  2 tools  4.1s", DIM)),
)

# Lines that start a new command get a beat before them, so the animation reads
# as three separate things happening rather than one long scroll.
PAUSE_BEFORE = {1, 5, 15, 18}


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _schedule() -> list[float]:
    """Return the start time of each line, in seconds."""
    weights = [(2.6 if index in PAUSE_BEFORE else 1.0) for index in range(len(LINES))]
    total = sum(weights)
    budget = CYCLE - HOLD
    starts: list[float] = []
    elapsed = 0.0
    for weight in weights:
        starts.append(elapsed)
        elapsed += weight / total * budget
    return starts


def _line_svg(index: int, spans: Line) -> str:
    y = TOP + index * LINE_H
    parts = [f'<text class="m l{index}" y="{y:.1f}" xml:space="preserve">']
    column = 0
    for span in spans:
        if not span:
            continue
        text, colour = span
        width = len(text) * CHAR_W
        parts.append(
            f'<tspan x="{PAD_X + column * CHAR_W:.1f}" fill="{colour}" '
            f'textLength="{width:.1f}" lengthAdjust="spacingAndGlyphs">{_escape(text)}</tspan>'
        )
        column += len(text)
    parts.append("</text>")
    return "".join(parts)


def build() -> str:
    """Return the complete SVG document."""
    starts = _schedule()
    height = TOP + len(LINES) * LINE_H + 22
    width = PAD_X * 2 + COLS * CHAR_W

    keyframes: list[str] = []
    for index, start in enumerate(starts):
        # Percentages of the cycle: invisible, then a short fade in, then held
        # until the loop restarts.
        at = start / CYCLE * 100
        fade = min(at + 1.2, 99.0)
        # The first line has no lead-in, so emitting "0%,0%" as a selector list
        # would be a duplicate selector. Browsers forgive it; validators do not.
        hidden = "0%" if at <= 0 else f"0%,{at:.2f}%"
        keyframes.append(
            f"@keyframes k{index}{{{hidden}{{opacity:0}}"
            f"{fade:.2f}%,100%{{opacity:1}}}}"
            f".l{index}{{opacity:0;animation:k{index} {CYCLE}s linear infinite}}"
        )

    cursor_at = (starts[-1] + 0.6) / CYCLE * 100
    cursor_y = TOP + len(LINES) * LINE_H
    keyframes.append(
        f"@keyframes blink{{0%,{cursor_at:.2f}%{{opacity:0}}"
        f"{min(cursor_at + 0.1, 99.0):.2f}%,100%{{opacity:1}}}}"
        f".cursor{{opacity:0;animation:blink {CYCLE}s steps(1) infinite}}"
    )

    body = "\n  ".join(_line_svg(index, spans) for index, spans in enumerate(LINES))

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" \
width="{width:.0f}" height="{height:.0f}" role="img" \
aria-label="Terminal recording: emalia init, emalia check reporting IMAP, SMTP, model and policy \
all healthy, then emalia run answering an incoming email using two tools">
  <title>emalia init, check, run</title>
  <defs><style>
    .m {{ font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace; font-size: 15px; }}
    .chrome {{ font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 13px; }}
    {chr(10).join("    " + line for line in keyframes)}
  </style></defs>

  <rect width="{width:.0f}" height="{height:.0f}" rx="10" fill="{BG}" stroke="{BORDER}"/>
  <path d="M0 40 V10 a10 10 0 0 1 10 -10 H{width - 10:.0f} a10 10 0 0 1 10 10 V40 Z" fill="{CHROME}"/>
  <line x1="0" y1="40" x2="{width:.0f}" y2="40" stroke="{BORDER}"/>
  <circle cx="22" cy="20" r="6" fill="#ff5f57"/>
  <circle cx="42" cy="20" r="6" fill="#febc2e"/>
  <circle cx="62" cy="20" r="6" fill="#28c840"/>
  <text class="chrome" x="{width / 2:.0f}" y="25" fill="{DIM}" text-anchor="middle">emalia</text>

  {body}
  <rect class="cursor" x="{PAD_X:.1f}" y="{cursor_y - 13:.1f}" width="9" height="18" fill="{FG}"/>
</svg>
"""


def main() -> None:
    """Write the SVG next to the other README assets."""
    target = Path(__file__).resolve().parent.parent / "docs" / "assets" / "demo.svg"
    target.write_text(build(), encoding="utf-8")
    print(f"Wrote {target} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
