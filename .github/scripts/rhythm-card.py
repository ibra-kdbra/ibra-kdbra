#!/usr/bin/env python3
"""Draw the contribution rhythm card for the profile README.

Reads every year of the user's contribution calendar from the GitHub GraphQL
API and writes a light and a dark SVG: all-time totals and streaks, then the
last 12 full months by month and by weekday.

    GITHUB_TOKEN=... USERNAME=octocat python3 .github/scripts/rhythm-card.py
    python3 .github/scripts/rhythm-card.py --days days.json --today 2026-10-06
"""

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

ONE_DAY = dt.timedelta(days=1)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif'

# Primer tokens: fgColor default/muted, bgColor default/muted, borderColor
# default, a hairline grid one step off the surface, and the bar color.
THEMES = {
    "light": dict(fg="#1f2328", muted="#59636e", bg="#ffffff", header="#f6f8fa",
                  border="#d1d9e0", grid="#eaeef2", bar="#8250df"),
    "dark": dict(fg="#f0f6fc", muted="#9198a1", bg="#0d1117", header="#151b23",
                 border="#3d444d", grid="#21262d", bar="#a371f7"),
}

PULSE_ICON = (
    "M6 2c.306 0 .582.187.696.471L10 10.731l1.304-3.26A.751.751 0 0 1 12 7h3.25a.75.75 0 0 1 0 1.5"
    "h-2.742l-1.812 4.528a.751.751 0 0 1-1.392 0L6 4.77 4.696 8.03A.75.75 0 0 1 4 8.5H.75a.75.75 0 0 1 0-1.5"
    "h2.742l1.812-4.529A.751.751 0 0 1 6 2Z"
)


def graphql(query, login, token, attempts=3):
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": {"login": login}}).encode(),
        headers={"Authorization": f"bearer {token}", "User-Agent": login},
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.load(response)
            break
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(10 * (attempt + 1))
    user = (body.get("data") or {}).get("user")
    if body.get("errors") or not user:
        raise SystemExit(f"GitHub GraphQL query failed: {body.get('errors') or body}")
    return user


def fetch_days(login, token):
    """Return {date: contribution count} for every day the user has a calendar."""
    years = graphql(
        "query($login: String!) { user(login: $login) { contributionsCollection { contributionYears } } }",
        login, token,
    )["contributionsCollection"]["contributionYears"]
    if not years:
        return {}

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    fields = []
    for year in years:
        end = min(now, dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc))
        fields.append(
            f'y{year}: contributionsCollection(from: "{year}-01-01T00:00:00Z", '
            f'to: "{end:%Y-%m-%dT%H:%M:%SZ}") '
            "{ contributionCalendar { weeks { contributionDays { date contributionCount } } } }"
        )
    user = graphql(f"query($login: String!) {{ user(login: $login) {{ {' '.join(fields)} }} }}", login, token)

    days = {}
    for year in years:
        for week in user[f"y{year}"]["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                date = dt.date.fromisoformat(day["date"])
                if date.year == year:
                    days[date] = day["contributionCount"]
    return days


def summarize(days, today):
    current_end = today if days.get(today, 0) else today - ONE_DAY
    start = current_end
    while days.get(start, 0):
        start -= ONE_DAY
    current = ((current_end - start).days, start + ONE_DAY)

    longest, run = (0, None, None), 0
    date = min(days, default=today)
    while date <= today:
        run = run + 1 if days.get(date, 0) else 0
        if run and run >= longest[0]:
            longest = (run, date - (run - 1) * ONE_DAY, date)
        date += ONE_DAY

    best_day = max(((count, date) for date, count in days.items() if count), default=None)

    month_end = today.replace(day=1) - ONE_DAY
    months = []
    year, month = month_end.year, month_end.month
    for _ in range(12):
        months.insert(0, (year, month))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    month_start = dt.date(months[0][0], months[0][1], 1)

    by_month = dict.fromkeys(months, 0)
    weekday_sum, weekday_days = [0] * 7, [0] * 7
    date = month_start
    while date <= month_end:
        count = days.get(date, 0)
        by_month[(date.year, date.month)] += count
        weekday_sum[date.weekday()] += count
        weekday_days[date.weekday()] += 1
        date += ONE_DAY

    return dict(
        total=sum(days.values()),
        first=min((date for date, count in days.items() if count), default=None),
        current=current,
        longest=longest,
        best_day=best_day,
        period=(month_start, month_end),
        months=[(dt.date(y, m, 1).strftime("%b"), by_month[(y, m)]) for y, m in months],
        weekdays=[(name, s / n if n else 0) for name, s, n in zip(WEEKDAYS, weekday_sum, weekday_days)],
    )


def fmt_date(date, year=True):
    return f"{date:%b} {date.day}, {date.year}" if year else f"{date:%b} {date.day}"


def fmt_range(start, end):
    if start == end:
        return fmt_date(end)
    return f"{fmt_date(start, start.year != end.year)} – {fmt_date(end)}"


def fmt_value(value):
    if value >= 10 or value == int(value):
        return f"{round(value):,}"
    return f"{value:.1f}"


def plural(count, word):
    return word if count == 1 else word + "s"


def nice_ticks(peak):
    """Gridline values at a round step, at least two of them up to the peak."""
    if peak <= 0:
        return [0]
    step = 1
    magnitude = 10 ** math.floor(math.log10(peak))
    for base in (magnitude * f for f in (5, 2.5, 2, 1, 0.5, 0.25, 0.2, 0.1)):
        if peak / base >= 2:
            step = base
            break
    return [i * step for i in range(int(peak // step) + 1)]


def column_path(x, width, top, baseline):
    """A column with a 4px rounded data end and a square baseline."""
    radius = min(4, width / 2, baseline - top)
    return (
        f"M{x:.1f} {baseline:.1f}V{top + radius:.1f}"
        f"A{radius:.1f} {radius:.1f} 0 0 1 {x + radius:.1f} {top:.1f}"
        f"H{x + width - radius:.1f}"
        f"A{radius:.1f} {radius:.1f} 0 0 1 {x + width:.1f} {top + radius:.1f}"
        f"V{baseline:.1f}Z"
    )


def column_chart(series, left, right, plot_top, baseline, gutter, bar_width, delay=0):
    """Columns with hairline gridlines, round tick labels and the peak labelled."""
    peak = max((value for _, value in series), default=0)
    parts = []
    for tick in nice_ticks(peak):
        y = baseline - (tick / peak) * (baseline - plot_top) if peak else baseline
        css = "axis" if tick == 0 else "grid"
        parts.append(f'<path class="{css}" d="M{left + gutter:.1f} {y + 0.5:.1f}H{right:.1f}"/>')
        parts.append(f'<text class="tick" x="{left + gutter - 8:.1f}" y="{y + 4:.1f}" text-anchor="end">{fmt_value(tick)}</text>')

    slot = (right - left - gutter) / len(series)
    peak_index = max(range(len(series)), key=lambda i: (series[i][1], i)) if peak else None
    for i, (name, value) in enumerate(series):
        center = left + gutter + slot * (i + 0.5)
        parts.append(f'<text class="tick" x="{center:.1f}" y="{baseline + 18:.1f}" text-anchor="middle">{escape(name)}</text>')
        if not value:
            continue
        top = baseline - (value / peak) * (baseline - plot_top)
        parts.append(
            f'<path class="bar" style="animation-delay:{delay + i * 45}ms" '
            f'd="{column_path(center - bar_width / 2, bar_width, top, baseline)}"/>'
        )
        if i == peak_index:
            parts.append(f'<text class="peak" x="{center:.1f}" y="{top - 7:.1f}" text-anchor="middle">{fmt_value(value)}</text>')
    return parts


def render(stats, theme, today):
    c = THEMES[theme]
    width, height = 840, 352
    columns = [24 + i * (width - 48) / 4 for i in range(4)]  # one grid for the tiles and the charts
    current_days, current_start = stats["current"]
    longest_days, longest_start, longest_end = stats["longest"]
    period_start, period_end = stats["period"]

    tiles = [
        ("Total contributions", f"{stats['total']:,}", "",
         f"Since {stats['first']:%b %Y}" if stats["first"] else "No contributions yet"),
        ("Current streak", f"{current_days:,}", plural(current_days, "day"),
         f"Since {fmt_date(current_start)}" if current_days else "No active streak"),
        ("Longest streak", f"{longest_days:,}", plural(longest_days, "day"),
         fmt_range(longest_start, longest_end) if longest_days else "No streak yet"),
        ("Best day", f"{stats['best_day'][0]:,}" if stats["best_day"] else "0",
         plural(stats["best_day"][0], "contribution") if stats["best_day"] else "contributions",
         fmt_date(stats["best_day"][1]) if stats["best_day"] else "No contributions yet"),
    ]
    period = f"{period_start:%b %Y} – {period_end:%b %Y}"

    summary = (
        f"{stats['total']:,} contributions in total. "
        f"Current streak {current_days} {plural(current_days, 'day')}; "
        f"longest streak {longest_days} {plural(longest_days, 'day')}"
        + (f" ({fmt_range(longest_start, longest_end)})" if longest_days else "")
        + (f"; best day {stats['best_day'][0]:,} on {fmt_date(stats['best_day'][1])}. " if stats["best_day"] else ". ")
        + f"Contributions by month, {period}: "
        + ", ".join(f"{name} {value:,}" for name, value in stats["months"])
        + ". Daily average by weekday: "
        + ", ".join(f"{name} {fmt_value(value)}" for name, value in stats["weekdays"])
        + "."
    )

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        'role="img" aria-labelledby="title desc">',
        '<title id="title">Contribution rhythm</title>',
        f'<desc id="desc">{escape(summary)}</desc>',
        "<style>",
        f"text {{ font-family: {FONT}; }}",
        f".heading {{ font-size: 14px; font-weight: 600; fill: {c['fg']}; }}",
        f".label, .note {{ font-size: 12px; fill: {c['muted']}; }}",
        f".value {{ font-size: 26px; font-weight: 600; fill: {c['fg']}; }}",
        f".unit {{ font-size: 13px; font-weight: 400; fill: {c['muted']}; }}",
        f".title {{ font-size: 12px; font-weight: 600; fill: {c['fg']}; }}",
        f".tick {{ font-size: 11px; fill: {c['muted']}; font-variant-numeric: tabular-nums; }}",
        f".peak {{ font-size: 11px; font-weight: 600; fill: {c['fg']}; animation: fade .5s ease .9s both; }}",
        f".grid {{ stroke: {c['grid']}; stroke-width: 1; }}",
        f".axis {{ stroke: {c['border']}; stroke-width: 1; }}",
        f".bar {{ fill: {c['bar']}; transform-box: fill-box; transform-origin: 50% 100%; "
        "animation: rise .9s cubic-bezier(.22, 1, .36, 1) both; }",
        "@keyframes rise { from { transform: scaleY(0); } }",
        "@keyframes fade { from { opacity: 0; } }",
        "@media (prefers-reduced-motion: reduce) { .bar, .peak { animation: none; } }",
        "</style>",
        f'<clipPath id="card"><rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="6"/></clipPath>',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="6" fill="{c["bg"]}"/>',
        f'<rect width="{width}" height="44" fill="{c["header"]}" clip-path="url(#card)"/>',
        f'<path d="M0 44.5H{width}" stroke="{c["border"]}" clip-path="url(#card)"/>',
        f'<path transform="translate(16 14)" fill="{c["muted"]}" d="{PULSE_ICON}"/>',
        '<text class="heading" x="40" y="27">Contribution rhythm</text>',
        f'<text class="label" x="{width - 16}" y="27" text-anchor="end">Updated {fmt_date(today)}</text>',
    ]

    for i, (label, value, unit, note) in enumerate(tiles):
        x = columns[i]
        if i:
            svg.append(f'<path class="grid" d="M{x - 16.5:.1f} 72V140"/>')
        unit_span = f'<tspan class="unit" dx="5">{escape(unit)}</tspan>' if unit else ""
        svg += [
            f'<text class="label" x="{x:.1f}" y="84">{escape(label)}</text>',
            f'<text class="value" x="{x:.1f}" y="116">{escape(value)}{unit_span}</text>',
            f'<text class="note" x="{x:.1f}" y="136">{escape(note)}</text>',
        ]

    weekdays = [(name[:2], value) for name, value in stats["weekdays"]]
    svg += [
        f'<path class="grid" d="M24 164.5H{width - 24}"/>',
        f'<text class="title" x="24" y="192">By month<tspan class="note" font-weight="400"> · {escape(period)}</tspan></text>',
        f'<text class="title" x="{columns[3]:.1f}" y="192">By weekday<tspan class="note" font-weight="400"> · daily average</tspan></text>',
    ]
    svg += column_chart(stats["months"], 24, columns[3] - 32, 228, 310, 40, 18)
    svg += column_chart(weekdays, columns[3], width - 24, 228, 310, 24, 14, delay=200)
    svg.append(f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="6" fill="none" stroke="{c["border"]}"/>')
    svg.append("</svg>")
    return "\n".join(svg) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", default=os.environ.get("USERNAME"))
    parser.add_argument("--out", default="profile-rhythm")
    parser.add_argument("--days", help="JSON list of {date, count} to draw instead of fetching")
    parser.add_argument("--today", type=dt.date.fromisoformat)
    args = parser.parse_args()

    if args.days:
        days = {dt.date.fromisoformat(d["date"]): d["count"] for d in json.loads(pathlib.Path(args.days).read_text())}
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not (args.user and token):
            raise SystemExit("Set USERNAME and GITHUB_TOKEN, or pass --days.")
        days = fetch_days(args.user, token)

    today = args.today or max(dt.datetime.now(dt.timezone.utc).date(), max(days, default=dt.date.min))
    stats = summarize(days, today)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        (out / f"rhythm-{theme}.svg").write_text(render(stats, theme, today), encoding="utf-8")
    print(f"Wrote {out}/rhythm-light.svg and {out}/rhythm-dark.svg "
          f"({stats['total']:,} contributions, current streak {stats['current'][0]} days).")


if __name__ == "__main__":
    main()
