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
