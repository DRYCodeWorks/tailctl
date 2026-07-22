"""Registry store tests: flock'd round-trip + concurrent-safe transaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from tailctl.registry import Forward, Holder, Instance, RegistryStore


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TAILCTL_HOME", str(tmp_path))
    return tmp_path


def test_empty_registry_reads_fresh(tmp_home: Path) -> None:
    reg = RegistryStore().read()
    assert reg.instances == {}
    assert reg.generation == 0


def test_transaction_round_trips_instance_with_forwards(tmp_home: Path) -> None:
    store = RegistryStore()
    with store.transaction() as reg:
        reg.instances["acme-dev"] = Instance(
            profile="acme-dev",
            pid=4242,
            create_time=123.5,
            socket="/x/sock",
            statedir="/x/state",
            socks_port=41100,
            http_port=41101,
            created_at="t",
            holders=[Holder(owner_pid=4242, owner_create_time=123.5, since="t")],
            forwards=[
                Forward(
                    service="clickhouse",
                    local_port=41102,
                    remote_host="100.64.0.10",
                    remote_port=9000,
                    pid=5252,
                    create_time=99.0,
                )
            ],
        )

    reloaded = RegistryStore().read()
    inst = reloaded.instances["acme-dev"]
    assert inst.pid == 4242
    assert inst.socks_port == 41100
    assert inst.forwards[0].service == "clickhouse"
    assert inst.forwards[0].local_port == 41102
    assert inst.refcount == 1  # one holder round-tripped
    assert inst.holders[0].owner_pid == 4242
    assert reloaded.generation == 1  # bumped on commit


def test_transaction_does_not_write_on_exception(tmp_home: Path) -> None:
    store = RegistryStore()
    with store.transaction() as reg:
        reg.instances["a"] = Instance(
            profile="a", pid=1, create_time=1.0, socket="s", statedir="d",
            socks_port=1, http_port=2,
        )
    with pytest.raises(RuntimeError):
        with store.transaction() as reg:
            reg.instances.clear()
            raise RuntimeError("boom")
    # The aborted transaction left the prior state intact.
    assert "a" in RegistryStore().read().instances
