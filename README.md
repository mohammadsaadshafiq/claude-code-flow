# code-flow-explainer — Claude Code skill

Explains how a codebase is wired: entry points, dependency graph, hubs, cycles.
Outputs an interactive HTML graph, a Mermaid diagram, and a JSON summary.

## Install

```bash
git clone <this-repo> ~/.claude/skills/code-flow-explainer
```

or unzip so that `~/.claude/skills/code-flow-explainer/SKILL.md` exists.

## Use

In Claude Code: type `/code-flow-explainer`, or just ask
"explain the code flow of this repo".

Manual run:

```bash
python3 ~/.claude/skills/code-flow-explainer/scripts/analyze.py .
```

## Features

- TS/JS/JSX/TSX + Python, zero dependencies (stdlib only)
- Resolves relative imports, dynamic `import()` (Angular lazy routes),
  and **tsconfig path aliases** (`@core/*`, `@env`, ...)
- Entry points, hub files (blast radius), circular dependencies, orphans
- Safe on large/deep repos (iterative cycle detection, node caps)

## Outputs (generated in project root — gitignored here)

- `code-flow.html` — interactive graph (open in browser)
- `code-flow.mmd`  — Mermaid, paste into PRs/docs
- `code-flow.json` — structured summary Claude narrates from
