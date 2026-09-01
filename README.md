# agents

Shared **ECA agent rules and skills** consumed by the ECA (Editor Code Assistant) configuration.

This repository is not a code library — it is the single source for the agent-facing
guidelines that ECA loads on top of the project-specific documentation (`AGENTS.md` files,
the `shell`/`mcp`/... libraries).

## Layout

```
README.md          This file
rules/             Path-scoped agent rules (loaded per file-path glob)
  shell.md         Rule applied when reading/writing **.sh files
  bats.md          Rule applied when reading/writing **.bats files
skills/            Agent skills (loaded by name when the task matches)
  bats/            BATS test-suite management skill (SKILL.md)
```

## How ECA loads these

`~/.config/eca/config.json` (a symlink to `$MY_GIT_DIR/ma-emacs/eca_config.json`)
references these paths:

```json
{
  "rules": [
    { "path": "~/git/agents/rules/shell.md" },
    { "path": "~/git/agents/rules/bats.md" }
  ],
  "skills": [
    { "path": "~/git/agents/skills/bats" }
  ]
}
```

- **Rules** are applied automatically by ECA based on the file path being read or edited:
  `shell.md` matches `**.sh`, `bats.md` matches `**.bats` (frontmatter `paths` glob).
- **Skills** are loaded on demand when a task matches the skill `description`
  (e.g. the `bats` skill for writing/refactoring `.bats` test files).

The global workflow rules (`eca_AI_dev.md`, `eca_language.md`) live in
`$MY_GIT_DIR/ma-emacs/` and are symlinked from `~/.config/eca/rules/`.

## Conventions

- **YAML frontmatter** is required on every rule (`agent`, `paths`, `enforce`) and skill
  (`name`, `description`). A rule without valid frontmatter is silently ignored by ECA.
- **Enforcement**: `enforce: read, modify` means ECA must surface the rule before both
  reading and editing a matching file.
- **Keep rules aligned with the authoritative project docs**: the real development rules
  live in `${MY_GIT_DIR}/shell/AGENTS.md`; these agent rules restate/enforce subsets of
  them (e.g. the BATS suite MUST be run through `my_warp.sh --lib <lib> -b`, never raw
  `bats`). When the project rules change, update these files in the same commit.
- **Testing**: these are markdown assets, not code — the `shell`/`mcp` quality gate
  (`-s`/`-b`/`-k`) does not apply. Validate by re-reading the file and checking ECA loads
  it (`eca-info` skill) after changes.
