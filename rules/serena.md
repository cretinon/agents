# Serena & graphify — navigate before reading

## Scope
Applies to any question about the code of `/root/git/<project>`: where a symbol lives, what it does, who calls it.
Serena (symbols) and graphify (knowledge graph) answer those without reading whole files, so try them
**before** `eca__grep` and `eca__read_file`, and never read a whole library to find one function.

## Serena — one shared server, one active project at a time
- The `serena` MCP server is started without `--project`: it serves every repository, but only one project is
  **active** at any moment.
- **Activate the project of the current workspace first** (`pwd` gives it): call `activate_project` with the
  absolute path, e.g. `/root/git/mcp`. A repository that is not registered yet is registered and its
  `project.yml` generated on the fly.
- Then, in this order: `get_symbols_overview` (file outline), `find_symbol` (`include_body` only when the body is
  really needed), `find_referencing_symbols` (callers), and only then a targeted partial read.
- Configured projects: `/root/git/mcp` (bash, yaml, python, go, json, markdown, cpp, plus `/root/git/shell` as an
  additional workspace folder so cross-repo references resolve) and `/root/git/tofu` (terraform, through the
  `terraform` → `/bin/tofu` symlink). For any other repository: activate it by path, then edit `language_servers`
  in its generated `project.yml` if the inferred list is wrong.
- Not covered: Emacs Lisp and Org (no language server exists for them) — use `eca__grep` there.
- The bash language server ignores `.bats` files; symbol tools see the `.sh` files only.
- Findings are only valid for the **active** project: a symbol called from a sibling repository returns no
  reference unless that repository is in the project's `ls_additional_workspace_folders`.

## graphify — a map, not ground truth
- One graph per repository, built code-only (local AST, no LLM) and kept **outside** the repository:
  `/root/.cache/graphify/<project>/graphify-out/graph.json`.
- Pass `project_path=/root/.cache/graphify/<project>` on every graphify tool call; without it the server answers
  for its default graph or returns an error.
- Use it to orient (which symbol or community owns a feature, `god_nodes`, `shortest_path`, `get_neighbors`), then
  confirm with Serena or a targeted read: the graph is extracted from the AST and misses dynamic constructs
  (`source` indirection, `eval`, dispatch by string).
- Rebuild it after a refactor:
  `graphify extract /root/git/<project> --code-only --out /root/.cache/graphify/<project>`.
- The graph is refreshed automatically before every graphify call by the ECA `preToolCall` hook
  `eca/hooks/graphify-refresh.sh` (projects under `/root/git` only, the target being the call's
  `project_path`), so a manual rebuild is only needed outside that root; when a stale graph cannot
  be refreshed the call is denied and must be reported instead of trusting the graph.

## Hard rules
- Never run `serena project health-check` for a normal task: it writes its log into
  `<repo>/.serena/logs/health-checks/`, i.e. **inside** the repository, and shows up in `git status`.
- Never let graphify write into a repository: always pass `--out /root/.cache/graphify/<project>`.
- Serena's own data belongs to `~/.serena/` and `~/.cache/serena/<project>/.serena/`; no repository may gain a
  `.serena/` or `graphify-out/` folder.
