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
  language.md      Global project rule: always respond in English
  AI_dev.md        Global project rule: phased AI development workflow
skills/            Agent skills (loaded by name when the task matches)
  bats/            BATS test-suite management skill (SKILL.md)
eca/               ECA configuration assets
  config.json      The ECA configuration consumed by ~/.config/eca/config.json
```

## How ECA loads these

`~/.config/eca/config.json` (a symlink to `$MY_GIT_DIR/agents/eca/config.json`)
references these paths:

```json
{
  "rules": [
    { "path": "~/git/agents/rules/shell.md" },
    { "path": "~/git/agents/rules/bats.md" },
    { "path": "~/git/agents/rules/language.md" },
    { "path": "~/git/agents/rules/AI_dev.md" }
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
- **Rules without a `paths` glob** (e.g. `language.md`, `AI_dev.md`) load as
  project/workspace rules and apply to the whole session regardless of the file
  being touched.

## Conventions

- **YAML frontmatter** is recommended on every rule (`agent`, `paths`, `enforce`) and skill
  (`name`, `description`). `paths` is what makes a rule path-scoped; a rule **without** a
  `paths` glob is loaded as a project/workspace rule (e.g. `language.md`).
- **Enforcement**: `enforce: read, modify` means ECA must surface the rule before both
  reading and editing a matching file.
- **Keep rules aligned with the authoritative project docs**: the real development rules
  live in `${MY_GIT_DIR}/shell/AGENTS.md`; these agent rules restate/enforce subsets of
  them (e.g. tests MUST be run through `my_warp.sh --lib <lib> -b` — developers run only
  the tests they wrote via a filter regex, while the `code_reviewer` sub-agent runs the
  full `-s|-b|-k` gate — never raw `bats`). When the project rules change, update these
  files in the same commit.
- **Testing**: these are markdown assets, not code — the `shell`/`mcp` quality gate
  (`-s`/`-b`/`-k`) does not apply. Validate by re-reading the file and checking ECA loads
  it (`eca-info` skill) after changes.
