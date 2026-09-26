"""合格率推移グラフを inline SVG で描く (JS 不要・表示が速い)。"""

from __future__ import annotations

from html import escape


def pass_rate_svg(rows: list[dict], width: int = 480, height: int = 220) -> str:
    """rows: [{'label': '2024年', 'rate': 42.3}, ...] を古い順で受け取り棒グラフにする。"""
    rows = [r for r in rows if isinstance(r.get('rate'), (int, float))]
    if len(rows) < 2:
        return ''
    pad_l, pad_r, pad_t, pad_b = 40, 12, 16, 44
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    top = max(r['rate'] for r in rows)
    # 目盛りがきりのいい数字になる刻みを選ぶ (5本以内)
    step = 5 if top <= 22 else 10 if top <= 45 else 20
    y_max = min(100, (int(top // step) + 1) * step)
    ticks = int(y_max // step)
    n = len(rows)
    slot = plot_w / n
    bar_w = min(48, slot * 0.6)

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="合格率の推移">'
    ]
    # 横グリッドと目盛り
    for i in range(0, ticks + 1):
        v = step * i
        y = pad_t + plot_h - plot_h * (v / y_max)
        parts.append(f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end">{v:.0f}%</text>')
    for i, r in enumerate(rows):
        h = plot_h * (r['rate'] / y_max)
        x = pad_l + slot * i + (slot - bar_w) / 2
        y = pad_t + plot_h - h
        is_last = i == n - 1
        cls = 'bar bar-last' if is_last else 'bar'
        parts.append(
            f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="3">'
            f'<title>{escape(str(r["label"]))}: {r["rate"]}%</title></rect>'
        )
        parts.append(
            f'<text class="val" x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" text-anchor="middle">{r["rate"]}</text>'
        )
        parts.append(
            f'<text class="tick" x="{x + bar_w / 2:.1f}" y="{height - pad_b + 16:.1f}" '
            f'text-anchor="middle">{escape(str(r["label"]))}</text>'
        )
    parts.append('</svg>')
    return ''.join(parts)
