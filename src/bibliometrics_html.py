"""Render a standalone report with inline SVG charts and networks."""

from collections import defaultdict
from html import escape
import math
import os
from pathlib import Path
from string import Template
import tempfile
from textwrap import wrap


REPORT_NAME = 'report.html'
TEMPLATE = Path(__file__).with_name('bibliometrics_report.html')


def ranking(counts, labels, limit):
    keys = sorted(counts, key=lambda key: (-counts[key], key))
    return [(labels[key], counts[key]) for key in keys[:limit]]


def bar_chart(rows, title):
    if not rows:
        return '<p class="empty">No data available.</p>'
    width, label_width, bar_width = 520, 250, 225
    max_value = max(value for _, value in rows)
    shapes = []
    y = 8
    for label, value in rows:
        lines = wrap(label, width=32)
        height = max(32, len(lines) * 16 + 12)
        middle = y + height / 2
        text_top = middle - (len(lines) - 1) * 8
        text = ''.join(f'<tspan x="0" y="{text_top + i * 16:g}">{escape(line)}</tspan>'
                       for i, line in enumerate(lines))
        length = value / max_value * bar_width
        shapes.append(
            f'<g><title>{escape(label)}: {value}</title>'
            f'<text class="bar-label">{text}</text>'
            f'<rect class="bar-track" x="{label_width}" y="{middle - 6:g}" width="{bar_width}" height="12" rx="3"/>'
            f'<rect class="bar-fill" x="{label_width}" y="{middle - 6:g}" width="{length:.2f}" height="12" rx="3"/>'
            f'<text class="bar-count" x="{width - 4}" y="{middle:g}" text-anchor="end">{value}</text></g>'
        )
        y += height
    return (f'<svg class="bars" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {y}" '
            f'role="img" aria-label="{escape(title)}">{"".join(shapes)}</svg>')


def component_layout(nodes, edges):
    """Pack connected groups on shelves, with isolated nodes in a compact grid."""
    neighbors = defaultdict(set)
    for left, right, _ in edges:
        neighbors[left].add(right)
        neighbors[right].add(left)
    seen, groups = set(), []
    for key in nodes:
        if key in seen:
            continue
        seen.add(key)
        group = [key]
        for member in group:
            adjacent = sorted(neighbors[member] - seen)
            group.extend(adjacent)
            seen.update(adjacent)
        groups.append(group)

    positions = {}
    width, padding = 960, 16
    x, y, shelf = padding, padding, 0
    for group in sorted(groups, key=len, reverse=True):
        size = min(width - 2 * padding, max(28, round(26 * math.sqrt(len(group)))))
        if x + size > width - padding:
            x, y, shelf = padding, y + shelf + padding, 0
        cx, cy = x + size / 2, y + size / 2
        if len(group) == 1:
            positions[group[0]] = cx, cy
        else:
            # Divide larger groups into rings so all nodes remain visible.
            offset, radius = 0, size * .44
            while offset < len(group):
                capacity = max(1, int(2 * math.pi * radius / 12))
                ring = group[offset:offset + capacity]
                for index, key in enumerate(ring):
                    angle = 2 * math.pi * index / len(ring) - math.pi / 2
                    positions[key] = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
                offset += len(ring)
                radius = max(1, radius - 14)
        shelf = max(shelf, size)
        x += size + padding
    return positions, width, max(120, y + shelf + padding)


def network_svg(nodes, edges, *, directed):
    if not nodes:
        return '<p class="empty">No nodes available.</p>'
    positions, width, height = component_layout(nodes, edges)
    title = 'Paper citation network' if directed else 'Coauthor network'
    svg = [
        f'<svg class="network" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{title}">'
    ]
    if directed:
        svg.append('<defs><marker id="citation-arrow" viewBox="0 0 8 8" refX="8" refY="4" '
                   'markerWidth="7" markerHeight="7" markerUnits="userSpaceOnUse" orient="auto">'
                   '<path d="M0,0 L8,4 L0,8 Z" fill="#536fae"/></marker></defs>')
    linked = set()
    for left, right, weight in edges:
        linked.update((left, right))
        x1, y1 = positions[left]
        x2, y2 = positions[right]
        marker = ''
        if directed:
            distance = math.hypot(x2 - x1, y2 - y1)
            if distance > 6:
                x2 -= (x2 - x1) * 6 / distance
                y2 -= (y2 - y1) * 6 / distance
            marker = ' marker-end="url(#citation-arrow)"'
        stroke = min(3, 0.8 + 0.35 * weight)
        svg.append(f'<line class="link" x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                   f'stroke-width="{stroke:g}"{marker}/>')
    for key, (label, detail) in nodes.items():
        x, y = positions[key]
        css = 'node' if key in linked else 'node isolated'
        svg.append(f'<circle class="{css}" cx="{x:.2f}" cy="{y:.2f}" r="5">'
                   f'<title>{escape(label)} · {escape(detail)}</title></circle>')
    svg.append('</svg>')
    return ''.join(svg)


def chart_panel(title, rows):
    return (f'<section class="panel"><h2>{escape(title)}</h2>'
            f'{bar_chart(rows, title)}</section>')


def network_panel(title, stem, description, nodes, edges, *, directed):
    return (f'<section class="panel network-panel"><div class="panel-heading"><div>'
            f'<h2>{escape(title)}</h2><p>{len(nodes):,} nodes · {len(edges):,} links</p></div>'
            f'<a href="{stem}.graphml" download>GraphML ↓</a></div>'
            f'<div class="network-frame">{network_svg(nodes, edges, directed=directed)}</div>'
            f'<p class="legend">{escape(description)} Hover over a node to read its label.</p></section>')


def write_html_report(path, source, stats, papers, collaborations, citations, top):
    authors = {key: (stats.labels[key], f'{count} papers')
               for key, count in sorted(stats.authors.items())}
    charts = [chart_panel('Papers per year', sorted(stats.years.items()))]
    for title, counts in (('Top authors', stats.authors), ('Top events', stats.events),
                          ('Top journals', stats.journals)):
        charts.append(chart_panel(title, ranking(counts, stats.labels, top)))
    networks = [
        network_panel('Coauthor network', 'coauthors',
                      'Each line connects coauthors. Thicker lines indicate more papers together.',
                      authors, collaborations, directed=False),
        network_panel('Paper citation network', 'citations',
                      ('Arrows point from the citing paper to the cited paper.' if citations
                       else 'No citations between input papers were found in the available data.'),
                      papers, citations, directed=True),
    ]
    page = Template(TEMPLATE.read_text(encoding='utf-8')).substitute(
        source=escape(source.name), papers=f'{len(papers):,}', authors=f'{len(authors):,}',
        collaborations=f'{len(collaborations):,}', citations=f'{len(citations):,}',
        charts=''.join(charts), networks=''.join(networks),
    )
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(page)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
