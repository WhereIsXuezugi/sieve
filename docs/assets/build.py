#!/usr/bin/env python3
"""Regenerate the README diagrams.

One source, two files per diagram: GitHub picks light or dark with a `<picture>`
element, so every diagram needs both and they must not drift. Writing them by
hand guarantees they drift, so they are generated.

    python docs/assets/build.py
"""

from __future__ import annotations

import pathlib

HERE = pathlib.Path(__file__).parent

THEMES = {
    "light": {
        "bg": "none", "ink": "#1c2128", "ink2": "#4d545e", "ink3": "#767d86",
        "rule": "#c8cac2", "panel": "#f5f5f2", "sunken": "#e2e4dd",
        "indigo": "#3f3aa8", "teal": "#0f6b60", "amber": "#916200",
        "clay": "#9c3b25", "plum": "#6c3480", "steel": "#4a7fb5",
    },
    "dark": {
        "bg": "none", "ink": "#e3e5e0", "ink2": "#a8aeb8", "ink3": "#7c838e",
        "rule": "#343a45", "panel": "#212630", "sunken": "#171b21",
        "indigo": "#8c86ff", "teal": "#4bbfae", "amber": "#d9a441",
        "clay": "#e08065", "plum": "#b78ad0", "steel": "#7fb0e0",
    },
}

SANS = "system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,'SF Mono',Menlo,Consolas,monospace"


# ---------------------------------------------------------------------------
# The pipeline: what happens between "some videos exist" and "here is a page"
# ---------------------------------------------------------------------------

PIPELINE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 330" width="900" height="330"
     role="img" aria-label="The ranking pipeline: candidates, gate, blend, explain, arrange, diagnose">
  <title>How a homepage is built</title>
  <style>
    .h {{ font-family: {SANS}; font-size: 15px; font-weight: 600; fill: {ink}; }}
    .l {{ font-family: {SANS}; font-size: 12.5px; fill: {ink2}; }}
    .m {{ font-family: {MONO}; font-size: 11px; fill: {ink3}; }}
    .n {{ font-family: {MONO}; font-size: 22px; font-weight: 500; fill: {ink}; }}
    .rule {{ stroke: {rule}; stroke-width: 1; }}
  </style>

  <!-- the funnel: width is proportional to what survives each stage -->
  <g>
    <path d="M40 40 L860 40 L860 96 L40 96 Z" fill="{sunken}"/>
    <path d="M40 40 L860 40 L744 96 L40 96 Z" fill="{indigo}" opacity=".16"/>
    <path d="M40 40 L744 40 L352 96 L40 96 Z" fill="{indigo}" opacity=".22"/>
    <path d="M40 40 L352 40 L172 96 L40 96 Z" fill="{indigo}" opacity=".5"/>
    <rect x="40" y="40" width="820" height="56" fill="none" class="rule"/>
  </g>

  <g>
    <text x="40" y="126" class="n">620</text>
    <text x="40" y="144" class="l">candidates</text>
    <text x="40" y="162" class="m">from your sources</text>

    <text x="214" y="126" class="n">214</text>
    <text x="214" y="144" class="l">survive the gate</text>
    <text x="214" y="162" class="m">406 rejected, each with a reason</text>

    <text x="536" y="126" class="n">36</text>
    <text x="536" y="144" class="l">on the page</text>
    <text x="536" y="162" class="m">after diversity and budget caps</text>
  </g>

  <line x1="40" y1="190" x2="860" y2="190" class="rule"/>

  <!-- the six stages -->
  <g>
    {stages}
  </g>
</svg>
"""

STAGES = [
    ("candidates", "pull from each enabled source,", "tagged with where it came from"),
    ("gate", "channel lists, then filters,", "then your rule tree"),
    ("blend", "weighted sum of named", "components, per video"),
    ("explain", "positive components become", "percentages summing to 100"),
    ("arrange", "diversify, cap per channel,", "enforce composition quotas"),
    ("diagnose", "topic entropy, rabbit-hole", "warning, rejection tally"),
]


def pipeline(theme: dict[str, str]) -> str:
    chunks = []
    width = 820 / len(STAGES)
    for index, (name, line1, line2) in enumerate(STAGES):
        x = 40 + index * width
        chunks.append(f'''
    <g transform="translate({x:.0f} 0)">
      <rect x="0" y="206" width="{width - 14:.0f}" height="3" fill="{theme['indigo']}"
            opacity="{0.35 + 0.13 * index:.2f}"/>
      <text x="0" y="232" class="h">{index + 1}. {name}</text>
      <text x="0" y="252" class="m">{line1}</text>
      <text x="0" y="268" class="m">{line2}</text>
    </g>''')
    body = "".join(chunks)
    return PIPELINE.format(SANS=SANS, MONO=MONO, stages=body, **theme)


# ---------------------------------------------------------------------------
# The explanation bar, annotated
# ---------------------------------------------------------------------------

WHY_SEGMENTS = [
    ("source", 41, "indigo", "from a channel you subscribe to"),
    ("interest", 28, "teal", "matches your interests"),
    ("channel", 17, "steel", "channel priority and watch time"),
    ("freshness", 9, "amber", "recently published"),
    ("novelty", 5, "plum", "outside your usual topics"),
]


def why_bar(theme: dict[str, str]) -> str:
    bar_x, bar_w, bar_y = 40, 620, 96
    cursor = bar_x
    segments, legend = [], []
    for index, (_key, percent, colour, label) in enumerate(WHY_SEGMENTS):
        width = bar_w * percent / 100
        radius = ""
        if index == 0:
            radius = f'<path d="M{cursor + 3} {bar_y} h{width - 3} v14 h-{width - 3} a3 3 0 0 1-3-3 v-8 a3 3 0 0 1 3-3 z" fill="{theme[colour]}"/>'
        elif index == len(WHY_SEGMENTS) - 1:
            radius = f'<path d="M{cursor} {bar_y} h{width - 3} a3 3 0 0 1 3 3 v8 a3 3 0 0 1-3 3 h-{width - 3} z" fill="{theme[colour]}"/>'
        else:
            radius = f'<rect x="{cursor}" y="{bar_y}" width="{width}" height="14" fill="{theme[colour]}"/>'
        segments.append(radius)
        row_y = 148 + index * 26
        legend.append(f'''
    <rect x="40" y="{row_y - 9}" width="10" height="10" rx="2" fill="{theme[colour]}"/>
    <text x="60" y="{row_y}" class="pct">{percent}%</text>
    <text x="104" y="{row_y}" class="l">{label}</text>''')
        cursor += width

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 700 300" width="700" height="300"
     role="img" aria-label="An explanation bar: 41 percent source, 28 percent interest, 17 percent channel, 9 percent freshness, 5 percent novelty">
  <title>The explanation bar</title>
  <style>
    .t {{ font-family: {SANS}; font-size: 15px; font-weight: 500; fill: {theme['ink']}; }}
    .l {{ font-family: {SANS}; font-size: 12.5px; fill: {theme['ink2']}; }}
    .pct {{ font-family: {MONO}; font-size: 12.5px; fill: {theme['ink3']}; }}
    .m {{ font-family: {MONO}; font-size: 11px; fill: {theme['ink3']}; }}
    .note {{ font-family: {SANS}; font-size: 12px; font-style: italic; fill: {theme['ink3']}; }}
  </style>

  <rect x="20" y="20" width="660" height="260" rx="4" fill="{theme['panel']}"
        stroke="{theme['rule']}" stroke-width="1"/>

  <text x="40" y="56" class="t">Writing a page allocator from scratch</text>
  <text x="40" y="78" class="m">Kernel Internals&#160;&#160;·&#160;&#160;84K views&#160;&#160;·&#160;&#160;38:12</text>

  {"".join(segments)}

  {"".join(legend)}

  <text x="40" y="{148 + len(WHY_SEGMENTS) * 26 + 16}" class="note">Every video carries one. The segments are the ranking arithmetic, not a summary of it.</text>
</svg>
'''


def main() -> None:
    written = []
    for name, builder in (("pipeline", pipeline), ("why-bar", why_bar)):
        for theme_name, theme in THEMES.items():
            path = HERE / f"{name}-{theme_name}.svg"
            path.write_text(builder(theme))
            written.append(path.name)
    print("wrote " + ", ".join(written))


if __name__ == "__main__":
    main()
