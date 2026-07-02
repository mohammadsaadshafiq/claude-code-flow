---
description: Visualize and explain a code flow — tell it which flow you care about
argument-hint: [which flow, e.g. "the auth flow" or "how src/app is wired"]
allowed-tools: Bash(python3 *), Read
---

The user wants a visual, narrated explanation of this flow: **$ARGUMENTS**

If no flow was given, explain the overall flow of the repo.

1. Run the analyzer from the repo root:

   ```bash
   python3 ~/.claude/skills/code-flow-explainer/scripts/analyze.py .
   ```

   If the requested flow lives in a subfolder, narrow the graph with
   `--dir <folder>` (e.g. `--dir src/app`). For huge repos add `--max-nodes 120`.

2. Read `code-flow.json` with the Read tool. Do NOT dump the raw JSON.
   Narrate only the parts relevant to the requested flow:
   - Start at the entry point that reaches the requested flow.
   - Walk the dependency chain file by file, explaining what each layer does.
   - Call out hubs the flow passes through (high blast radius when changed).
   - Flag any cycles that involve files in this flow.
   If the requested flow doesn't match anything in the graph, say so and list
   the closest matches — never invent modules.

3. Point the user at the visuals: `code-flow.html` (interactive graph — click a
   node to isolate its links) and `code-flow.mmd` (Mermaid, paste into PRs/docs).
