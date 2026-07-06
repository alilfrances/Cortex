from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from .export import _community_map
from .models import Community, GraphEdge, GraphNode

_COLORS = [
    "#2563eb",
    "#16a34a",
    "#dc2626",
    "#9333ea",
    "#ea580c",
    "#0891b2",
    "#be123c",
    "#4f46e5",
]


def _safe_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":")).replace("</", "<\\/")


def write_html(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    communities: Mapping[str, int] | Sequence[Community],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if len(nodes) > 2000:
        path.write_text(
            """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Cortex graph</title>
<body style="font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.5">
<h1>Cortex graph is too large for the inline viewer</h1>
<p>This graph has more than 2000 nodes. Please use the Obsidian export instead for navigation.</p>
</body></html>
""",
            encoding="utf-8",
        )
        return

    community_by_node = _community_map(communities)
    labels = {node.node_id: node.label for node in nodes}
    payload_nodes = [
        {
            "id": node.node_id,
            "label": node.label,
            "kind": node.kind,
            "source": node.source_ref,
            "community": community_by_node.get(node.node_id, -1),
            "color": _COLORS[community_by_node.get(node.node_id, 0) % len(_COLORS)],
        }
        for node in nodes
    ]
    payload_edges = [
        {
            "source": edge.source,
            "target": edge.target,
            "relation": edge.relation,
            "weight": edge.weight,
        }
        for edge in edges
        if edge.source in labels and edge.target in labels
    ]
    graph_json = _safe_json({"nodes": payload_nodes, "edges": payload_edges})
    title = html.escape(path.name)
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Cortex graph - {title}</title>
<style>
html, body {{ height: 100%; margin: 0; overflow: hidden; background: #f8fafc; color: #0f172a; font-family: system-ui, sans-serif; }}
#canvas {{ width: 100vw; height: 100vh; display: block; cursor: grab; }}
#canvas:active {{ cursor: grabbing; }}
#hud {{ position: fixed; left: 16px; top: 16px; background: rgba(255,255,255,.92); border: 1px solid #cbd5e1; border-radius: 8px; padding: 10px 12px; font-size: 13px; box-shadow: 0 8px 20px rgba(15,23,42,.08); }}
#tip {{ position: fixed; display: none; pointer-events: none; background: #0f172a; color: white; border-radius: 6px; padding: 6px 8px; font-size: 12px; max-width: 320px; }}
</style>
</head>
<body>
<canvas id="canvas"></canvas>
<div id="hud">{len(nodes)} nodes · {len(payload_edges)} edges · wheel to zoom · drag to pan · hover for labels</div>
<div id="tip"></div>
<script>
const graph = {graph_json};
const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const tip = document.getElementById("tip");
let scale = 1;
let offsetX = 0;
let offsetY = 0;
let dragging = false;
let lastX = 0;
let lastY = 0;
const byId = new Map(graph.nodes.map(n => [n.id, n]));
function resize() {{
  canvas.width = Math.max(1, window.innerWidth * devicePixelRatio);
  canvas.height = Math.max(1, window.innerHeight * devicePixelRatio);
  canvas.style.width = window.innerWidth + "px";
  canvas.style.height = window.innerHeight + "px";
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
}}
function initPositions() {{
  const cx = window.innerWidth / 2;
  const cy = window.innerHeight / 2;
  const radius = Math.max(80, Math.min(cx, cy) - 80);
  graph.nodes.forEach((n, i) => {{
    const angle = (Math.PI * 2 * i) / Math.max(1, graph.nodes.length);
    n.x = cx + Math.cos(angle) * radius;
    n.y = cy + Math.sin(angle) * radius;
    n.vx = 0;
    n.vy = 0;
  }});
}}
function tick() {{
  for (let i = 0; i < graph.nodes.length; i++) {{
    for (let j = i + 1; j < graph.nodes.length; j++) {{
      const a = graph.nodes[i], b = graph.nodes[j];
      const dx = a.x - b.x, dy = a.y - b.y;
      const dist2 = Math.max(25, dx * dx + dy * dy);
      const force = 550 / dist2;
      a.vx += dx * force; a.vy += dy * force;
      b.vx -= dx * force; b.vy -= dy * force;
    }}
  }}
  graph.edges.forEach(e => {{
    const a = byId.get(e.source), b = byId.get(e.target);
    if (!a || !b) return;
    const dx = b.x - a.x, dy = b.y - a.y;
    const dist = Math.max(1, Math.hypot(dx, dy));
    const force = (dist - 120) * 0.0025 * Math.max(0.5, e.weight || 1);
    a.vx += dx / dist * force; a.vy += dy / dist * force;
    b.vx -= dx / dist * force; b.vy -= dy / dist * force;
  }});
  graph.nodes.forEach(n => {{
    n.vx += (window.innerWidth / 2 - n.x) * 0.0008;
    n.vy += (window.innerHeight / 2 - n.y) * 0.0008;
    n.vx *= 0.82; n.vy *= 0.82;
    n.x += n.vx; n.y += n.vy;
  }});
}}
function screen(n) {{ return {{x: n.x * scale + offsetX, y: n.y * scale + offsetY}}; }}
function draw() {{
  ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
  ctx.lineWidth = 1;
  ctx.strokeStyle = "rgba(71,85,105,.35)";
  graph.edges.forEach(e => {{
    const a = byId.get(e.source), b = byId.get(e.target);
    if (!a || !b) return;
    const as = screen(a), bs = screen(b);
    ctx.beginPath(); ctx.moveTo(as.x, as.y); ctx.lineTo(bs.x, bs.y); ctx.stroke();
  }});
  graph.nodes.forEach(n => {{
    const p = screen(n);
    ctx.beginPath();
    ctx.fillStyle = n.color;
    ctx.arc(p.x, p.y, Math.max(3, 5 * scale), 0, Math.PI * 2);
    ctx.fill();
  }});
}}
function animate() {{
  for (let i = 0; i < 3 && graph.nodes.length < 600; i++) tick();
  draw();
  requestAnimationFrame(animate);
}}
function hovered(x, y) {{
  let best = null, bestDist = 14;
  graph.nodes.forEach(n => {{
    const p = screen(n);
    const dist = Math.hypot(p.x - x, p.y - y);
    if (dist < bestDist) {{ best = n; bestDist = dist; }}
  }});
  return best;
}}
canvas.addEventListener("mousemove", event => {{
  if (dragging) {{
    offsetX += event.clientX - lastX;
    offsetY += event.clientY - lastY;
    lastX = event.clientX; lastY = event.clientY;
  }}
  const n = hovered(event.clientX, event.clientY);
  if (n) {{
    tip.style.display = "block";
    tip.style.left = (event.clientX + 12) + "px";
    tip.style.top = (event.clientY + 12) + "px";
    tip.textContent = n.label + " · " + n.kind + " · community " + n.community;
  }} else {{
    tip.style.display = "none";
  }}
}});
canvas.addEventListener("mousedown", event => {{ dragging = true; lastX = event.clientX; lastY = event.clientY; }});
window.addEventListener("mouseup", () => {{ dragging = false; }});
canvas.addEventListener("wheel", event => {{
  event.preventDefault();
  const before = scale;
  scale = Math.min(4, Math.max(0.2, scale * (event.deltaY < 0 ? 1.1 : 0.9)));
  offsetX = event.clientX - (event.clientX - offsetX) * (scale / before);
  offsetY = event.clientY - (event.clientY - offsetY) * (scale / before);
}}, {{passive: false}});
window.addEventListener("resize", resize);
resize();
initPositions();
animate();
</script>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")
