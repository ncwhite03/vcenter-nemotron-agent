# vCenter Nemotron Agent

Chat with your VMware estate. An [Open WebUI](https://github.com/open-webui/open-webui) **Tool** that gives an NVIDIA
**Nemotron** model function-calling access to one or many vCenters: inventory, health and capacity insights, power
operations, snapshots, and template-based VM deployment.

```
 You ──► Open WebUI ──► Nemotron (NVIDIA NIM or local Ollama)
             │                 │  tool calls
             └── Tool: tools/vcenter_tools.py ──► pyvmomi ──► vCenter A / B / C ...
```

Each call opens a short-lived vCenter session and always logs out, so nothing is left connected between requests.

## Capabilities

| Area | Tools |
|---|---|
| Inventory | `list_vcenters`, `list_datacenters`, `list_clusters`, `list_hosts`, `list_vms`, `list_templates`, `search_vm` (by name or IP, across all vCenters), `get_vm_details` |
| Insights | `health_report`, `get_cluster_insights`, `get_datastore_report`, `get_alarms`, `compare_vcenters`, `find_idle_vms`, `get_recent_tasks` |
| Power | `power_on_vm`, `power_off_vm`, `restart_vm`, `suspend_vm` (graceful via VMware Tools, `force` for hard) |
| Snapshots | `list_snapshots`, `create_snapshot`, `revert_snapshot`*, `delete_snapshot`* |
| Provisioning | `deploy_vm_from_template` (resize, folder/cluster/datastore, Linux static-IP customization), `reconfigure_vm`, `add_disk`, `migrate_vm` (vMotion / Storage vMotion) |
| Cleanup | `delete_vm`* |

\* Destructive: refuse to run unless `confirm=true`, which the model is instructed to set only after you agree.

## Quick start

1. **Get Nemotron.** Easiest: create a free API key at <https://build.nvidia.com> (hosted NIM, OpenAI-compatible).
   Prefer local? `docker compose --profile local up -d`, then `docker exec ollama ollama pull nemotron-mini`.
2. **Configure and launch Open WebUI**
   ```bash
   cp .env.example .env      # set WEBUI_SECRET_KEY and NVIDIA_NIM_API_KEY
   docker compose up -d      # http://localhost:3000
   ```
3. **Add the tool.** In Open WebUI: *Workspace → Tools → +* and paste the contents of
   [`tools/vcenter_tools.py`](tools/vcenter_tools.py) (Open WebUI installs `pyvmomi` automatically from the header).
4. **Configure vCenters.** Click the tool's gear icon (Valves) and set `VCENTERS`:
   ```json
   {
     "prod": {"host": "vcenter-prod.corp.local", "user": "svc-agent@vsphere.local", "password": "...", "verify_ssl": true},
     "lab":  {"host": "vcenter-lab.corp.local",  "user": "svc-agent@vsphere.local", "password": "..."}
   }
   ```
   Or leave it as `{}` and supply `VCENTERS_JSON` via `.env`.
5. **Enable native tool calling.** *Admin → Models →* your Nemotron model → Advanced Params → **Function Calling: Native**,
   and attach the tool to the model (or toggle it per chat). Use a Nemotron variant with tool-calling support
   (e.g. `nvidia/llama-3.3-nemotron-super-49b-v1`); very small models may call tools unreliably.

Try: *"Give me a health report for prod"*, *"Which vCenter has the most CPU headroom?"*,
*"Deploy ubuntu-22-template as web-07 on lab with 4 vCPU, 8 GB, IP 10.20.0.57/255.255.255.0 gw 10.20.0.1"*.
More in [docs/EXAMPLE_PROMPTS.md](docs/EXAMPLE_PROMPTS.md).

## Safety

- Create a **dedicated vCenter service account with a least-privilege role**. Give a read-only role for insight-only
  use; add VM create/power/snapshot privileges only where you want the agent to act.
- Set the **`READ_ONLY` valve** to disable every state-changing tool at the tool layer, regardless of what the model asks.
- Credentials in Valves are stored in Open WebUI's database and visible to Open WebUI admins. Restrict admin access,
  or use `VCENTERS_JSON` from a secret store.
- Set `verify_ssl: true` (per vCenter or globally) once your vCenters have trusted certificates.
- LLMs make mistakes: review the tool calls Open WebUI shows before trusting a deploy or delete.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Tests mock pyvmomi (config resolution, READ_ONLY, confirm guards, session cleanup, multi-vCenter search). The vSphere
calls themselves have **not** been exercised against a live vCenter in CI. Try in a lab first, and run
`get_vm_details` / `list_*` before any mutating call.

## Known limitations / ideas

- Guest customization supports Linux (LinuxPrep) only; Windows Sysprep, OVF/Content Library deploys, and vSphere
  tagging (REST API) are natural next additions.
- Performance data uses vCenter quick-stats (current values), not historical performance counters.
- Migration is within a single vCenter (no cross-vCenter vMotion).
