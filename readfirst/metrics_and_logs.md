# Metrics and logs — centralization, content, search, capacity

Companion to `fleet.md`. The observability guest is `victoria` (`192.168.2.125`), the
receiver is `otel-receiv` (`192.168.2.202`), the dashboards are on `grafana` (`:3000`).

`fleet.md` §5 remains the owner of the port and link inventory: when a port moves, update
both documents together. Grafana needs its own login, the two databases are open on the LAN.

## 1. How metrics and logs are centralized

- Every Debian 12/13 guest runs an `otelcol-contrib` **agent**, and so does the hypervisor
  `pve` (group `proxmox`): installed by the `catalog/otel_collector.yaml` playbook of `git/ansible`.
- The agent reads three things on its own guest: the `hostmetrics` of the machine, the
  OTLP of its local applications, and its systemd journal.
- It ships all of it to the **receiver** `otel-receiv` over OTLP: `:4317` (gRPC) and
  `:4318` (HTTP). One agent, one hop; the only local storage is the guest journal.
- The receiver splits the flow: the logs are pushed to **VictoriaLogs**
  `192.168.2.125:9428` (OTLP), the metrics are re-exposed on `:1234` for a scraper.
- **VictoriaMetrics** `192.168.2.125:8428` scrapes that endpoint (job `otelcol-fleet`) once
  a minute, the hypervisor endpoint `:1235` (job `pve-otel`), and the series of the stack
  itself (job `victoria-stack`).
- That scrape asks for the UTF-8 form of the exposition format, so the metric **and** label
  names stay the OpenTelemetry ones: `system.cpu.utilization`, `host.name`, `container.name`.
- Reading: **Grafana** `192.168.2.30:3000` (datasource = VictoriaMetrics), and the two
  `vmalert` instances (log rules against VictoriaLogs, metric rules against VictoriaMetrics).
  Neither notifies anything: their alerts are only visible in the UIs.
- Producers that are not guests:
  - `pve` — its own OTel metric server pushes node/VM/CT/storage metrics to
    `otel-receiv:4319` (OTLP/HTTP, JSON), on a port of its own because those names are
    translated to `proxmox_*` on the way. The same playbook also installs the ordinary
    agent on it, so its journal (the only record of the node) is in the log database too.
  - `opnsense-lan` — pushes syslog to `otel-receiv:5514/tcp`, and is polled over its HTTPS
    API by `opnsense2otel`, which runs on `otel-receiv` and pushes over OTLP.
  - `synology` — pushes DSM syslog to `otel-receiv:54526/udp`, and answers SNMP v2c on
    `:161`, polled every 30 s by the receiver.
  - `pihole` — has no metrics endpoint of its own, so `pihole-exporter` (on `otel-receiv`,
    `127.0.0.1:9617`) polls its REST API, and the receiver scrapes that exporter.
- Retention and limits: VictoriaLogs keeps 90 days and drops its oldest daily partition
  above 6 GiB; VictoriaMetrics keeps 3 months and turns read-only below 2 GiB of free
  space; each guest keeps 7 days of its own journal in `/var/log/journal`.
- Nothing is authenticated or encrypted: whoever reaches `:9428`, `:8428`, `:3000`,
  `:1234` or `:1235` on the LAN reads everything.

## 2. What we have

**Metrics** — the names are the OpenTelemetry ones, dots included:

- `system.*` — one series set per guest: `system.cpu.utilization`,
  `system.memory.utilization`, `system.filesystem.utilization`, `system.network.io`,
  `system.disk.io`, `system.cpu.load_average.1m`, `system.uptime`, `system.processes.count`.
- `container.*` — the containers of `docker-new` (`docker_stats`): cpu, memory usage and
  limit, network rx/tx, block I/O, uptime, restarts, pids, state.
- `proxmox_*` — the hypervisor: node (cpu, memory, uptime, iowait), each VM (cpu, memory,
  max memory, disk read/write, net in/out, max disk) and each datastore (total/used bytes).
- `synology.*` — the NAS: system status/temperature/fans, disks (status, temperature),
  volumes (total/free bytes), memory, cpu, network.
- `pihole_*` — DNS of the LAN, from the Pi-hole REST API: `pihole_status`,
  `pihole_dns_queries_all_types`, `pihole_ads_blocked_today`, `pihole_unique_clients`, ...
- `opnsense_*` — the firewall: interfaces and throughput, states, gateway, DHCP, certificates.
- `otelcol_*` — the counters of the collector itself
  (`otelcol_receiver_accepted_log_records`, `otelcol_exporter_sent_log_records`, ...),
  which is how the chain watches itself.

**Logs**:

- The journal of every guest, `pve` included, with `host.name` = the inventory name
  (`victoria`, `pihole`, `pve`, ...) and `level` derived from the journal priority;
  `service.instance.id` and `host.id` tag the record too.
- The stdout/stderr of the containers of `docker-new` (read from Docker's own log files, one
  receiver per container): `host.name` is the **container** name (`sonarr`, `sabnzbd`, ...)
  and `service.instance.id` is the guest `docker-new`.
- The firewall logs of `opnsense-lan` (filterlog, sshd, DHCP, HAProxy, Suricata), enriched
  from its API; only that address may push to the syslog port.
- The DSM logs of `synology`, pushed in RFC 5424.
- The journal of `otel-receiv` itself, so a failing collector is visible in the database.
- Not there: no traces at all — the only debug output kept is the one written into the
  journal of `otel-receiv`. Everything else arrives, the journal of `pve` among it.

## 3. Searching the logs

**The tool to use in ECA: `search_logs`** (from the `mcp-bash` MCP server). It queries
VictoriaLogs and answers the matching lines as `time | host | level | message`, newest
first, followed by the source line of the query.

- `query` (required) — the string to look for, matched case-insensitively.
- `host` (optional) — one or more guests, comma-separated; by default the whole fleet.
- `timeframe` (optional) — `30m`, `12h`, `1day`/`24h`, `7days`, `2weeks`; default `1day`.
- `limit` (optional) — maximum number of lines, default 100, clamped 1..1000.

Ask it things like:

- `query=OOM-killer, host=victoria, timeframe=7days` — was the VM ever out of memory?
- `query=No space left on device` — the whole fleet, in the default window.
- `query=error, host=sonarr, timeframe=12h, limit=200` — a container that misbehaves.
- `query=segfault` — any crash, anywhere, in the last day.

**By hand (LogsQL)** — for what the tool does not cover, POST a LogsQL query:

```shell
curl -s -X POST 'http://192.168.2.125:9428/select/logsql/query' \
  --data-urlencode 'query=host.name:"grafana" level:error' --data-urlencode 'limit=20'
```

- Fields to filter on: `host.name` (a guest or a container), `level`, `_time`, and the
  message body; `i("...")` is the case-insensitive phrase filter the tool builds.
- `_time:7d` bounds a window, and `start`/`end` (RFC 3339 UTC) bound it exactly — that is
  what the tool sends.
- Interactive UI: `http://192.168.2.125:9428/select/vmui/` — log explorer, live tail, charts.
- The log `vmalert` rules are browsed in the same UI, under `/select/vmalert/*`.

## 4. Metrics for performance / capacity analysis

**Where to look**: Grafana `http://192.168.2.30:3000`, whose dashboards are provisioned from
`git/ansible` — `/d/fleet-hosts` (the guests), `/d/docker-containers` (the containers),
`/d/proxmox-ve` (the hypervisor), `/d/synology-nas` (the NAS), `/d/Pi-hole-Exporter` (DNS)
and the two OPNsense ones. `/d/fleet-hosts` also draws `opnsense-lan`: it merges the
`opnsense_*` series that have a counterpart there (cpu, memory, filesystem, load, network
throughput, uptime) with the guests' `system.*` ones, under the same `host.name`, and its
`host` variable lists the firewall with them. Its `Disk throughput` and `Processes running`
panels have no `opnsense_*` counterpart and stay guests-only. For a one-off question, use
vmui: `http://192.168.2.125:8428/vmui/`.

Dotted names must be quoted when a client escapes dots — this fleet stores the dotted form,
so write `{"system.cpu.utilization"}`, not `system_cpu_utilization`.

One-liners, grouped by question:

- CPU of every guest: `1 - avg by (host.name) ({"system.cpu.utilization", "state"="idle"})`
- Memory pressure: `100 * max by (host.name) ({"system.memory.utilization", "state"="used"})`
- Those two families hold one sample per `state` (`idle`/`user`/... and `used`/`free`/...),
  so filtering `state` is what makes the lines above a utilization and not a mean of states.
- Fullest filesystem: `100 * max by (host.name) ({"system.filesystem.utilization"})`
- Network throughput, in bytes/s: `sum by (host.name) (rate({"system.network.io"}[5m]))`
- Load average: `max by (host.name) ({"system.cpu.load_average.1m"})`
- Reboot of a guest: `min by (host.name) ({"system.uptime"})` — a small value is a fresh boot
- Container memory in use: `{"container.memory.usage.total"}`, against its ceiling
  `max by (container.name) ({"container.memory.usage.limit"})`
- Containers that restart: `max by (container.name) ({"container.restarts"})`
- VM memory against its maximum: `100 * proxmox_vm_mem_bytes / proxmox_vm_maxmem_bytes`
- Datastore fill: `100 * proxmox_storage_used_bytes / proxmox_storage_total_bytes`
- Hypervisor CPU: `proxmox_node_cpustat_cpu_percent`
- NAS volume headroom: `{"synology.volume.free_bytes"}`
- NAS disk health: `{"synology.disk.temperature"}` and `{"synology.disk.status"}`
- Firewall performance, the same questions as the guests: CPU
  `1 - avg(rate(opnsense_cpu_seconds_total{mode="idle"}[5m]))`, memory
  `opnsense_system_memory_used_bytes / opnsense_system_memory_total_bytes`, root filesystem
  `{"opnsense_system_disk_usage_ratio", "mountpoint"="/"}`

Reading the answers:

- One sample per minute and 3 months of history: prefer `avg_over_time` / `max_over_time`
  (for example `max_over_time({"system.memory.utilization"}[7d])`) to raw samples.
- Group by `host.name` for the guests, `container.name` for the containers of `docker-new`,
  and the node/VM dimensions of the `proxmox_*` series; vmui's metric browser lists the
  exact label set of a family.
- Capacity numbers: `proxmox_storage_used_bytes`, `proxmox_vm_maxdisk_bytes`,
  `{"synology.volume.free_bytes"}` and `container.memory.usage.limit` say what is left,
  while the utilization series say how fast it is being consumed.
- The databases are sized in `ansible/playbook/catalog/victoria_stack.yaml` (retention,
  partition cap, cache) and the collection in `catalog/otel_collector.yaml`.
