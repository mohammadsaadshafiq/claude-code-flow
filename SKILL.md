---
name: code-flow-explainer
description: >-
  Analyze and explain how code flows through a codebase. Use when the user wants
  to understand the structure of a repo, trace how modules/files depend on each
  other, see the import/dependency graph, find entry points, identify cycles, or
  get a visual + narrated explanation of "how this code works" / "the flow of the
  code" / "what calls what". Works for TypeScript/JavaScript (incl. Angular),
  Python, and mixed repos. Produces a Mermaid diagram, an interactive HTML graph,
  and a JSON summary that you should read to narrate the flow in plain language.
allowed-tools: Bash(python3 *), Bash(npx *), Bash(node *), Read
---

# Code Flow Explainer

Explain how a codebase is wired together: entry points, the dependency graph
between files/modules, cycles, and the most-depended-on ("hub") files. Output is
both visual and narrated.

## Usage

Run the analyzer from the project root (or pass a subdirectory):

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/analyze.py .
```

Useful flags:

- `--dir src/app` — analyze only a subfolder (good for large monorepos)
- `--include ts,tsx,js,py` — restrict to specific extensions
- `--max-nodes 120` — cap graph size (default 200); huge repos get summarized
- `--out flow` — output filename stem (default `code-flow`)

This writes three files next to where you run it:

1. `code-flow.html` — interactive graph (drag, zoom, click a node to isolate its
   in/out edges). Open it in a browser.
2. `code-flow.mmd` — a Mermaid diagram you can paste into docs / PRs.
3. `code-flow.json` — structured summary (entry points, hubs, cycles, edges).

## What to do after running

1. **Read `code-flow.json`** with the Read tool. Do NOT dump the raw JSON at the
   user. Use it to explain the flow in prose.
2. Structure the explanation like this:
   - **Entry points** — where execution starts (files nothing else imports, or
     files matching main/index/app/bootstrap patterns).
   - **Core path** — walk from an entry point through the highest-traffic edges,
     describing what each layer does (e.g. component → service → data layer).
   - **Hubs** — files many others depend on; changing these is high-blast-radius.
   - **Cycles** — flag any circular dependencies as things to watch/refactor.
3. Reference the diagram: tell the user to open `code-flow.html`, and optionally
   inline the Mermaid from `code-flow.mmd` if they want it in a doc.
4. Keep it grounded in the actual files found — never invent modules.

## Notes

- The analyzer is static: it maps imports/requires, not runtime call order.
  That's the reliable, framework-agnostic signal for "code flow" at the module
  level. For function-level call graphs, say so and offer a language-specific
  tool (e.g. `pyan` for Python, `madge --dot` for JS/TS).
- Resolves tsconfig path aliases (`@core/*`, `@env`, ...) from tsconfig.json /
  tsconfig.base.json / tsconfig.app.json, and dynamic `import()` used by
  Angular lazy routes.
- If `edges_truncated` is true in the JSON, the edge list is capped at 400 —
  rely on `entry_points`/`hubs`/`cycles` for the big picture and rerun with
  `--dir` on a subfolder for detail.
- Zero external Python dependencies — standard library only.
