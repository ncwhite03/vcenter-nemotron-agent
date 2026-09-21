# Example prompts

**Discovery**
- "What vCenters do you manage, and how do they compare on capacity?" (`list_vcenters`, `compare_vcenters`)
- "Find anything called `db` or with IP 10.20.0.57 across all vCenters." (`search_vm`)
- "Show details for web-03 on prod, including disks and snapshots." (`get_vm_details`)

**Insights**
- "Run a health report on prod and summarize what needs attention first." (`health_report`)
- "Do we have room in cluster Prod-A for 10 more VMs of 4 vCPU / 16 GB?" (`get_cluster_insights`, `get_datastore_report`)
- "Which VMs on lab look idle or are powered off and can be reclaimed?" (`find_idle_vms`)
- "What failed in the last 30 vCenter tasks?" (`get_recent_tasks`)

**Deployment**
- "List templates on lab." then "Deploy `rhel9-template` as `app-12` in cluster Lab-1, datastore ssd01, 4 vCPU, 8 GB."
- "Same, but with static IP 10.20.0.60/255.255.255.0, gateway 10.20.0.1, DNS 10.20.0.2." (`deploy_vm_from_template`)

**Operations**
- "Snapshot app-12 as `pre-patch` (no memory), then reboot it." (`create_snapshot`, `restart_vm`)
- "Grow app-12 to 8 vCPU and add a 200 GB thin disk." (`reconfigure_vm`, `add_disk`)
- "Move app-12 to host esx03." (`migrate_vm`)
- "Delete snapshots older than 3 days on prod." (the model lists stale snapshots via `health_report`, asks you to confirm, then calls `delete_snapshot`)
