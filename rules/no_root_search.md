# Rule: no root search — SLASH IS FORBIDDEN

## Scope
Applies to every command run through `eca__shell_command`, to any MCP shell-like tool,
and to the file/search tools (`eca__grep`, `eca__read_file`, `eca__directory_tree`).

## Hard prohibition (no exception)
- NEVER start a recursive search or lookup at `/` — nor at `//`, `/*`, `/**`, `cd /`,
  or `.` after a `cd /`.
- FORBIDDEN verbatim: `find /`, `find / -type d …`, `find / -name …`, `grep -r … /`,
  `rg … /`, `ls -R /`, `ls /*`, `du -sh /`, `tree /`, `ncdu /`, `tar -cf x.tar /`,
  `locate`, `mlocate`, `plocate`, `updatedb`.
- This host carries many NFS filesystems behind automount: touching `/` wakes them and
  the command hangs for minutes (60000 ms tool timeout) — it is also a data-leak risk.

## What to do instead
1. Search only the workspace root that can hold the answer: `/root/git/<project>`,
   `/root/.cache`, `/tmp`, `/root/.emacs.d`, `/root/.config/eca`.
2. Bound every search: `timeout 20 find /root/git/<project> -maxdepth 4 -type d -name '<x>'`.
3. Prefer `eca__grep` (or the editor's search) on a directory you already know.
4. For "is X installed / where is X", do not scan the disk: use `which`/`type`,
   `_installed`, `dpkg -S`, or read the project's `README`.
5. Need a path outside your roots (e.g. `/root/go/pkg/mod`)? ASK the user for the exact
   path instead of discovering it with `find`.

## Escalation
If the answer truly requires scanning `/`, STOP and ask the user. Never run a forbidden
command "just to check", and never re-run it after a timeout.
