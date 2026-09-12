# Projects managed 

The `commit_all` / `commit_show` tooling manages these 9 projects (default `MCP_COMMIT_PROJECTS`), all resolved under `$MCP_COMMIT_ROOT`/`$MY_GIT_DIR`:

> **Runtime source of truth**: the list itself lives in `${MY_GIT_DIR}/mcp/conf/mcp.conf`, in the `MCP_COMMIT_PROJECTS` variable (an exported environment variable of the same name overrides it). This table documents that configuration — to add or remove a managed project, edit `conf/mcp.conf` first, then mirror the change here.

| Project  | Nature                                                                 |
|----------|------------------------------------------------------------------------|
| `agents`   | ECA rules, sub-agent config, skills (`rules/`, `eca/config.json`, `skills/`) |
| `shell`    | Orchestrator `my_warp.sh` + base runtime `lib_shell.sh` + BATS suite       |
| `mcp`      | Bash MCP server library (`lib_mcp.sh`) + `mcp_server.sh`                   |
| `storm`    | StorM platform library (`lib_storm.sh`) + `storm-mcp.sh`                   |
| `ma-emacs` | Literate Emacs config (`README.org` → `.emacs`)                            |
| `rc`       | Shell/rc dotfiles                                                      |
| `ma-cgr`   | (personal project)                                                     |
| `ansible`  | Ansible playbooks                                                      |
| `tofu`     | Opentofu |
