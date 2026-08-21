from pathlib import Path
from unittest.mock import MagicMock

from sandbox_service.backend import ContainerSpec, DockerBackend


def test_create_mounts_session_root_and_uses_workspace_cwd(tmp_path: Path):
    session = tmp_path / "s1"
    workspace = session / "workspace"
    containers = MagicMock()
    containers.create.return_value.short_id = "cid"
    backend = DockerBackend(client=MagicMock(containers=containers))

    backend.create(ContainerSpec(sandbox_id="s1", image="agent", workspace_path=workspace))

    kwargs = containers.create.call_args.kwargs
    assert kwargs["volumes"] == {str(session.resolve()): {"bind": "/session", "mode": "rw"}}
    assert kwargs["working_dir"] == "/session/workspace"


def test_create_applies_elastic_resource_limits(tmp_path: Path):
    """max -> CFS quota + mem_limit（禁 swap）；min -> cpu_shares 权重 + mem_reservation。"""
    containers = MagicMock()
    containers.create.return_value.short_id = "cid"
    backend = DockerBackend(client=MagicMock(containers=containers))

    backend.create(ContainerSpec(
        sandbox_id="s1", image="agent",
        workspace_path=tmp_path / "s1" / "workspace",
    ))

    kwargs = containers.create.call_args.kwargs
    assert kwargs["nano_cpus"] == 2_000_000_000      # 硬顶 2 核
    assert kwargs["cpu_shares"] == 256               # 软底 0.25 核权重
    assert kwargs["mem_limit"] == "2048m"            # 硬顶 2G
    assert kwargs["memswap_limit"] == "2048m"        # 禁 swap
    assert kwargs["mem_reservation"] == "256m"       # 软底 256m


def test_create_clamps_min_below_max(tmp_path: Path):
    """min > max 时 clamp 到 max，防 mem_reservation > mem_limit 被 daemon 拒。"""
    containers = MagicMock()
    containers.create.return_value.short_id = "cid"
    backend = DockerBackend(client=MagicMock(containers=containers))

    backend.create(ContainerSpec(
        sandbox_id="s1", image="agent",
        workspace_path=tmp_path / "s1" / "workspace",
        cpu_max=0.5, cpu_min=2.0, mem_max_mb=1024, mem_min_mb=4096,
    ))

    kwargs = containers.create.call_args.kwargs
    assert kwargs["cpu_shares"] == 512               # min(2.0, 0.5) -> 0.5 核权重
    assert kwargs["mem_reservation"] == "1024m"
