#!/usr/bin/env python3
"""
code-flow-explainer: static module-dependency analyzer.

Scans a repo, resolves import/require/from statements between local files,
and emits:
  - <out>.md   : report with embedded Mermaid diagram — renders as a real
                 diagram in IDE markdown preview (VS Code/JetBrains) & GitHub
  - <out>.html : interactive board-style graph (vis-network via CDN)
  - <out>.json : structured summary, only with --json

Standard library only. Supports TS/JS(X), Python. Ignores node_modules, .git,
dist, build, venv, __pycache__, etc.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict, deque

DEFAULT_EXTS = ["ts", "tsx", "js", "jsx", "mjs", "cjs", "py"]
IGNORE_DIRS = {
    "node_modules", ".git", "dist", "build", "out", ".next", ".nuxt",
    "coverage", "venv", ".venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".angular", "vendor", "target", ".cache", "tmp",
}
ENTRY_HINTS = re.compile(
    r"(^|[/\\])(main|index|app|bootstrap|server|__main__|manage|cli)\.",
    re.IGNORECASE,
)

# import forms we care about (local ones start with . or are resolvable)
JS_PATTERNS = [
    re.compile(r"""import\s+[^'"]*from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""import\s+['"]([^'"]+)['"]"""),
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""export\s+[^'"]*from\s+['"]([^'"]+)['"]"""),
]
# (module_spec, imported_names_or_None)
PY_FROM = re.compile(r"""^\s*from\s+([.\w]+)\s+import\s+(.+)$""", re.MULTILINE)
PY_IMPORT = re.compile(r"""^\s*import\s+([.\w]+)""", re.MULTILINE)


def parse_args():
    p = argparse.ArgumentParser(description="Explain code flow via dependency graph.")
    p.add_argument("root", nargs="?", default=".", help="project root")
    p.add_argument("--dir", default=None, help="analyze only this subdir (relative to root)")
    p.add_argument("--include", default=",".join(DEFAULT_EXTS), help="comma-separated extensions")
    p.add_argument("--max-nodes", type=int, default=200, help="cap graph node count")
    p.add_argument("--out", default="code-flow", help="output filename stem")
    p.add_argument("--json", action="store_true",
                   help="also write <out>.json (machine-readable summary)")
    return p.parse_args()


def collect_files(base, exts):
    files = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.split(".")[-1].lower() in exts:
                files.append(os.path.join(dirpath, fn))
    return files


def norm(path, base):
    return os.path.relpath(path, base).replace("\\", "/")


def load_ts_aliases(base):
    """Parse tsconfig.json compilerOptions.paths into (prefix, targets) rules."""
    rules = []
    for name in ("tsconfig.json", "tsconfig.base.json", "tsconfig.app.json"):
        p = os.path.join(base, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                # tolerate comments/trailing commas common in tsconfig
                txt = re.sub(r"//[^\n]*", "", f.read())
                txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.DOTALL)
                txt = re.sub(r",\s*([}\]])", r"\1", txt)
                cfg = json.loads(txt)
        except Exception:
            continue
        co = cfg.get("compilerOptions", {})
        base_url = co.get("baseUrl", ".")
        for alias, targets in (co.get("paths") or {}).items():
            a = alias[:-1] if alias.endswith("*") else alias
            for t in targets:
                t = t[:-1] if t.endswith("*") else t
                rules.append((a, os.path.normpath(os.path.join(base, base_url, t))))
    return rules


def resolve_js(spec, from_file, files_set, base, aliases):
    """Resolve a relative or aliased JS/TS import to a known file."""
    target = None
    if spec.startswith("."):
        target = os.path.normpath(os.path.join(os.path.dirname(from_file), spec))
    else:
        for prefix, tgt_base in aliases:
            if spec == prefix.rstrip("/") or spec.startswith(prefix):
                rest = spec[len(prefix):].lstrip("/")
                target = os.path.normpath(os.path.join(tgt_base, rest)) if rest else tgt_base
                break
    if target is None:
        return None  # external package
    candidates = []
    for ext in ["", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]:
        candidates.append(target + ext)
    for idx in ["index.ts", "index.tsx", "index.js", "index.jsx"]:
        candidates.append(os.path.join(target, idx))
    for c in candidates:
        rc = norm(c, base)
        if rc in files_set:
            return rc
    return None


def resolve_py(spec, from_file, files_set, base):
    """Resolve a python import (relative or dotted) to a known file."""
    from_dir = os.path.dirname(from_file)
    # relative import: leading dots
    if spec.startswith("."):
        up = len(spec) - len(spec.lstrip("."))
        rest = spec.lstrip(".").replace(".", "/")
        target_dir = from_dir
        for _ in range(up - 1):
            target_dir = os.path.dirname(target_dir)
        target = os.path.normpath(os.path.join(target_dir, rest)) if rest else target_dir
    else:
        # absolute-ish dotted path, resolve from base
        target = os.path.normpath(os.path.join(base, spec.replace(".", "/")))
    for cand in [target + ".py", os.path.join(target, "__init__.py")]:
        rc = norm(cand, base)
        if rc in files_set:
            return rc
    return None


def build_graph(files, base):
    files_set = {norm(f, base) for f in files}
    aliases = load_ts_aliases(base)
    edges = set()
    for f in files:
        rf = norm(f, base)
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                src = fh.read()
        except Exception:
            continue
        is_py = f.endswith(".py")
        if is_py:
            specs = []
            for mod, names in PY_FROM.findall(src):
                specs.append(mod)
                # 'from pkg import a, b' -> a,b may be submodules pkg.a / pkg.b
                clean = names.split("#")[0].replace("(", "").replace(")", "")
                for nm in clean.split(","):
                    nm = nm.strip().split(" as ")[0].strip()
                    if nm and nm != "*":
                        joiner = "" if mod.endswith(".") else "."
                        specs.append(f"{mod}{joiner}{nm}" if mod != "." else f".{nm}")
            specs.extend(PY_IMPORT.findall(src))
        else:
            specs = []
            for pat in JS_PATTERNS:
                specs.extend(pat.findall(src))
        for spec in specs:
            if is_py:
                tgt = resolve_py(spec, f, files_set, base)
            else:
                tgt = resolve_js(spec, f, files_set, base, aliases)
            if tgt and tgt != rf:
                edges.add((rf, tgt))
    return files_set, edges


def find_cycles(nodes, out_adj):
    """Return a few simple cycles via iterative DFS (safe on deep graphs)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    cycles = []
    for start in nodes:
        if color[start] != WHITE:
            continue
        # stack holds (node, iterator over its children)
        path = [start]
        on_path = {start}
        iters = [iter(sorted(out_adj.get(start, ())))]
        color[start] = GRAY
        while iters:
            try:
                v = next(iters[-1])
            except StopIteration:
                done = path.pop()
                on_path.discard(done)
                color[done] = BLACK
                iters.pop()
                continue
            c = color.get(v, BLACK)
            if c == GRAY and v in on_path:
                i = path.index(v)
                cyc = path[i:] + [v]
                if cyc not in cycles and len(cycles) < 12:
                    cycles.append(cyc)
            elif c == WHITE:
                color[v] = GRAY
                path.append(v)
                on_path.add(v)
                iters.append(iter(sorted(out_adj.get(v, ()))))
    return cycles


def analyze(files_set, edges):
    out_adj = defaultdict(set)
    in_adj = defaultdict(set)
    for a, b in edges:
        out_adj[a].add(b)
        in_adj[b].add(a)

    in_deg = {n: len(in_adj.get(n, ())) for n in files_set}
    out_deg = {n: len(out_adj.get(n, ())) for n in files_set}

    entry_points = sorted(
        n for n in files_set
        if in_deg[n] == 0 and out_deg[n] > 0
    )
    hinted = sorted(n for n in files_set if ENTRY_HINTS.search(n))
    entry_points = list(dict.fromkeys(hinted + entry_points))

    hubs = sorted(files_set, key=lambda n: in_deg[n], reverse=True)
    hubs = [{"file": n, "dependents": in_deg[n]} for n in hubs if in_deg[n] > 0][:10]

    orphans = sorted(n for n in files_set if in_deg[n] == 0 and out_deg[n] == 0)
    cycles = find_cycles(sorted(files_set), out_adj)

    return {
        "entry_points": entry_points[:15],
        "hubs": hubs,
        "cycles": cycles,
        "orphans": orphans[:30],
        "in_deg": in_deg,
        "out_deg": out_deg,
    }


def short(name):
    return name.rsplit("/", 1)[-1]


def make_mermaid(edges, meta, max_nodes):
    # pick most-connected nodes to keep the diagram readable
    score = defaultdict(int)
    for a, b in edges:
        score[a] += 1
        score[b] += 1
    keep = set(sorted(score, key=lambda n: score[n], reverse=True)[:max_nodes])
    ids = {}
    lines = ["flowchart LR"]
    entry = set(meta["entry_points"])
    hub = {h["file"] for h in meta["hubs"]}

    def nid(n):
        if n not in ids:
            ids[n] = f"n{len(ids)}"
        return ids[n]

    shown = [(a, b) for a, b in edges if a in keep and b in keep]
    for a, b in sorted(shown):
        lines.append(f'    {nid(a)}["{short(a)}"] --> {nid(b)}["{short(b)}"]')
    entry_ids = [nid(n) for n in sorted(entry & keep) if n in ids]
    hub_ids = [nid(n) for n in sorted(hub & keep) if n in ids and n not in entry]
    if entry_ids:
        lines.append(f"    class {','.join(entry_ids)} entry;")
    if hub_ids:
        lines.append(f"    class {','.join(hub_ids)} hub;")
    lines.append("    classDef entry fill:#cde2fb,stroke:#2a78d6,color:#0b0b0b;")
    lines.append("    classDef hub fill:#c9f0e1,stroke:#1baf7a,color:#0b0b0b;")
    if not shown:
        lines.append("    empty[No local dependencies detected]")
    return "\n".join(lines)


def make_markdown(files_set, edges, meta, title, max_nodes):
    """IDE-viewable report: Mermaid diagram (renders in VS Code/JetBrains/
    GitHub markdown preview) plus the summary sections."""
    n_cyc = len(meta["cycles"])
    lines = [
        f"# Code flow — {title}",
        "",
        f"{len(files_set)} files · {len(edges)} dependencies · "
        f"{len(meta['entry_points'])} entry points · {n_cyc} cycle{'s' if n_cyc != 1 else ''}",
        "",
        "```mermaid",
        make_mermaid(edges, meta, max_nodes),
        "```",
        "",
        "## Entry points",
        "",
    ]
    lines += [f"- `{n}`" for n in meta["entry_points"]] or ["- none detected"]
    lines += ["", "## Hubs (most depended-on)", ""]
    lines += [f"- `{h['file']}` — imported by {h['dependents']}" for h in meta["hubs"]] or ["- none"]
    lines += ["", "## Circular dependencies", ""]
    if meta["cycles"]:
        lines += ["- " + " → ".join(f"`{n}`" for n in cyc) for cyc in meta["cycles"]]
    else:
        lines.append("- none found")
    if meta["orphans"]:
        lines += ["", "## Orphans (unconnected files)", ""]
        lines += [f"- `{n}`" for n in meta["orphans"]]
    lines += [
        "",
        "---",
        "",
        "_Interactive version: open `code-flow.html` in a browser "
        "(drag, zoom, filter, click a node to isolate its links)._",
        "",
    ]
    return "\n".join(lines)


# Board-style UI. Tokens (__TITLE__, __NODES__, ...) are substituted with
# str.replace, not str.format, so the CSS/JS below needs no brace escaping.
HTML_TMPL = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Code Flow — __TITLE__</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  :root{
    --plane:#f9f9f7; --card:#ffffff; --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
    --hairline:#e1e0d9; --accent:#2a78d6; --critical:#d03b3b;
    --shadow:0 1px 2px rgba(11,11,11,.05), 0 6px 16px rgba(11,11,11,.07);
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;display:flex;flex-direction:column;
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    background:var(--plane);color:var(--ink)}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
    padding:12px 20px;background:var(--card);border-bottom:1px solid var(--hairline)}
  header h1{margin:0;font-size:15px;font-weight:650;letter-spacing:-.01em}
  header h1 span{color:var(--muted);font-weight:500}
  .chips{display:flex;gap:8px;flex-wrap:wrap}
  .chip{font-size:12px;color:var(--ink-2);background:var(--plane);
    border:1px solid var(--hairline);border-radius:999px;padding:3px 10px;white-space:nowrap}
  .chip b{color:var(--ink);font-weight:650}
  .chip.alert{color:var(--critical);border-color:#f0c7c7;background:#fdf4f4}
  .chip.alert b{color:var(--critical)}
  .spacer{flex:1}
  #search{font:inherit;font-size:13px;color:var(--ink);background:var(--plane);
    border:1px solid var(--hairline);border-radius:8px;padding:6px 12px;width:230px;outline:none}
  #search:focus{border-color:var(--accent);background:var(--card)}
  #search::placeholder{color:var(--muted)}
  #fit{font:inherit;font-size:13px;font-weight:550;color:var(--ink-2);background:var(--card);
    border:1px solid var(--hairline);border-radius:8px;padding:6px 14px;cursor:pointer}
  #fit:hover{border-color:var(--muted);color:var(--ink)}
  #net{flex:1;min-height:0;
    background-image:radial-gradient(var(--hairline) 1px, transparent 1px);
    background-size:22px 22px}
  .legend{position:fixed;bottom:16px;left:16px;background:var(--card);
    border:1px solid var(--hairline);border-radius:12px;padding:10px 14px;
    font-size:12px;color:var(--ink-2);box-shadow:var(--shadow);line-height:2}
  .sw{display:inline-block;width:12px;height:12px;border-radius:4px;
    margin-right:8px;vertical-align:-2px;border:2px solid}
  .cyc{display:inline-block;width:14px;margin-right:8px;vertical-align:2px;
    border-top:2px dashed var(--critical)}
  .hint{color:var(--muted);margin-top:2px}
  div.vis-tooltip{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif !important;
    font-size:12px !important;color:var(--ink-2) !important;background:var(--card) !important;
    border:1px solid var(--hairline) !important;border-radius:8px !important;
    padding:8px 11px !important;box-shadow:var(--shadow) !important;white-space:pre !important}
</style></head>
<body>
<header>
  <h1>__TITLE__ <span>· code flow</span></h1>
  <div class="chips">
    <span class="chip"><b>__FILE_COUNT__</b> files</span>
    <span class="chip"><b>__EDGE_COUNT__</b> dependencies</span>
    <span class="chip"><b>__ENTRY_COUNT__</b> entry points</span>
    <span class="chip__CYCLE_ALERT__"><b>__CYCLE_COUNT__</b> cycles</span>
  </div>
  <div class="spacer"></div>
  <input id="search" type="search" placeholder="Filter files… (e.g. auth)">
  <button id="fit" title="Fit graph to view">Fit</button>
</header>
<div id="net"></div>
<div class="legend">
  <div><span class="sw" style="background:#cde2fb;border-color:#2a78d6"></span>Entry point</div>
  <div><span class="sw" style="background:#c9f0e1;border-color:#1baf7a"></span>Hub (many dependents)</div>
  <div><span class="sw" style="background:#ffffff;border-color:#e1e0d9"></span>Module</div>
  <div><span class="cyc"></span>Circular dependency</div>
  <div class="hint">Click a node to isolate its links · click canvas to reset</div>
</div>
<script>
const nodes = new vis.DataSet(__NODES_JSON__);
const edges = new vis.DataSet(__EDGES_JSON__);
const container = document.getElementById('net');
const options = {
  nodes:{
    shape:'box', shapeProperties:{borderRadius:10}, margin:10,
    widthConstraint:{maximum:180},
    font:{color:'#0b0b0b',size:13,face:'system-ui,-apple-system,Segoe UI,Roboto,sans-serif'},
    shadow:{enabled:true,color:'rgba(11,11,11,0.10)',size:10,x:0,y:3},
    borderWidth:1.5, borderWidthSelected:2.5
  },
  edges:{
    arrows:{to:{enabled:true,scaleFactor:0.55}},
    color:{color:'#c3c2b7',highlight:'#2a78d6',hover:'#2a78d6'},
    smooth:{type:'cubicBezier',roundness:0.45},
    width:1.5, hoverWidth:0.5, selectionWidth:1
  },
  physics:{
    stabilization:{iterations:300},
    barnesHut:{gravitationalConstant:-9000,springLength:150,springConstant:0.03,avoidOverlap:0.4}
  },
  interaction:{hover:true,tooltipDelay:150}
};
const network = new vis.Network(container, {nodes, edges}, options);
network.once('stabilizationIterationsDone', () => network.fit({animation:false}));
document.getElementById('fit').onclick = () =>
  network.fit({animation:{duration:400,easingFunction:'easeInOutQuad'}});
network.on('click', p => {
  if(p.nodes.length){
    const id=p.nodes[0];
    const conn=new Set([id]);
    edges.forEach(e=>{if(e.from===id)conn.add(e.to);if(e.to===id)conn.add(e.from);});
    nodes.forEach(n=>nodes.update({id:n.id,hidden:!conn.has(n.id)}));
  } else { nodes.forEach(n=>nodes.update({id:n.id,hidden:false})); }
});
document.getElementById('search').addEventListener('input', e => {
  const q=e.target.value.trim().toLowerCase();
  nodes.forEach(n=>nodes.update({id:n.id,opacity:(!q||n.path.toLowerCase().includes(q))?1:0.15}));
});
</script>
</body></html>
"""

# Node styling by role: light tint fill + saturated border, dark label text.
# Palette pair (#2a78d6 / #1baf7a) is CVD-validated; cycle red is a reserved
# status color, applied to borders/edges only — labels always carry identity.
NODE_STYLES = {
    "entry":  {"bg": "#cde2fb", "border": "#2a78d6", "font": "#0b0b0b"},
    "hub":    {"bg": "#c9f0e1", "border": "#1baf7a", "font": "#0b0b0b"},
    "module": {"bg": "#ffffff", "border": "#e1e0d9", "font": "#52514e"},
}


def make_html(files_set, edges, meta, title):
    entry = set(meta["entry_points"])
    hub = {h["file"] for h in meta["hubs"]}
    cyc_edges = set()
    for cyc in meta["cycles"]:
        for i in range(len(cyc) - 1):
            cyc_edges.add((cyc[i], cyc[i + 1]))
    cyc_nodes = {n for e in cyc_edges for n in e}
    out_deg = defaultdict(int)
    for a, _ in edges:
        out_deg[a] += 1
    used = set()
    for a, b in edges:
        used.add(a); used.add(b)
    node_list = []
    idx = {}
    for i, n in enumerate(sorted(used)):
        idx[n] = i
        role = "entry" if n in entry else "hub" if n in hub else "module"
        style = NODE_STYLES[role]
        in_deg = meta["in_deg"].get(n, 0)
        border = "#d03b3b" if n in cyc_nodes else style["border"]
        node_list.append({
            "id": i,
            "label": short(n),
            "path": n,
            "title": f"{n}\nimports: {out_deg[n]} · imported by: {in_deg}",
            "color": {
                "background": style["bg"], "border": border,
                "highlight": {"background": style["bg"], "border": "#2a78d6"},
                "hover": {"background": style["bg"], "border": border},
            },
            "font": {"color": style["font"], "size": 12 + min(in_deg, 6)},
            "margin": 9 + min(in_deg, 8),
            "borderWidth": 2.5 if n in cyc_nodes else 1.5,
        })
    edge_list = []
    for a, b in edges:
        if a not in idx or b not in idx:
            continue
        e = {"from": idx[a], "to": idx[b]}
        if (a, b) in cyc_edges:
            e.update({"dashes": [6, 4], "width": 2,
                      "color": {"color": "#d03b3b", "highlight": "#b52c2c", "hover": "#b52c2c"}})
        edge_list.append(e)
    n_cycles = len(meta["cycles"])
    return (HTML_TMPL
            .replace("__TITLE__", title)
            .replace("__FILE_COUNT__", str(len(files_set)))
            .replace("__EDGE_COUNT__", str(len(edges)))
            .replace("__ENTRY_COUNT__", str(len(meta["entry_points"])))
            .replace("__CYCLE_ALERT__", " alert" if n_cycles else "")
            .replace("__CYCLE_COUNT__", str(n_cycles))
            .replace("__NODES_JSON__", json.dumps(node_list))
            .replace("__EDGES_JSON__", json.dumps(edge_list)))


def main():
    args = parse_args()
    exts = [e.strip().lower().lstrip(".") for e in args.include.split(",") if e.strip()]
    base = os.path.abspath(args.root)
    scan_dir = os.path.join(base, args.dir) if args.dir else base
    if not os.path.isdir(scan_dir):
        print(f"error: not a directory: {scan_dir}", file=sys.stderr)
        sys.exit(1)

    files = collect_files(scan_dir, exts)
    if not files:
        print(f"No matching files ({','.join(exts)}) under {scan_dir}", file=sys.stderr)
        sys.exit(1)

    files_set, edges = build_graph(files, base)
    meta = analyze(files_set, edges)

    out = args.out
    title = os.path.basename(base)
    written = [f"{out}.md", f"{out}.html"]
    with open(f"{out}.md", "w", encoding="utf-8") as f:
        f.write(make_markdown(files_set, edges, meta, title, args.max_nodes))
    with open(f"{out}.html", "w", encoding="utf-8") as f:
        f.write(make_html(files_set, edges, meta, title))

    if args.json:
        all_edges = sorted([[a, b] for a, b in edges])
        summary = {
            "root": norm(scan_dir, base) or ".",
            "file_count": len(files_set),
            "edge_count": len(edges),
            "edges_truncated": len(all_edges) > 400,
            "entry_points": meta["entry_points"],
            "hubs": meta["hubs"],
            "cycles": meta["cycles"],
            "orphans": meta["orphans"],
            "edges": all_edges[:400],
        }
        with open(f"{out}.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        written.append(f"{out}.json")

    # remove leftovers from prior runs / older versions so stale outputs
    # never get mistaken for the current graph
    stale = [f"{out}.mmd"] + ([] if args.json else [f"{out}.json"])
    removed = [s for s in stale if os.path.isfile(s) and (os.remove(s) or True)]
    if removed:
        print(f"Removed stale {', '.join(removed)}")

    print(f"Analyzed {len(files_set)} files, {len(edges)} dependencies.")
    print(f"  Entry points: {len(meta['entry_points'])}  Hubs: {len(meta['hubs'])}  Cycles: {len(meta['cycles'])}")
    print(f"Wrote {', '.join(written)}")
    print(f"View {out}.md in your IDE (Mermaid renders in markdown preview), "
          f"or open {out}.html in a browser.")


if __name__ == "__main__":
    main()
