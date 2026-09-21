"""
title: vCenter Multi-Environment Agent
author: ncwhite03
version: 0.1.0
license: MIT
description: Lets a tool-calling LLM (e.g. NVIDIA Nemotron) inspect, analyze, and deploy VMs across multiple vCenters.
requirements: pyvmomi,requests
"""

import json
import os
import ssl
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field
from pyVim.connect import Disconnect, SmartConnect
from pyVim.task import WaitForTask
from pyVmomi import vim

HIGH_UTILIZATION_PCT = 85
STALE_SNAPSHOT_DAYS = 3


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _ssl_context(verify_ssl: bool):
    if verify_ssl:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get_all_objs(content, vimtype):
    view = content.viewManager.CreateContainerView(content.rootFolder, vimtype, True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


def _find_by_name(content, vimtype, name):
    for obj in _get_all_objs(content, vimtype):
        if obj.name == name:
            return obj
    return None


def _size_gb(num_bytes):
    return None if num_bytes is None else round(num_bytes / (1024**3), 2)


def _parent_datacenter(entity):
    obj = entity
    while obj is not None and not isinstance(obj, vim.Datacenter):
        obj = getattr(obj, "parent", None)
    return obj


def _cluster_name_of_host(host):
    if host is not None and isinstance(host.parent, vim.ClusterComputeResource):
        return host.parent.name
    return None


def _flatten_snapshots(nodes):
    out = []
    for node in nodes:
        out.append(
            {
                "name": node.name,
                "created": node.createTime.isoformat() if node.createTime else None,
                "description": node.description,
            }
        )
        out.extend(_flatten_snapshots(node.childSnapshotList))
    return out


def _find_snapshot(vm, snapshot_name):
    def walk(nodes):
        for node in nodes:
            if node.name == snapshot_name:
                return node
            found = walk(node.childSnapshotList)
            if found:
                return found
        return None

    return walk(vm.snapshot.rootSnapshotList) if vm.snapshot else None


def _err(exc: Exception) -> str:
    return f"Error: {type(exc).__name__}: {getattr(exc, 'msg', None) or exc}"


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


class _Session:
    """Opens a vCenter session on enter and always logs out on exit."""

    def __init__(self, host, user, password, port, verify_ssl):
        self._args = dict(host=host, user=user, pwd=password, port=port, sslContext=_ssl_context(verify_ssl))
        self._si = None

    def __enter__(self):
        self._si = SmartConnect(**self._args)
        return self._si.RetrieveContent()

    def __exit__(self, *exc):
        if self._si:
            try:
                Disconnect(self._si)
            except Exception:
                pass
        return False


# ---------------------------------------------------------------------------
# Open WebUI Tool
# ---------------------------------------------------------------------------
class Tools:
    class Valves(BaseModel):
        VCENTERS: str = Field(
            default="{}",
            description=(
                "JSON object mapping a short name to connection info, e.g. "
                '{"prod": {"host": "vcenter-prod.corp.local", "user": "svc-agent@vsphere.local", '
                '"password": "...", "port": 443, "verify_ssl": false}}. '
                "If left as {} the VCENTERS_JSON environment variable is used instead."
            ),
        )
        VERIFY_SSL: bool = Field(
            default=False,
            description="Default TLS certificate verification (can be overridden per vCenter with verify_ssl).",
        )
        DEFAULT_VCENTER: str = Field(
            default="",
            description="vCenter name used when a call omits `vcenter`. Optional if only one is configured.",
        )
        MAX_LIST_RESULTS: int = Field(default=200, description="Safety cap on rows returned by list-style tools.")
        READ_ONLY: bool = Field(
            default=False,
            description="When true, every tool that changes state (power, snapshots, deploy, migrate, delete) is refused.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # -- configuration ------------------------------------------------------
    def _vcenters(self) -> dict:
        raw = self.valves.VCENTERS
        if not raw or raw.strip() == "{}":
            raw = os.environ.get("VCENTERS_JSON", "{}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"VCENTERS is not valid JSON: {e}")

    def _session(self, vcenter: str = ""):
        vcenters = self._vcenters()
        if not vcenters:
            raise ValueError("No vCenters configured. An admin must set the VCENTERS valve or VCENTERS_JSON env var.")
        name = vcenter or self.valves.DEFAULT_VCENTER
        if not name:
            if len(vcenters) != 1:
                raise ValueError(f"Specify `vcenter`. Configured: {', '.join(vcenters)}")
            name = next(iter(vcenters))
        if name not in vcenters:
            raise ValueError(f"Unknown vcenter '{name}'. Configured: {', '.join(vcenters)}")
        cfg = vcenters[name]
        return name, _Session(
            cfg["host"],
            cfg["user"],
            cfg["password"],
            cfg.get("port", 443),
            cfg.get("verify_ssl", self.valves.VERIFY_SSL),
        )

    def _blocked(self) -> Optional[str]:
        if self.valves.READ_ONLY:
            return "Error: this agent is in READ_ONLY mode; state-changing operations are disabled."
        return None

    # =======================================================================
    # Inventory
    # =======================================================================
    def list_vcenters(self) -> str:
        """List every vCenter environment this agent is configured to manage. Does not connect to any of them."""
        try:
            vcenters = self._vcenters()
        except ValueError as e:
            return f"Error: {e}"
        if not vcenters:
            return "No vCenters are configured yet."
        return _dump({n: {"host": c.get("host")} for n, c in vcenters.items()})

    def list_datacenters(self, vcenter: str = "") -> str:
        """List the datacenters in a vCenter.

        :param vcenter: short vCenter name (see list_vcenters). Uses the default if omitted.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                dcs = [dc.name for dc in _get_all_objs(content, [vim.Datacenter])]
            return _dump({"vcenter": name, "datacenters": dcs})
        except Exception as e:
            return _err(e)

    def list_clusters(self, vcenter: str = "", datacenter: str = "") -> str:
        """List compute clusters in a vCenter with host count and DRS/HA status.

        :param vcenter: short vCenter name.
        :param datacenter: optional datacenter name to filter by.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                rows = []
                for c in _get_all_objs(content, [vim.ClusterComputeResource]):
                    dc = _parent_datacenter(c)
                    if datacenter and (not dc or dc.name != datacenter):
                        continue
                    cfg = c.configuration
                    rows.append(
                        {
                            "name": c.name,
                            "datacenter": dc.name if dc else None,
                            "hosts": len(c.host),
                            "drs_enabled": bool(cfg.drsConfig and cfg.drsConfig.enabled),
                            "ha_enabled": bool(cfg.dasConfig and cfg.dasConfig.enabled),
                        }
                    )
            return _dump({"vcenter": name, "clusters": rows})
        except Exception as e:
            return _err(e)

    def list_hosts(self, vcenter: str = "", cluster: str = "") -> str:
        """List ESXi hosts with connection state, maintenance mode, and CPU/memory usage.

        :param vcenter: short vCenter name.
        :param cluster: optional cluster name to filter by.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                rows = []
                for h in _get_all_objs(content, [vim.HostSystem]):
                    c_name = _cluster_name_of_host(h)
                    if cluster and c_name != cluster:
                        continue
                    qs, hw = h.summary.quickStats, h.summary.hardware
                    rows.append(
                        {
                            "name": h.name,
                            "cluster": c_name,
                            "connection_state": str(h.runtime.connectionState),
                            "in_maintenance_mode": h.runtime.inMaintenanceMode,
                            "cpu_used_mhz": qs.overallCpuUsage,
                            "cpu_total_mhz": hw.cpuMhz * hw.numCpuCores,
                            "memory_used_gb": round((qs.overallMemoryUsage or 0) / 1024, 1),
                            "memory_total_gb": _size_gb(hw.memorySize),
                            "num_vms": len(h.vm),
                            "esxi_version": h.config.product.fullName if h.config else None,
                        }
                    )
                    if len(rows) >= self.valves.MAX_LIST_RESULTS:
                        break
            return _dump({"vcenter": name, "hosts": rows})
        except Exception as e:
            return _err(e)

    def list_vms(self, vcenter: str = "", folder: str = "", power_state: str = "") -> str:
        """List virtual machines (not templates) with power state, host, size, and IP.

        :param vcenter: short vCenter name.
        :param folder: optional VM folder name to filter by.
        :param power_state: optional filter: poweredOn, poweredOff, or suspended.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                rows = []
                for vm in _get_all_objs(content, [vim.VirtualMachine]):
                    if not vm.config or vm.config.template:
                        continue
                    if folder and (not vm.parent or vm.parent.name != folder):
                        continue
                    ps = str(vm.runtime.powerState)
                    if power_state and ps != power_state:
                        continue
                    rows.append(
                        {
                            "name": vm.name,
                            "power_state": ps,
                            "host": vm.runtime.host.name if vm.runtime.host else None,
                            "cpu": vm.config.hardware.numCPU,
                            "memory_gb": round(vm.config.hardware.memoryMB / 1024, 1),
                            "guest_os": vm.config.guestFullName,
                            "ip_address": vm.guest.ipAddress if vm.guest else None,
                        }
                    )
                    if len(rows) >= self.valves.MAX_LIST_RESULTS:
                        break
            return _dump({"vcenter": name, "count": len(rows), "vms": rows})
        except Exception as e:
            return _err(e)

    def search_vm(self, query: str, vcenter: str = "") -> str:
        """Find VMs by partial name or IP address. Searches every configured vCenter unless one is specified.

        :param query: full or partial VM name, or an IP address.
        :param vcenter: optional short vCenter name to restrict the search.
        """
        try:
            targets = [vcenter] if vcenter else list(self._vcenters())
        except ValueError as e:
            return f"Error: {e}"
        q = query.lower()
        matches = []
        for target in targets:
            try:
                name, sess = self._session(target)
                with sess as content:
                    for vm in _get_all_objs(content, [vim.VirtualMachine]):
                        ip = (vm.guest.ipAddress if vm.guest else None) or ""
                        if q in vm.name.lower() or q == ip:
                            matches.append(
                                {
                                    "vcenter": name,
                                    "name": vm.name,
                                    "power_state": str(vm.runtime.powerState),
                                    "host": vm.runtime.host.name if vm.runtime.host else None,
                                    "ip_address": ip or None,
                                }
                            )
            except Exception as e:
                matches.append({"vcenter": target, "error": _err(e)})
        return _dump({"query": query, "matches": matches})

    def get_vm_details(self, vm_name: str, vcenter: str = "") -> str:
        """Get full configuration, live usage, networking, storage, and snapshot info for one VM.

        :param vm_name: exact VM name.
        :param vcenter: short vCenter name.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                vm = _find_by_name(content, [vim.VirtualMachine], vm_name)
                if not vm:
                    return f"Error: VM '{vm_name}' not found in vCenter '{name}'."
                qs = vm.summary.quickStats
                nics = [
                    {
                        "label": d.deviceInfo.label,
                        "mac": getattr(d, "macAddress", None),
                        "network": getattr(d.backing, "deviceName", None),
                    }
                    for d in vm.config.hardware.device
                    if isinstance(d, vim.vm.device.VirtualEthernetCard)
                ]
                disks = [
                    {"label": d.deviceInfo.label, "size_gb": round(d.capacityInKB / 1024 / 1024, 1)}
                    for d in vm.config.hardware.device
                    if isinstance(d, vim.vm.device.VirtualDisk)
                ]
                return _dump(
                    {
                        "vcenter": name,
                        "name": vm.name,
                        "power_state": str(vm.runtime.powerState),
                        "host": vm.runtime.host.name if vm.runtime.host else None,
                        "cluster": _cluster_name_of_host(vm.runtime.host),
                        "cpu": vm.config.hardware.numCPU,
                        "memory_gb": round(vm.config.hardware.memoryMB / 1024, 1),
                        "cpu_usage_mhz": qs.overallCpuUsage,
                        "guest_memory_usage_gb": round((qs.guestMemoryUsage or 0) / 1024, 2),
                        "uptime_seconds": qs.uptimeSeconds,
                        "guest_os": vm.config.guestFullName,
                        "guest_hostname": vm.guest.hostName if vm.guest else None,
                        "ip_address": vm.guest.ipAddress if vm.guest else None,
                        "tools_status": str(vm.guest.toolsStatus) if vm.guest else None,
                        "hardware_version": vm.config.version,
                        "datastores": [ds.name for ds in vm.datastore],
                        "disks": disks,
                        "networks": nics,
                        "snapshots": _flatten_snapshots(vm.snapshot.rootSnapshotList) if vm.snapshot else [],
                        "annotation": vm.config.annotation,
                    }
                )
        except Exception as e:
            return _err(e)

    def list_templates(self, vcenter: str = "") -> str:
        """List VM templates available to deploy from.

        :param vcenter: short vCenter name.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                rows = [
                    {"name": vm.name, "guest_os": vm.config.guestFullName, "cpu": vm.config.hardware.numCPU,
                     "memory_gb": round(vm.config.hardware.memoryMB / 1024, 1)}
                    for vm in _get_all_objs(content, [vim.VirtualMachine])
                    if vm.config and vm.config.template
                ]
            return _dump({"vcenter": name, "templates": rows})
        except Exception as e:
            return _err(e)

    # =======================================================================
    # Insights
    # =======================================================================
    def get_cluster_insights(self, cluster: str, vcenter: str = "") -> str:
        """Summarize a cluster's capacity and utilization: total vs used CPU/memory, headroom, DRS/HA, host states.

        :param cluster: cluster name.
        :param vcenter: short vCenter name.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                c = _find_by_name(content, [vim.ClusterComputeResource], cluster)
                if not c:
                    return f"Error: cluster '{cluster}' not found in vCenter '{name}'."
                cpu_total = cpu_used = mem_total = mem_used = 0
                hosts = []
                for h in c.host:
                    hw, qs = h.summary.hardware, h.summary.quickStats
                    cpu_total += hw.cpuMhz * hw.numCpuCores
                    cpu_used += qs.overallCpuUsage or 0
                    mem_total += hw.memorySize / 1024**2
                    mem_used += qs.overallMemoryUsage or 0
                    hosts.append(
                        {
                            "name": h.name,
                            "connection_state": str(h.runtime.connectionState),
                            "in_maintenance_mode": h.runtime.inMaintenanceMode,
                        }
                    )
                cfg = c.configuration
                cpu_pct = round(cpu_used / cpu_total * 100, 1) if cpu_total else None
                mem_pct = round(mem_used / mem_total * 100, 1) if mem_total else None
                notes = []
                if cpu_pct and cpu_pct > HIGH_UTILIZATION_PCT:
                    notes.append("CPU utilization is high; little headroom for new VMs.")
                if mem_pct and mem_pct > HIGH_UTILIZATION_PCT:
                    notes.append("Memory utilization is high; little headroom for new VMs.")
                if not (cfg.dasConfig and cfg.dasConfig.enabled):
                    notes.append("vSphere HA is not enabled.")
                return _dump(
                    {
                        "vcenter": name,
                        "cluster": cluster,
                        "hosts": hosts,
                        "vm_count": len(c.resourcePool.vm) if c.resourcePool else 0,
                        "drs_enabled": bool(cfg.drsConfig and cfg.drsConfig.enabled),
                        "ha_enabled": bool(cfg.dasConfig and cfg.dasConfig.enabled),
                        "cpu_total_ghz": round(cpu_total / 1000, 1),
                        "cpu_used_ghz": round(cpu_used / 1000, 1),
                        "cpu_used_pct": cpu_pct,
                        "memory_total_gb": round(mem_total / 1024, 1),
                        "memory_used_gb": round(mem_used / 1024, 1),
                        "memory_used_pct": mem_pct,
                        "notes": notes,
                    }
                )
        except Exception as e:
            return _err(e)

    def get_datastore_report(self, vcenter: str = "", datastore: str = "") -> str:
        """Report datastore capacity, free space, and percent used, flagging datastores that are nearly full.

        :param vcenter: short vCenter name.
        :param datastore: optional single datastore name.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                rows = []
                for ds in _get_all_objs(content, [vim.Datastore]):
                    if datastore and ds.name != datastore:
                        continue
                    cap, free = ds.summary.capacity, ds.summary.freeSpace
                    used_pct = round((cap - free) / cap * 100, 1) if cap else None
                    rows.append(
                        {
                            "name": ds.name,
                            "type": ds.summary.type,
                            "capacity_gb": _size_gb(cap),
                            "free_gb": _size_gb(free),
                            "used_pct": used_pct,
                            "accessible": ds.summary.accessible,
                            "num_vms": len(ds.vm),
                            "warning": "nearly full" if used_pct and used_pct > HIGH_UTILIZATION_PCT else None,
                        }
                    )
            return _dump({"vcenter": name, "datastores": rows})
        except Exception as e:
            return _err(e)

    def get_alarms(self, vcenter: str = "") -> str:
        """List triggered alarms across hosts, VMs, clusters, and datastores.

        :param vcenter: short vCenter name.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                alarms = self._collect_alarms(content)
            return _dump({"vcenter": name, "count": len(alarms), "alarms": alarms})
        except Exception as e:
            return _err(e)

    @staticmethod
    def _collect_alarms(content):
        alarms = []
        for etype in (vim.HostSystem, vim.VirtualMachine, vim.ClusterComputeResource, vim.Datastore):
            for entity in _get_all_objs(content, [etype]):
                for st in entity.triggeredAlarmState or []:
                    alarms.append(
                        {
                            "entity": entity.name,
                            "type": etype.__name__,
                            "alarm": st.alarm.info.name if st.alarm else None,
                            "status": str(st.overallStatus),
                            "time": st.time.isoformat() if st.time else None,
                            "acknowledged": st.acknowledged,
                        }
                    )
        return alarms

    def health_report(self, vcenter: str = "") -> str:
        """One-shot health check: unhealthy hosts, nearly-full datastores, triggered alarms, stale snapshots,
        and VMs running without working VMware Tools.

        :param vcenter: short vCenter name.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                hosts = [
                    {"name": h.name, "connection_state": str(h.runtime.connectionState),
                     "in_maintenance_mode": h.runtime.inMaintenanceMode}
                    for h in _get_all_objs(content, [vim.HostSystem])
                    if str(h.runtime.connectionState) != "connected" or h.runtime.inMaintenanceMode
                ]
                datastores = []
                for ds in _get_all_objs(content, [vim.Datastore]):
                    cap, free = ds.summary.capacity, ds.summary.freeSpace
                    if cap and (cap - free) / cap * 100 > HIGH_UTILIZATION_PCT:
                        datastores.append({"name": ds.name, "used_pct": round((cap - free) / cap * 100, 1)})

                cutoff = datetime.now(timezone.utc).timestamp() - STALE_SNAPSHOT_DAYS * 86400
                stale_snapshots, no_tools = [], []
                for vm in _get_all_objs(content, [vim.VirtualMachine]):
                    if not vm.config or vm.config.template:
                        continue
                    if vm.snapshot:
                        for s in _flatten_snapshots(vm.snapshot.rootSnapshotList):
                            if s["created"] and datetime.fromisoformat(s["created"]).timestamp() < cutoff:
                                stale_snapshots.append({"vm": vm.name, **s})
                    if str(vm.runtime.powerState) == "poweredOn" and str(vm.guest.toolsStatus) in (
                        "toolsNotInstalled",
                        "toolsNotRunning",
                    ):
                        no_tools.append(vm.name)

                alarms = self._collect_alarms(content)
                return _dump(
                    {
                        "vcenter": name,
                        "summary": (
                            f"{len(hosts)} host issue(s), {len(datastores)} nearly-full datastore(s), "
                            f"{len(alarms)} alarm(s), {len(stale_snapshots)} snapshot(s) older than "
                            f"{STALE_SNAPSHOT_DAYS} days, {len(no_tools)} powered-on VM(s) without working VMware Tools."
                        ),
                        "hosts_needing_attention": hosts,
                        "nearly_full_datastores": datastores,
                        "alarms": alarms,
                        "stale_snapshots": stale_snapshots,
                        "vms_without_tools": no_tools,
                    }
                )
        except Exception as e:
            return _err(e)

    def compare_vcenters(self) -> str:
        """Compare all configured vCenters side by side: host, VM, and datastore counts plus CPU/memory utilization.
        Useful for deciding which environment has capacity for a new workload."""
        try:
            names = list(self._vcenters())
        except ValueError as e:
            return f"Error: {e}"
        rows = []
        for n in names:
            try:
                _, sess = self._session(n)
                with sess as content:
                    hosts = _get_all_objs(content, [vim.HostSystem])
                    vms = [v for v in _get_all_objs(content, [vim.VirtualMachine]) if v.config and not v.config.template]
                    ds = _get_all_objs(content, [vim.Datastore])
                    cpu_t = sum(h.summary.hardware.cpuMhz * h.summary.hardware.numCpuCores for h in hosts)
                    cpu_u = sum(h.summary.quickStats.overallCpuUsage or 0 for h in hosts)
                    mem_t = sum(h.summary.hardware.memorySize / 1024**2 for h in hosts)
                    mem_u = sum(h.summary.quickStats.overallMemoryUsage or 0 for h in hosts)
                    cap = sum(d.summary.capacity for d in ds)
                    free = sum(d.summary.freeSpace for d in ds)
                    rows.append(
                        {
                            "vcenter": n,
                            "hosts": len(hosts),
                            "vms": len(vms),
                            "vms_powered_on": sum(1 for v in vms if str(v.runtime.powerState) == "poweredOn"),
                            "cpu_used_pct": round(cpu_u / cpu_t * 100, 1) if cpu_t else None,
                            "memory_used_pct": round(mem_u / mem_t * 100, 1) if mem_t else None,
                            "storage_free_gb": _size_gb(free),
                            "storage_used_pct": round((cap - free) / cap * 100, 1) if cap else None,
                        }
                    )
            except Exception as e:
                rows.append({"vcenter": n, "error": _err(e)})
        return _dump({"comparison": rows})

    def find_idle_vms(self, vcenter: str = "", cpu_mhz_threshold: int = 50) -> str:
        """Find reclaim candidates: powered-off VMs plus powered-on VMs using almost no CPU.

        :param vcenter: short vCenter name.
        :param cpu_mhz_threshold: powered-on VMs using less CPU than this (MHz) are flagged as idle.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                off, idle = [], []
                for vm in _get_all_objs(content, [vim.VirtualMachine]):
                    if not vm.config or vm.config.template:
                        continue
                    alloc_gb = round(vm.summary.storage.committed / 1024**3, 1) if vm.summary.storage else None
                    if str(vm.runtime.powerState) == "poweredOff":
                        off.append({"name": vm.name, "disk_gb": alloc_gb})
                    elif str(vm.runtime.powerState) == "poweredOn":
                        used = vm.summary.quickStats.overallCpuUsage or 0
                        if used < cpu_mhz_threshold:
                            idle.append({"name": vm.name, "cpu_usage_mhz": used, "vcpus": vm.config.hardware.numCPU,
                                         "memory_gb": round(vm.config.hardware.memoryMB / 1024, 1)})
            return _dump({"vcenter": name, "powered_off": off, "idle_powered_on": idle})
        except Exception as e:
            return _err(e)

    def get_recent_tasks(self, vcenter: str = "", limit: int = 20) -> str:
        """Show the most recent vCenter tasks (deploys, power ops, migrations) with their outcome.

        :param vcenter: short vCenter name.
        :param limit: maximum number of tasks to return, newest first.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                epoch = datetime.min.replace(tzinfo=timezone.utc)
                tasks = sorted(content.taskManager.recentTask, key=lambda t: t.info.startTime or epoch, reverse=True)
                rows = [
                    {
                        "task": t.info.descriptionId,
                        "entity": t.info.entityName,
                        "state": str(t.info.state),
                        "started": t.info.startTime.isoformat() if t.info.startTime else None,
                        "error": t.info.error.localizedMessage if t.info.error else None,
                    }
                    for t in tasks[:limit]
                ]
            return _dump({"vcenter": name, "tasks": rows})
        except Exception as e:
            return _err(e)

    # =======================================================================
    # Power operations
    # =======================================================================
    def power_on_vm(self, vm_name: str, vcenter: str = "") -> str:
        """Power on a VM.

        :param vm_name: exact VM name.
        :param vcenter: short vCenter name.
        """
        return self._power(vm_name, vcenter, "on")

    def power_off_vm(self, vm_name: str, vcenter: str = "", force: bool = False) -> str:
        """Shut down a VM. Uses a graceful guest shutdown when VMware Tools is running, unless force is true.

        :param vm_name: exact VM name.
        :param vcenter: short vCenter name.
        :param force: hard power-off immediately instead of a graceful shutdown.
        """
        return self._power(vm_name, vcenter, "off", force)

    def restart_vm(self, vm_name: str, vcenter: str = "", force: bool = False) -> str:
        """Restart a VM. Uses a graceful guest reboot when VMware Tools is running, unless force is true.

        :param vm_name: exact VM name.
        :param vcenter: short vCenter name.
        :param force: hard reset immediately instead of a graceful reboot.
        """
        return self._power(vm_name, vcenter, "reset", force)

    def suspend_vm(self, vm_name: str, vcenter: str = "") -> str:
        """Suspend a running VM.

        :param vm_name: exact VM name.
        :param vcenter: short vCenter name.
        """
        return self._power(vm_name, vcenter, "suspend")

    def _power(self, vm_name, vcenter, op, force=False) -> str:
        if blocked := self._blocked():
            return blocked
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                vm = _find_by_name(content, [vim.VirtualMachine], vm_name)
                if not vm:
                    return f"Error: VM '{vm_name}' not found in vCenter '{name}'."
                tools_ok = bool(vm.guest) and str(vm.guest.toolsStatus) in ("toolsOk", "toolsOld")
                action = op
                if op == "on":
                    WaitForTask(vm.PowerOnVM_Task())
                elif op == "off":
                    if tools_ok and not force:
                        vm.ShutdownGuest()
                        action = "graceful shutdown requested (guest may take a moment to stop)"
                    else:
                        WaitForTask(vm.PowerOffVM_Task())
                elif op == "reset":
                    if tools_ok and not force:
                        vm.RebootGuest()
                        action = "graceful reboot requested"
                    else:
                        WaitForTask(vm.ResetVM_Task())
                elif op == "suspend":
                    WaitForTask(vm.SuspendVM_Task())
                return _dump({"vcenter": name, "vm": vm_name, "action": action, "power_state": str(vm.runtime.powerState)})
        except Exception as e:
            return _err(e)

    # =======================================================================
    # Snapshots
    # =======================================================================
    def list_snapshots(self, vm_name: str, vcenter: str = "") -> str:
        """List all snapshots on a VM.

        :param vm_name: exact VM name.
        :param vcenter: short vCenter name.
        """
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                vm = _find_by_name(content, [vim.VirtualMachine], vm_name)
                if not vm:
                    return f"Error: VM '{vm_name}' not found in vCenter '{name}'."
                snaps = _flatten_snapshots(vm.snapshot.rootSnapshotList) if vm.snapshot else []
            return _dump({"vcenter": name, "vm": vm_name, "snapshots": snaps})
        except Exception as e:
            return _err(e)

    def create_snapshot(self, vm_name: str, snapshot_name: str, vcenter: str = "", description: str = "",
                        include_memory: bool = False, quiesce: bool = False) -> str:
        """Create a snapshot of a VM.

        :param vm_name: exact VM name.
        :param snapshot_name: name for the snapshot.
        :param vcenter: short vCenter name.
        :param description: optional description.
        :param include_memory: also capture memory state.
        :param quiesce: quiesce the guest file system first (needs VMware Tools).
        """
        if blocked := self._blocked():
            return blocked
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                vm = _find_by_name(content, [vim.VirtualMachine], vm_name)
                if not vm:
                    return f"Error: VM '{vm_name}' not found in vCenter '{name}'."
                WaitForTask(vm.CreateSnapshot_Task(snapshot_name, description, include_memory, quiesce))
            return _dump({"vcenter": name, "vm": vm_name, "snapshot": snapshot_name, "status": "created"})
        except Exception as e:
            return _err(e)

    def revert_snapshot(self, vm_name: str, snapshot_name: str, vcenter: str = "", confirm: bool = False) -> str:
        """Revert a VM to a snapshot. Discards all changes made since; requires confirm=true.

        :param vm_name: exact VM name.
        :param snapshot_name: snapshot to revert to.
        :param vcenter: short vCenter name.
        :param confirm: must be true, and only after the user explicitly agreed to the revert.
        """
        if blocked := self._blocked():
            return blocked
        if not confirm:
            return (f"Refusing to revert '{vm_name}' to '{snapshot_name}': this discards current state. "
                    "Ask the user to confirm, then call again with confirm=true.")
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                vm = _find_by_name(content, [vim.VirtualMachine], vm_name)
                if not vm:
                    return f"Error: VM '{vm_name}' not found in vCenter '{name}'."
                node = _find_snapshot(vm, snapshot_name)
                if not node:
                    return f"Error: snapshot '{snapshot_name}' not found on '{vm_name}'."
                WaitForTask(node.snapshot.RevertToSnapshot_Task())
            return _dump({"vcenter": name, "vm": vm_name, "snapshot": snapshot_name, "status": "reverted"})
        except Exception as e:
            return _err(e)

    def delete_snapshot(self, vm_name: str, snapshot_name: str, vcenter: str = "", remove_children: bool = False,
                        confirm: bool = False) -> str:
        """Delete a snapshot (merges its changes into the parent disk). Requires confirm=true.

        :param vm_name: exact VM name.
        :param snapshot_name: snapshot to delete.
        :param vcenter: short vCenter name.
        :param remove_children: also delete child snapshots.
        :param confirm: must be true, and only after the user explicitly agreed.
        """
        if blocked := self._blocked():
            return blocked
        if not confirm:
            return (f"Refusing to delete snapshot '{snapshot_name}' on '{vm_name}' without confirmation. "
                    "Ask the user to confirm, then call again with confirm=true.")
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                vm = _find_by_name(content, [vim.VirtualMachine], vm_name)
                if not vm:
                    return f"Error: VM '{vm_name}' not found in vCenter '{name}'."
                node = _find_snapshot(vm, snapshot_name)
                if not node:
                    return f"Error: snapshot '{snapshot_name}' not found on '{vm_name}'."
                WaitForTask(node.snapshot.RemoveSnapshot_Task(removeChildren=remove_children))
            return _dump({"vcenter": name, "vm": vm_name, "snapshot": snapshot_name, "status": "deleted"})
        except Exception as e:
            return _err(e)

    # =======================================================================
    # Provisioning & lifecycle
    # =======================================================================
    def deploy_vm_from_template(
        self,
        template_name: str,
        new_vm_name: str,
        vcenter: str = "",
        cluster: str = "",
        datastore: str = "",
        folder: str = "",
        cpu: int = 0,
        memory_gb: int = 0,
        power_on: bool = True,
        hostname: str = "",
        ip_address: str = "",
        subnet_mask: str = "",
        gateway: str = "",
        dns_servers: str = "",
        domain: str = "",
    ) -> str:
        """Deploy a new VM by cloning a template. Optionally resize it and apply Linux guest customization
        (static IP, hostname). Omit ip_address to leave networking untouched (DHCP). Before calling, confirm the
        target vCenter, template, and sizing with the user; use get_cluster_insights / get_datastore_report to
        verify there is capacity.

        :param template_name: exact source template name (see list_templates).
        :param new_vm_name: name for the new VM.
        :param vcenter: short vCenter name.
        :param cluster: target cluster (defaults to the template's own).
        :param datastore: target datastore (defaults to the template's own).
        :param folder: target VM folder (defaults to the template's own).
        :param cpu: vCPU count override; 0 keeps the template value.
        :param memory_gb: memory override in GB; 0 keeps the template value.
        :param power_on: power the VM on after it is created.
        :param hostname: guest hostname; defaults to new_vm_name when customizing.
        :param ip_address: static IPv4 address; enables Linux guest customization.
        :param subnet_mask: subnet mask; required with ip_address.
        :param gateway: default gateway; required with ip_address.
        :param dns_servers: comma-separated DNS server IPs.
        :param domain: DNS domain for the guest.
        """
        if blocked := self._blocked():
            return blocked
        if ip_address and not (subnet_mask and gateway):
            return "Error: subnet_mask and gateway are required when ip_address is set."
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                template = _find_by_name(content, [vim.VirtualMachine], template_name)
                if not template:
                    return f"Error: template '{template_name}' not found in vCenter '{name}'."
                if _find_by_name(content, [vim.VirtualMachine], new_vm_name):
                    return f"Error: a VM named '{new_vm_name}' already exists in vCenter '{name}'."

                dest_folder = template.parent
                if folder:
                    dest_folder = _find_by_name(content, [vim.Folder], folder)
                    if not dest_folder:
                        return f"Error: folder '{folder}' not found."

                relocate = vim.vm.RelocateSpec()
                if cluster:
                    cl = _find_by_name(content, [vim.ClusterComputeResource], cluster)
                    if not cl:
                        return f"Error: cluster '{cluster}' not found."
                    relocate.pool = cl.resourcePool
                else:
                    relocate.pool = template.resourcePool
                if datastore:
                    ds = _find_by_name(content, [vim.Datastore], datastore)
                    if not ds:
                        return f"Error: datastore '{datastore}' not found."
                    relocate.datastore = ds

                spec = vim.vm.CloneSpec(location=relocate, powerOn=False)
                if cpu or memory_gb:
                    spec.config = vim.vm.ConfigSpec()
                    if cpu:
                        spec.config.numCPUs = cpu
                    if memory_gb:
                        spec.config.memoryMB = int(memory_gb * 1024)

                if ip_address:
                    global_ip = vim.vm.customization.GlobalIPSettings()
                    if dns_servers:
                        global_ip.dnsServerList = [s.strip() for s in dns_servers.split(",") if s.strip()]
                    spec.customization = vim.vm.customization.Specification(
                        identity=vim.vm.customization.LinuxPrep(
                            domain=domain or "local",
                            hostName=vim.vm.customization.FixedName(name=hostname or new_vm_name),
                        ),
                        globalIPSettings=global_ip,
                        nicSettingMap=[
                            vim.vm.customization.AdapterMapping(
                                adapter=vim.vm.customization.IPSettings(
                                    ip=vim.vm.customization.FixedIp(ipAddress=ip_address),
                                    subnetMask=subnet_mask,
                                    gateway=[gateway],
                                )
                            )
                        ],
                    )

                new_vm = WaitForTask(template.Clone(folder=dest_folder, name=new_vm_name, spec=spec))
                if power_on:
                    WaitForTask(new_vm.PowerOnVM_Task())

            return _dump(
                {
                    "vcenter": name,
                    "vm": new_vm_name,
                    "cloned_from": template_name,
                    "cluster": cluster or "(template default)",
                    "datastore": datastore or "(template default)",
                    "static_ip": ip_address or None,
                    "powered_on": power_on,
                    "status": "deployed",
                }
            )
        except Exception as e:
            return _err(e)

    def reconfigure_vm(self, vm_name: str, vcenter: str = "", cpu: int = 0, memory_gb: int = 0) -> str:
        """Change a VM's vCPU count and/or memory. Powered-on VMs need hot-add enabled; otherwise power off first.

        :param vm_name: exact VM name.
        :param vcenter: short vCenter name.
        :param cpu: new vCPU count; 0 leaves it unchanged.
        :param memory_gb: new memory in GB; 0 leaves it unchanged.
        """
        if blocked := self._blocked():
            return blocked
        if not cpu and not memory_gb:
            return "Error: specify cpu and/or memory_gb."
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                vm = _find_by_name(content, [vim.VirtualMachine], vm_name)
                if not vm:
                    return f"Error: VM '{vm_name}' not found in vCenter '{name}'."
                spec = vim.vm.ConfigSpec()
                if cpu:
                    spec.numCPUs = cpu
                if memory_gb:
                    spec.memoryMB = int(memory_gb * 1024)
                WaitForTask(vm.ReconfigVM_Task(spec))
            return _dump({"vcenter": name, "vm": vm_name, "cpu": cpu or None, "memory_gb": memory_gb or None,
                          "status": "reconfigured"})
        except Exception as e:
            return _err(e)

    def add_disk(self, vm_name: str, size_gb: int, vcenter: str = "", thin_provision: bool = True) -> str:
        """Add a new virtual disk to a VM.

        :param vm_name: exact VM name.
        :param size_gb: size of the new disk in GB.
        :param vcenter: short vCenter name.
        :param thin_provision: thin-provision the disk (default) instead of thick.
        """
        if blocked := self._blocked():
            return blocked
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                vm = _find_by_name(content, [vim.VirtualMachine], vm_name)
                if not vm:
                    return f"Error: VM '{vm_name}' not found in vCenter '{name}'."
                devices = vm.config.hardware.device
                controller = next((d for d in devices if isinstance(d, vim.vm.device.VirtualSCSIController)), None)
                if controller is None:
                    return f"Error: '{vm_name}' has no SCSI controller to attach a disk to."
                used_units = {d.unitNumber for d in devices
                              if isinstance(d, vim.vm.device.VirtualDisk) and d.controllerKey == controller.key}
                unit = next((u for u in range(16) if u != 7 and u not in used_units), None)
                if unit is None:
                    return f"Error: no free SCSI slots on '{vm_name}'."

                disk = vim.vm.device.VirtualDisk(
                    capacityInKB=size_gb * 1024 * 1024,
                    controllerKey=controller.key,
                    unitNumber=unit,
                    backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
                        diskMode="persistent", thinProvisioned=thin_provision
                    ),
                )
                change = vim.vm.device.VirtualDeviceSpec(
                    operation=vim.vm.device.VirtualDeviceSpec.Operation.add,
                    fileOperation=vim.vm.device.VirtualDeviceSpec.FileOperation.create,
                    device=disk,
                )
                WaitForTask(vm.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=[change])))
            return _dump({"vcenter": name, "vm": vm_name, "added_disk_gb": size_gb, "thin": thin_provision,
                          "status": "added"})
        except Exception as e:
            return _err(e)

    def migrate_vm(self, vm_name: str, vcenter: str = "", target_host: str = "", target_datastore: str = "") -> str:
        """Move a VM to another ESXi host (vMotion) and/or another datastore (Storage vMotion) within one vCenter.

        :param vm_name: exact VM name.
        :param vcenter: short vCenter name.
        :param target_host: destination ESXi host name.
        :param target_datastore: destination datastore name.
        """
        if blocked := self._blocked():
            return blocked
        if not target_host and not target_datastore:
            return "Error: specify target_host and/or target_datastore."
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                vm = _find_by_name(content, [vim.VirtualMachine], vm_name)
                if not vm:
                    return f"Error: VM '{vm_name}' not found in vCenter '{name}'."
                spec = vim.vm.RelocateSpec()
                if target_host:
                    host = _find_by_name(content, [vim.HostSystem], target_host)
                    if not host:
                        return f"Error: host '{target_host}' not found."
                    spec.host = host
                    spec.pool = host.parent.resourcePool
                if target_datastore:
                    ds = _find_by_name(content, [vim.Datastore], target_datastore)
                    if not ds:
                        return f"Error: datastore '{target_datastore}' not found."
                    spec.datastore = ds
                WaitForTask(vm.RelocateVM_Task(spec))
            return _dump({"vcenter": name, "vm": vm_name, "target_host": target_host or None,
                          "target_datastore": target_datastore or None, "status": "migrated"})
        except Exception as e:
            return _err(e)

    def delete_vm(self, vm_name: str, vcenter: str = "", confirm: bool = False) -> str:
        """Permanently delete a VM and its disks. Irreversible; requires confirm=true.

        :param vm_name: exact VM name.
        :param vcenter: short vCenter name.
        :param confirm: must be true, and only after the user explicitly agreed to delete this exact VM.
        """
        if blocked := self._blocked():
            return blocked
        if not confirm:
            return (f"Refusing to delete '{vm_name}' without confirmation. Ask the user to confirm the exact VM "
                    "name and vCenter, then call again with confirm=true.")
        try:
            name, sess = self._session(vcenter)
            with sess as content:
                vm = _find_by_name(content, [vim.VirtualMachine], vm_name)
                if not vm:
                    return f"Error: VM '{vm_name}' not found in vCenter '{name}'."
                if vm.config and vm.config.template:
                    return f"Error: '{vm_name}' is a template; refusing to delete templates via this tool."
                if str(vm.runtime.powerState) == "poweredOn":
                    WaitForTask(vm.PowerOffVM_Task())
                WaitForTask(vm.Destroy_Task())
            return _dump({"vcenter": name, "vm": vm_name, "status": "deleted"})
        except Exception as e:
            return _err(e)
