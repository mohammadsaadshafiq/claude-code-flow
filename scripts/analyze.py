#!/usr/bin/env python3
"""
code-flow-explainer: static module-dependency analyzer.

Scans a repo, resolves import/require/from statements between local files,
and emits:
  - <out>.json : structured summary (entry points, hubs, cycles, edges)
  - <out>.mmd  : Mermaid flowchart
  - <out>.html : interactive force-graph (vis-network via CDN)

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
    lines.append("    classDef entry fill:#234b7e,stroke:#1a3860,color:#fff;")
    lines.append("    classDef hub fill:#4f81bd,stroke:#3a6294,color:#fff;")
    if not shown:
        lines.append("    empty[No local dependencies detected]")
    return "\n".join(lines)


HTML_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>Code Flow — {title}</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1117;color:#e6e6e6}}
  header{{padding:14px 20px;background:#181b24;border-bottom:1px solid #262a36}}
  header h1{{margin:0;font-size:16px;font-weight:600}}
  header .stats{{font-size:12px;color:#9aa4b2;margin-top:4px}}
  #net{{width:100vw;height:calc(100vh - 92px)}}
  .legend{{position:absolute;bottom:14px;left:14px;background:#181b24cc;padding:8px 12px;border-radius:8px;font-size:12px;border:1px solid #262a36}}
  .dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}}
</style></head>
<body>
<header><h1>Code Flow — {title}</h1>
<div class="stats">{nodes} files &middot; {edges} dependencies &middot; {entries} entry points &middot; {cycles} cycles</div></header>
<div id="net"></div>
<div class="legend">
  <div><span class="dot" style="background:#234b7e"></span>Entry point</div>
  <div><span class="dot" style="background:#4f81bd"></span>Hub (many dependents)</div>
  <div><span class="dot" style="background:#6b7280"></span>Module</div>
  <div style="margin-top:6px;color:#9aa4b2">Click a node to isolate its links</div>
</div>
<script>
const nodes = new vis.DataSet({nodes_json});
const edges = new vis.DataSet({edges_json});
const container = document.getElementById('net');
const data = {{nodes, edges}};
const options = {{
  nodes:{{shape:'dot',size:12,font:{{color:'#e6e6e6',size:12}},borderWidth:0}},
  edges:{{arrows:'to',color:{{color:'#3a4152',highlight:'#7aa2e3'}},smooth:{{type:'continuous'}},width:1}},
  physics:{{stabilization:true,barnesHut:{{gravitationalConstant:-8000,springLength:120}}}},
  interaction:{{hover:true,tooltipDelay:120}}
}};
const network = new vis.Network(container, data, options);
network.on('click', p => {{
  if(p.nodes.length){{
    const id=p.nodes[0];
    const conn=new Set([id]);
    edges.forEach(e=>{{if(e.from===id)conn.add(e.to);if(e.to===id)conn.add(e.from);}});
    nodes.forEach(n=>nodes.update({{id:n.id,hidden:!conn.has(n.id)}}));
  }} else {{ nodes.forEach(n=>nodes.update({{id:n.id,hidden:false}})); }}
}});
</script>
</body></html>
"""


def make_html(files_set, edges, meta, title):
    entry = set(meta["entry_points"])
    hub = {h["file"] for h in meta["hubs"]}
    used = set()
    for a, b in edges:
        used.add(a); used.add(b)
    node_list = []
    idx = {}
    for i, n in enumerate(sorted(used)):
        idx[n] = i
        color = "#6b7280"
        size = 10 + min(meta["in_deg"].get(n, 0) * 2, 26)
        if n in entry:
            color = "#234b7e"
        elif n in hub:
            color = "#4f81bd"
        node_list.append({"id": i, "label": short(n), "title": n, "color": color, "size": size})
    edge_list = [{"from": idx[a], "to": idx[b]} for a, b in edges if a in idx and b in idx]
    return HTML_TMPL.format(
        title=title,
        nodes=len(files_set),
        edges=len(edges),
        entries=len(meta["entry_points"]),
        cycles=len(meta["cycles"]),
        nodes_json=json.dumps(node_list),
        edges_json=json.dumps(edge_list),
    )


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

    out = args.out
    with open(f"{out}.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(f"{out}.mmd", "w", encoding="utf-8") as f:
        f.write(make_mermaid(edges, meta, args.max_nodes))
    with open(f"{out}.html", "w", encoding="utf-8") as f:
        f.write(make_html(files_set, edges, meta, os.path.basename(base)))

    print(f"Analyzed {len(files_set)} files, {len(edges)} dependencies.")
    print(f"  Entry points: {len(meta['entry_points'])}  Hubs: {len(meta['hubs'])}  Cycles: {len(meta['cycles'])}")
    print(f"Wrote {out}.json, {out}.mmd, {out}.html")


if __name__ == "__main__":
    main()
