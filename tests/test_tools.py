import importlib.util
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

spec = importlib.util.spec_from_file_location("vcenter_tools", Path(__file__).parent.parent / "tools" / "vcenter_tools.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CFG = {
    "prod": {"host": "vc-prod", "user": "u", "password": "p"},
    "lab": {"host": "vc-lab", "user": "u", "password": "p", "verify_ssl": True},
}


@pytest.fixture
def tools():
    t = mod.Tools()
    t.valves.VCENTERS = json.dumps(CFG)
    return t


def test_every_public_method_has_typed_params_and_docstring(tools):
    for name, fn in inspect.getmembers(tools, inspect.ismethod):
        if name.startswith("_"):
            continue
        assert fn.__doc__, f"{name} lacks a docstring (LLM tool description)"
        for p in inspect.signature(fn).parameters.values():
            assert p.annotation is not inspect.Parameter.empty, f"{name}.{p.name} untyped"


def test_list_vcenters_hides_credentials(tools):
    out = tools.list_vcenters()
    assert "vc-prod" in out and "password" not in out


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("VCENTERS_JSON", json.dumps(CFG))
    assert set(mod.Tools()._vcenters()) == {"prod", "lab"}


def test_bad_json_reports_error(tools):
    tools.valves.VCENTERS = "{nope"
    assert tools.list_vcenters().startswith("Error:")


def test_resolution_rules(tools):
    with pytest.raises(ValueError, match="Specify"):
        tools._session("")
    with pytest.raises(ValueError, match="Unknown"):
        tools._session("nope")
    tools.valves.DEFAULT_VCENTER = "lab"
    assert tools._session("")[0] == "lab"
    tools.valves.VCENTERS = json.dumps({"only": CFG["prod"]})
    tools.valves.DEFAULT_VCENTER = ""
    assert tools._session("")[0] == "only"


def test_no_config_is_clear_error(monkeypatch):
    monkeypatch.delenv("VCENTERS_JSON", raising=False)
    assert "No vCenters configured" in mod.Tools().list_datacenters()


@pytest.mark.parametrize("call", [
    lambda t: t.power_on_vm("a", "prod"),
    lambda t: t.power_off_vm("a", "prod"),
    lambda t: t.create_snapshot("a", "s", "prod"),
    lambda t: t.deploy_vm_from_template("t", "n", "prod"),
    lambda t: t.reconfigure_vm("a", "prod", cpu=2),
    lambda t: t.add_disk("a", 10, "prod"),
    lambda t: t.migrate_vm("a", "prod", target_host="h"),
    lambda t: t.delete_vm("a", "prod", confirm=True),
    lambda t: t.revert_snapshot("a", "s", "prod", confirm=True),
    lambda t: t.delete_snapshot("a", "s", "prod", confirm=True),
])
def test_read_only_blocks_mutations_without_connecting(tools, call):
    tools.valves.READ_ONLY = True
    with patch.object(mod, "SmartConnect") as sc:
        assert "READ_ONLY" in call(tools)
        sc.assert_not_called()


@pytest.mark.parametrize("call", [
    lambda t: t.delete_vm("a", "prod"),
    lambda t: t.revert_snapshot("a", "s", "prod"),
    lambda t: t.delete_snapshot("a", "s", "prod"),
])
def test_destructive_ops_require_confirm(tools, call):
    with patch.object(mod, "SmartConnect") as sc:
        assert "confirm" in call(tools).lower()
        sc.assert_not_called()


def test_deploy_validates_static_ip_args(tools):
    assert "subnet_mask and gateway" in tools.deploy_vm_from_template("t", "n", "prod", ip_address="10.0.0.5")


def test_migrate_and_reconfigure_validate_args(tools):
    assert "specify" in tools.migrate_vm("a", "prod").lower()
    assert "specify" in tools.reconfigure_vm("a", "prod").lower()


def test_connection_failure_is_returned_not_raised(tools):
    with patch.object(mod, "SmartConnect", side_effect=OSError("unreachable")):
        assert tools.list_datacenters("prod").startswith("Error: OSError")


def test_session_always_disconnects(tools):
    with patch.object(mod, "SmartConnect") as sc, patch.object(mod, "Disconnect") as dc, \
         patch.object(mod, "_get_all_objs", return_value=[SimpleNamespace(name="DC1")]):
        out = json.loads(tools.list_datacenters("prod"))
    assert out["datacenters"] == ["DC1"]
    dc.assert_called_once_with(sc.return_value)


def test_verify_ssl_per_vcenter_override(tools):
    with patch.object(mod, "SmartConnect") as sc, patch.object(mod, "Disconnect"), \
         patch.object(mod, "_get_all_objs", return_value=[]):
        tools.list_datacenters("prod")
        tools.list_datacenters("lab")
    assert sc.call_args_list[0].kwargs["sslContext"] is not None  # prod: default no-verify context
    assert sc.call_args_list[1].kwargs["sslContext"] is None      # lab: verify_ssl=True


def _snap(name, children=()):
    return SimpleNamespace(name=name, createTime=None, description="", childSnapshotList=list(children), snapshot=name)


def test_snapshot_tree_helpers():
    tree = [_snap("a", [_snap("b", [_snap("c")])]), _snap("d")]
    assert [s["name"] for s in mod._flatten_snapshots(tree)] == ["a", "b", "c", "d"]
    vm = SimpleNamespace(snapshot=SimpleNamespace(rootSnapshotList=tree))
    assert mod._find_snapshot(vm, "c").name == "c"
    assert mod._find_snapshot(vm, "zzz") is None
    assert mod._find_snapshot(SimpleNamespace(snapshot=None), "a") is None


def test_search_vm_spans_vcenters_and_isolates_failures(tools):
    vm = SimpleNamespace(name="web-01", guest=SimpleNamespace(ipAddress="10.1.1.1"),
                         runtime=SimpleNamespace(powerState="poweredOn", host=SimpleNamespace(name="esx1")))

    def fake_connect(**kw):
        if kw["host"] == "vc-lab":
            raise OSError("down")
        return MagicMock()

    with patch.object(mod, "SmartConnect", side_effect=fake_connect), patch.object(mod, "Disconnect"), \
         patch.object(mod, "_get_all_objs", return_value=[vm]):
        res = json.loads(tools.search_vm("WEB"))["matches"]
    assert res[0]["vcenter"] == "prod" and res[0]["name"] == "web-01"
    assert res[1]["vcenter"] == "lab" and "error" in res[1]
