"""
Brief rendering (Stage 4)
=========================

Takes the brief dict from synthesis.synthesize_brief() plus the compact
records, and writes ONE self-contained HTML file — inline CSS, inline SVG
charts, no build step, no network dependency beyond the webfont link (which
degrades to system fonts offline).

This is the artefact you screen-share and screenshot. The Google Sheet stays
as the raw layer; this is the deliverable on top of it.

Fully deterministic — no AI, no network calls. Safe to re-run as many times
as you like while tuning the design, which is exactly the point: iterate on
the report for free against a brief you already generated.

    from render import render_brief
    html = render_brief(brief, records, category="Ragi Atta")
    Path("brief.html").write_text(html, encoding="utf-8")
"""

from datetime import date
from html import escape

BRAND_START = "#0000A5"
BRAND_END = "#3732FE"


# ================================================================
# SMALL HELPERS
# ================================================================

def _e(x):
    """Escape for HTML, turning None into an empty string."""
    return escape(str(x)) if x is not None else ""


def _li(items, cls=""):
    items = [i for i in (items or []) if i]
    if not items:
        return ""
    lis = "".join(f"<li>{_e(i)}</li>" for i in items)
    return f'<ul class="{cls}">{lis}</ul>'


def _section(title, body, kicker=""):
    if not body:
        return ""
    k = f'<div class="kicker">{_e(kicker)}</div>' if kicker else ""
    return f'<section class="block">{k}<h2>{_e(title)}</h2>{body}</section>'


def _rupees(n):
    """Indian-style short form: 4,44,000 -> Rs 4.4L"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n >= 1e7:
        return f"Rs {n/1e7:.1f}Cr"
    if n >= 1e5:
        return f"Rs {n/1e5:.1f}L"
    if n >= 1e3:
        return f"Rs {n/1e3:.0f}k"
    return f"Rs {n:.0f}"


# ================================================================
# CHARTS  (hand-built SVG — no chart library, no CDN)
# ================================================================

def _scatter_price_rating(records, w=760, h=400):
    """Price per kg (x) vs rating (y). Bubble size = review count."""
    pts = [r for r in records
           if r.get("price_per_kg") is not None and r.get("rating") is not None]
    if len(pts) < 3:
        return ""

    pad_l, pad_r, pad_t, pad_b = 62, 24, 24, 52
    xs = [p["price_per_kg"] for p in pts]
    ys = [p["rating"] for p in pts]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    # Pad the axes so nothing sits on the frame.
    x_pad = (x_max - x_min) * 0.08 or 10
    y_pad = (y_max - y_min) * 0.15 or 0.2
    x_min, x_max = x_min - x_pad, x_max + x_pad
    y_min, y_max = y_min - y_pad, y_max + y_pad

    def px(v):
        return pad_l + (v - x_min) / (x_max - x_min) * (w - pad_l - pad_r)

    def py(v):
        return h - pad_b - (v - y_min) / (y_max - y_min) * (h - pad_t - pad_b)

    max_rev = max((p.get("review_count") or 0) for p in pts) or 1

    parts = [
        f'<svg viewBox="0 0 {w} {h}" class="chart" xmlns="http://www.w3.org/2000/svg">',
        f'<line x1="{pad_l}" y1="{h-pad_b}" x2="{w-pad_r}" y2="{h-pad_b}" class="axis"/>',
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{h-pad_b}" class="axis"/>',
    ]

    # Y gridlines (rating)
    steps = 4
    for i in range(steps + 1):
        v = y_min + (y_max - y_min) * i / steps
        y = py(v)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l-10}" y="{y+4:.1f}" class="tick" text-anchor="end">{v:.1f}</text>')

    # X ticks (price/kg)
    for i in range(5):
        v = x_min + (x_max - x_min) * i / 4
        x = px(v)
        parts.append(f'<text x="{x:.1f}" y="{h-pad_b+22}" class="tick" text-anchor="middle">{v:.0f}</text>')

    for p in pts:
        x, y = px(p["price_per_kg"]), py(p["rating"])
        r = 5 + 11 * ((p.get("review_count") or 0) / max_rev) ** 0.5
        label = (p.get("brand") or "")[:18]
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" class="dot"/>')
        parts.append(f'<text x="{x:.1f}" y="{y-r-5:.1f}" class="dotlabel" text-anchor="middle">{_e(label)}</text>')

    parts.append(f'<text x="{(w+pad_l)/2:.0f}" y="{h-8}" class="axlabel" text-anchor="middle">Price per kg (Rs)</text>')
    parts.append(f'<text x="16" y="{(h-pad_b+pad_t)/2:.0f}" class="axlabel" text-anchor="middle" '
                 f'transform="rotate(-90 16 {(h-pad_b+pad_t)/2:.0f})">Customer rating</text>')
    parts.append("</svg>")
    return "".join(parts)


def _revenue_bars(records, top_n=10, w=760):
    """Horizontal bars of estimated monthly revenue."""
    rows = [r for r in records if r.get("monthly_revenue_estimate")]
    if len(rows) < 2:
        return ""
    rows.sort(key=lambda r: r["monthly_revenue_estimate"], reverse=True)
    rows = rows[:top_n]
    top = rows[0]["monthly_revenue_estimate"]

    bar_h, gap, pad_l, pad_t = 26, 12, 190, 12
    h = pad_t * 2 + len(rows) * (bar_h + gap)
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" xmlns="http://www.w3.org/2000/svg">',
             '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="0">'
             f'<stop offset="0%" stop-color="{BRAND_START}"/>'
             f'<stop offset="100%" stop-color="{BRAND_END}"/></linearGradient></defs>']

    for i, r in enumerate(rows):
        y = pad_t + i * (bar_h + gap)
        bw = max(3, (r["monthly_revenue_estimate"] / top) * (w - pad_l - 110))
        parts.append(f'<text x="{pad_l-12}" y="{y+bar_h*0.68:.0f}" class="barlabel" '
                     f'text-anchor="end">{_e((r.get("brand") or "")[:26])}</text>')
        parts.append(f'<rect x="{pad_l}" y="{y}" width="{bw:.1f}" height="{bar_h}" rx="4" fill="url(#bg)"/>')
        parts.append(f'<text x="{pad_l+bw+10:.1f}" y="{y+bar_h*0.68:.0f}" class="barvalue">'
                     f'{_e(_rupees(r["monthly_revenue_estimate"]))}</text>')

    parts.append("</svg>")
    return "".join(parts)


# ================================================================
# SECTIONS
# ================================================================

def _exec_summary(b):
    ex = b.get("executive_summary") or {}
    if not ex:
        return ""
    out = []
    if ex.get("headline"):
        out.append(f'<p class="headline">{_e(ex["headline"])}</p>')
    if ex.get("category_shape"):
        out.append(f'<p class="lede">{_e(ex["category_shape"])}</p>')
    if ex.get("key_findings"):
        out.append('<h3>What the data says</h3>' + _li(ex["key_findings"], "findings"))
    if ex.get("recommended_actions"):
        acts = "".join(
            f'<li><span class="num">{i}</span><span>{_e(a)}</span></li>'
            for i, a in enumerate(ex["recommended_actions"], 1))
        out.append(f'<h3>Recommended actions</h3><ol class="actions">{acts}</ol>')
    return "".join(out)


def _price_map(b, records):
    pm = b.get("price_quality_map") or {}
    out = [_scatter_price_rating(records)]
    for c in pm.get("clusters") or []:
        meta = " · ".join(x for x in [c.get("price_per_kg_range"), c.get("rating_range")] if x)
        members = ", ".join(c.get("members") or [])
        out.append(
            f'<div class="card"><div class="card-head"><strong>{_e(c.get("name"))}</strong>'
            f'<span class="meta">{_e(meta)}</span></div>'
            f'<div class="members">{_e(members)}</div>'
            f'<p>{_e(c.get("read"))}</p></div>')
    if pm.get("anomalies"):
        out.append('<h3>Anomalies</h3>' + _li(pm["anomalies"], "flag"))
    return "".join(out) if any(out) else ""


def _revenue(b, records):
    rc = b.get("revenue_concentration") or {}
    out = []
    if rc.get("narrative"):
        out.append(f'<p class="lede">{_e(rc["narrative"])}</p>')
    out.append(_revenue_bars(records))
    for p in rc.get("top_players") or []:
        out.append(
            f'<div class="card"><div class="card-head"><strong>{_e(p.get("brand"))}</strong>'
            f'<span class="meta">{_e(_rupees(p.get("monthly_revenue_estimate")))} / month (est.)</span></div>'
            f'<p>{_e(p.get("read"))}</p></div>')
    if rc.get("long_tail_note"):
        out.append(f'<p class="note">{_e(rc["long_tail_note"])}</p>')
    return "".join(out)


def _voc(b):
    v = b.get("voice_of_customer") or {}
    out = []
    if v.get("category_complaints"):
        out.append("<h3>Recurring complaints</h3>")
        for c in v["category_complaints"]:
            out.append(
                f'<div class="card neg"><div class="card-head"><strong>{_e(c.get("theme"))}</strong></div>'
                f'<p class="evidence">{_e(c.get("evidence"))}</p>'
                f'<p>{_e(c.get("read"))}</p></div>')
    if v.get("category_praise"):
        out.append("<h3>What buyers reward</h3>")
        for p in v["category_praise"]:
            out.append(
                f'<div class="card pos"><div class="card-head"><strong>{_e(p.get("theme"))}</strong></div>'
                f'<p>{_e(p.get("read"))}</p></div>')
    if v.get("unmet_needs"):
        out.append("<h3>Unmet needs</h3>" + _li(v["unmet_needs"]))
    if v.get("coverage_note"):
        out.append(f'<p class="note">{_e(v["coverage_note"])}</p>')
    return "".join(out)


def _whitespace(b):
    w = b.get("positioning_whitespace") or {}
    out = []
    terr = w.get("occupied_territories") or []
    if terr:
        rows = "".join(
            f'<tr><td><strong>{_e(t.get("aesthetic"))}</strong></td>'
            f'<td>{_e(", ".join(t.get("brands") or []))}</td>'
            f'<td class="meta">{_e(t.get("price_tier"))}</td></tr>' for t in terr)
        out.append('<table class="grid-table"><thead><tr><th>Aesthetic</th>'
                   '<th>Brands</th><th>Price tier</th></tr></thead>'
                   f'<tbody>{rows}</tbody></table>')
    if w.get("gaps"):
        out.append("<h3>Open territory</h3>" + _li(w["gaps"], "flag"))
    if w.get("read"):
        out.append(f'<p class="lede">{_e(w["read"])}</p>')
    return "".join(out)


# ================================================================
# MAIN
# ================================================================

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
 color:#12121a;background:#f4f4f7;line-height:1.55;padding:32px 16px}
.page{max-width:940px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;
 box-shadow:0 2px 40px rgba(0,0,60,.10)}
.cover{background:linear-gradient(180deg,__START__,__END__);color:#fff;padding:48px 56px 40px}
.cover .eyebrow{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11px;
 letter-spacing:.18em;text-transform:uppercase;opacity:.75;margin-bottom:14px}
.cover h1{font-family:'Bricolage Grotesque','Hanken Grotesk',sans-serif;font-size:40px;
 line-height:1.1;font-weight:700;letter-spacing:-.02em}
.cover .sub{margin-top:18px;display:flex;gap:28px;flex-wrap:wrap;
 font-family:'JetBrains Mono',ui-monospace,monospace;font-size:12px;opacity:.85}
.body{padding:8px 56px 48px}
.block{padding:36px 0;border-bottom:1px solid #e8e8ef}
.block:last-child{border-bottom:none}
.kicker{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:10px;letter-spacing:.18em;
 text-transform:uppercase;color:#8a8aa3;margin-bottom:8px}
h2{font-family:'Bricolage Grotesque','Hanken Grotesk',sans-serif;font-size:24px;
 letter-spacing:-.01em;margin-bottom:18px}
h3{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:#5a5a73;
 margin:26px 0 12px;font-weight:600}
p{margin-bottom:12px}
.headline{font-family:'Bricolage Grotesque','Hanken Grotesk',sans-serif;font-size:26px;
 line-height:1.3;font-weight:600;letter-spacing:-.01em;margin-bottom:16px}
.lede{font-size:16px;color:#3a3a4d}
.note{font-size:13px;color:#7a7a91;font-style:italic;margin-top:14px}
ul,ol{margin:0 0 4px 0;padding-left:0;list-style:none}
ul li{position:relative;padding-left:20px;margin-bottom:10px}
ul li:before{content:"";position:absolute;left:4px;top:9px;width:6px;height:6px;
 border-radius:50%;background:__END__}
ul.flag li:before{background:#d4700a;border-radius:1px}
ol.actions li{display:flex;gap:14px;margin-bottom:14px;align-items:flex-start}
ol.actions .num{flex:0 0 26px;height:26px;border-radius:50%;
 background:linear-gradient(135deg,__START__,__END__);color:#fff;display:flex;
 align-items:center;justify-content:center;font-size:12px;font-weight:700;margin-top:1px}
.card{border:1px solid #e4e4ee;border-left:3px solid __END__;border-radius:8px;
 padding:16px 18px;margin-bottom:12px;background:#fbfbfd}
.card.neg{border-left-color:#d4700a}
.card.pos{border-left-color:#1f9268}
.card-head{display:flex;justify-content:space-between;align-items:baseline;
 gap:16px;margin-bottom:6px;flex-wrap:wrap}
.card p:last-child{margin-bottom:0}
.meta{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11px;color:#8a8aa3}
.members{font-size:13px;color:#5a5a73;margin-bottom:8px}
.evidence{font-size:13px;color:#5a5a73;padding-left:12px;border-left:2px solid #e4e4ee}
.chart{width:100%;height:auto;margin:8px 0 22px;display:block}
.axis{stroke:#c9c9d8;stroke-width:1}
.grid{stroke:#eeeef4;stroke-width:1}
.tick,.barvalue{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:10px;fill:#8a8aa3}
.axlabel{font-size:11px;fill:#5a5a73}
.dot{fill:__END__;fill-opacity:.55;stroke:__START__;stroke-width:1.2}
.dotlabel{font-size:9px;fill:#5a5a73}
.barlabel{font-size:12px;fill:#12121a}
.barvalue{font-size:11px;fill:#5a5a73}
.grid-table{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:8px}
.grid-table th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
 color:#8a8aa3;padding:8px 10px;border-bottom:1px solid #e4e4ee;font-weight:600}
.grid-table td{padding:10px;border-bottom:1px solid #f0f0f5;vertical-align:top}
.caveats{background:#f8f8fb;border-radius:10px;padding:20px 24px;margin-top:8px}
.caveats li{font-size:13px;color:#5a5a73}
.foot{padding:24px 56px 34px;background:#f8f8fb;border-top:1px solid #e8e8ef;
 font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11px;color:#8a8aa3;
 display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
@media print{body{background:#fff;padding:0}.page{box-shadow:none;border-radius:0}}
"""


def render_brief(brief, records, category, client_brand=None,
                 run_date=None, source_note=""):
    """Return the complete HTML document as a string."""
    brief = brief or {}
    run_date = run_date or date.today().isoformat()

    covered = sum(1 for r in records if r.get("review_summary"))
    sub = [f"{len(records)} products analysed",
           f"{covered} with review data",
           run_date]
    if client_brand:
        sub.insert(0, f"Prepared for {client_brand}")

    css = _CSS.replace("__START__", BRAND_START).replace("__END__", BRAND_END)

    blocks = "".join([
        _section("Executive summary", _exec_summary(brief), "01 — The short version"),
        _section("Price and quality landscape", _price_map(brief, records), "02 — Where everyone sits"),
        _section("Revenue concentration", _revenue(brief, records), "03 — Where the money is"),
        _section("Voice of the customer", _voc(brief), "04 — What buyers actually say"),
        _section("Positioning white-space", _whitespace(brief), "05 — Where the gaps are"),
    ])

    caveats = brief.get("data_caveats") or []
    caveats_html = (f'<section class="block"><h2>Method and caveats</h2>'
                    f'<div class="caveats">{_li(caveats)}</div></section>') if caveats else ""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(category)} — Category Brief</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=Hanken+Grotesk:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{css}</style></head>
<body><div class="page">
<header class="cover">
  <div class="eyebrow">Amazon India · Category Intelligence</div>
  <h1>{_e(category)}</h1>
  <div class="sub">{"".join(f"<span>{_e(s)}</span>" for s in sub)}</div>
</header>
<div class="body">{blocks}{caveats_html}</div>
<footer class="foot">
  <span>Generated by OffLoad · theoffload.in</span>
  <span>{_e(source_note or "Amazon India public listing data · figures are estimates")}</span>
</footer>
</div></body></html>"""
