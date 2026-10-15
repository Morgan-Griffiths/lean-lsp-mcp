from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from lean_lsp_mcp import client_utils, config
from lean_lsp_mcp.client_utils import (
    attach_shared_client,
    bind_lean_project_path,
    close_shared_client,
    get_path_policy,
    resolve_file_path,
    set_build_in_progress,
    setup_client_for_file,
    startup_client,
    valid_lean_project_path,
)


class _FakeTransport:
    def __init__(self) -> None:
        self.kill_calls = 0

    def _kill_group(self) -> None:
        self.kill_calls += 1


class _MockAioClient:
    def __init__(self, project_path: Path) -> None:
        self.project_path = str(project_path)
        self.closed = False
        self._alive = True
        self._transport = _FakeTransport()

    @property
    def alive(self) -> bool:
        return self._alive and not self.closed

    async def close(self) -> None:
        self.closed = True


class _FailingCloseClient(_MockAioClient):
    def __init__(self, project_path: Path) -> None:
        super().__init__(project_path)
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        raise PermissionError("operation not permitted")


class _LifespanContext:
    def __init__(
        self,
        lean_project_path: Path | None,
        client: _MockAioClient | None,
        *,
        active_transport: str = "stdio",
        project_switching_allowed: bool = True,
    ) -> None:
        self.lean_project_path = lean_project_path
        self.client = client
        self.active_transport = active_transport
        self.project_switching_allowed = project_switching_allowed
        self.file_content_hashes: dict[str, int] = {}
        self.project_cache: dict[str, Path | str] = {}
        self.external_module_root: Path | None = None
        self.external_extra_roots: tuple[Path, ...] = ()
        self.external_roots_configured = False


class _RequestContext:
    def __init__(self, lifespan_context: _LifespanContext) -> None:
        self.lifespan_context = lifespan_context


class _Context:
    def __init__(self, lifespan_context: _LifespanContext) -> None:
        self.request_context = _RequestContext(lifespan_context)


def _make_project(root: Path) -> Path:
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.24.0\n")
    (root / "lakefile.toml").write_text('name = "test"\n')
    return root


def _make_dependency(project: Path, dep_root: Path) -> Path:
    dep_file = dep_root / "Mathlib" / "Foo.lean"
    dep_file.parent.mkdir(parents=True)
    dep_file.write_text("theorem dep : True := by trivial\n")

    dep_link = project / ".lake" / "packages" / "mathlib"
    dep_link.parent.mkdir(parents=True)
    dep_link.symlink_to(dep_root, target_is_directory=True)
    return dep_file


@pytest.fixture(autouse=True)
def _reset_shared_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        config.MODULE_ROOT_ENV,
        config.EXTRA_ROOTS_ENV,
        config.SERVER_OPTIONS_ENV,
        "LEAN_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    client_utils._shared_clients.clear()
    client_utils._shared_client_commands.clear()
    client_utils._shared_pools.clear()
    client_utils._builds_in_progress.clear()
    yield
    client_utils._shared_clients.clear()
    client_utils._shared_client_commands.clear()
    client_utils._shared_pools.clear()
    client_utils._builds_in_progress.clear()


@pytest.fixture
def patched_clients(monkeypatch: pytest.MonkeyPatch) -> list[_MockAioClient]:
    created: list[_MockAioClient] = []

    async def _constructor(
        project_path: Path, server_command: tuple[str, ...] = ("lake", "serve", "--")
    ) -> _MockAioClient:
        client = _MockAioClient(project_path)
        client.server_command = server_command
        created.append(client)
        return client

    monkeypatch.setattr(client_utils, "_start_client", _constructor)
    return created


@pytest.mark.asyncio
async def test_startup_client_reuses_existing(
    tmp_path: Path, patched_clients: list[_MockAioClient]
) -> None:
    project = _make_project(tmp_path / "proj")
    ctx = _Context(_LifespanContext(project, None))

    await startup_client(ctx)
    first = ctx.request_context.lifespan_context.client
    assert isinstance(first, _MockAioClient)
    assert not first.closed
    await startup_client(ctx)
    assert ctx.request_context.lifespan_context.client is first
    assert len(patched_clients) == 1


@pytest.mark.asyncio
async def test_startup_client_restarts_when_module_root_changes(
    tmp_path: Path, patched_clients: list[_MockAioClient]
) -> None:
    project = _make_project(tmp_path / "proj")
    module_root = tmp_path / "theory"
    module_root.mkdir()
    ctx = _Context(_LifespanContext(project, None))

    await startup_client(ctx)
    first = ctx.request_context.lifespan_context.client
    ctx.request_context.lifespan_context.external_module_root = module_root
    ctx.request_context.lifespan_context.external_roots_configured = True
    await startup_client(ctx)

    assert first is not None
    assert first.closed
    assert ctx.request_context.lifespan_context.client is not first
    assert len(patched_clients) == 2


def test_path_policy_accepts_session_external_root(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    external_root = tmp_path / "theory"
    external_file = external_root / "Support" / "Theorem.lean"
    external_file.parent.mkdir(parents=True)
    external_file.write_text("theorem external : True := by trivial\n")
    lifespan = _LifespanContext(project, None)
    lifespan.external_module_root = external_root
    lifespan.external_roots_configured = True
    ctx = _Context(lifespan)

    policy = get_path_policy(ctx)

    assert policy.contains(external_file)
    assert policy.client_relative_path(external_file) == external_file.as_posix()


@pytest.mark.asyncio
async def test_startup_client_keeps_project_specific_clients(
    tmp_path: Path, patched_clients: list[_MockAioClient]
) -> None:
    project1 = _make_project(tmp_path / "proj1")
    project2 = _make_project(tmp_path / "proj2")
    ctx = _Context(_LifespanContext(project1, None))

    await startup_client(ctx)
    first = ctx.request_context.lifespan_context.client

    ctx.request_context.lifespan_context.lean_project_path = project2
    await startup_client(ctx)
    second = ctx.request_context.lifespan_context.client

    ctx.request_context.lifespan_context.lean_project_path = project1
    await startup_client(ctx)

    assert first is not None
    assert second is not None
    assert first is not second
    assert ctx.request_context.lifespan_context.client is first
    assert not first.closed
    assert not second.closed
    assert len(patched_clients) == 2


@pytest.mark.asyncio
async def test_startup_client_recreates_dead_shared_client(
    tmp_path: Path, patched_clients: list[_MockAioClient]
) -> None:
    project = _make_project(tmp_path / "proj")
    ctx = _Context(_LifespanContext(project, None))

    await startup_client(ctx)
    first = ctx.request_context.lifespan_context.client
    first._alive = False

    await startup_client(ctx)

    second = ctx.request_context.lifespan_context.client
    assert second is not first
    assert first.closed
    assert len(patched_clients) == 2


@pytest.mark.asyncio
async def test_startup_client_recreates_dead_client_even_if_close_fails(
    tmp_path: Path, patched_clients: list[_MockAioClient]
) -> None:
    project = _make_project(tmp_path / "proj")
    old_client = _FailingCloseClient(project)
    old_client._alive = False

    attach_shared_client(project, old_client)

    ctx = _Context(_LifespanContext(project, old_client))
    await startup_client(ctx)

    new_client = ctx.request_context.lifespan_context.client
    assert isinstance(new_client, _MockAioClient)
    assert new_client is not old_client
    assert Path(new_client.project_path) == project.resolve()
    assert old_client.close_calls == 1
    assert len(patched_clients) == 1


def test_valid_lean_project_path_requires_lakefile(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.24.0\n")

    assert not valid_lean_project_path(project)
    assert not valid_lean_project_path(project / "missing")

    (project / "lakefile.lean").write_text("import Lake\n")
    assert valid_lean_project_path(project)


@pytest.mark.asyncio
async def test_setup_client_for_file_discovers_project(
    tmp_path: Path, patched_clients: list[_MockAioClient]
) -> None:
    project = _make_project(tmp_path / "proj")
    lean_file = project / "src" / "Example.lean"
    lean_file.parent.mkdir(parents=True)
    lean_file.write_text("example")

    ctx = _Context(_LifespanContext(None, None))

    rel_path = await setup_client_for_file(ctx, str(lean_file))
    assert rel_path == "src/Example.lean"
    assert (
        Path(ctx.request_context.lifespan_context.client.project_path)
        == project.resolve()
    )
    assert len(patched_clients) == 1


@pytest.mark.asyncio
async def test_setup_client_for_file_reuses_client_for_same_project(
    tmp_path: Path, patched_clients: list[_MockAioClient]
) -> None:
    project = _make_project(tmp_path / "proj")
    file1 = project / "File1.lean"
    file1.write_text("theorem a : True := by trivial")
    file2 = project / "src" / "File2.lean"
    file2.parent.mkdir(parents=True)
    file2.write_text("theorem b : True := by trivial")

    ctx = _Context(_LifespanContext(None, None))

    rel_path1 = await setup_client_for_file(ctx, str(file1))
    assert rel_path1 == "File1.lean"
    first_client = ctx.request_context.lifespan_context.client
    assert len(patched_clients) == 1

    rel_path2 = await setup_client_for_file(ctx, str(file2))
    assert rel_path2 == "src/File2.lean"
    assert ctx.request_context.lifespan_context.client is first_client
    assert not first_client.closed
    assert len(patched_clients) == 1


@pytest.mark.asyncio
async def test_setup_client_for_file_switches_projects_without_closing_cache(
    tmp_path: Path, patched_clients: list[_MockAioClient]
) -> None:
    project1 = _make_project(tmp_path / "proj1")
    file1 = project1 / "File1.lean"
    file1.write_text("theorem a : True := by trivial")

    project2 = _make_project(tmp_path / "proj2")
    file2 = project2 / "File2.lean"
    file2.write_text("theorem b : True := by trivial")

    ctx = _Context(_LifespanContext(None, None))

    assert await setup_client_for_file(ctx, str(file1)) == "File1.lean"
    first_client = ctx.request_context.lifespan_context.client

    assert await setup_client_for_file(ctx, str(file2)) == "File2.lean"
    second_client = ctx.request_context.lifespan_context.client

    assert first_client is not second_client
    assert not first_client.closed
    assert not second_client.closed
    assert len(patched_clients) == 2
    assert ctx.request_context.lifespan_context.lean_project_path == project2


@pytest.mark.asyncio
async def test_setup_client_for_dependency_file_uses_parent_project(
    tmp_path: Path, patched_clients: list[_MockAioClient]
) -> None:
    project = _make_project(tmp_path / "proj")
    try:
        dep_file = _make_dependency(project, tmp_path / "deps" / "mathlib")
    except OSError as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks unavailable: {exc}")

    ctx = _Context(_LifespanContext(project, None))

    rel_path = await setup_client_for_file(ctx, str(dep_file))

    assert rel_path == os.path.relpath(dep_file, project)
    assert ctx.request_context.lifespan_context.lean_project_path == project
    assert (
        Path(ctx.request_context.lifespan_context.client.project_path)
        == project.resolve()
    )
    assert len(patched_clients) == 1


def test_bind_lean_project_path_rejects_switch_on_remote_transport(
    tmp_path: Path,
) -> None:
    project1 = _make_project(tmp_path / "proj1")
    project2 = _make_project(tmp_path / "proj2")
    ctx = _Context(
        _LifespanContext(
            project1,
            None,
            active_transport="streamable-http",
            project_switching_allowed=False,
        )
    )

    with pytest.raises(ValueError, match="Project switching is disabled"):
        bind_lean_project_path(ctx, project2)


@pytest.mark.asyncio
async def test_startup_client_serializes_concurrent_calls(
    tmp_path: Path, patched_clients: list[_MockAioClient]
) -> None:
    project = _make_project(tmp_path / "proj")
    ctx = _Context(_LifespanContext(project, None))

    await asyncio.gather(*(startup_client(ctx) for _ in range(10)))

    assert len(patched_clients) == 1
    assert ctx.request_context.lifespan_context.client is patched_clients[0]


@pytest.mark.asyncio
async def test_build_in_progress_blocks_only_same_project(
    tmp_path: Path, patched_clients: list[_MockAioClient]
) -> None:
    project1 = _make_project(tmp_path / "proj1")
    project2 = _make_project(tmp_path / "proj2")

    set_build_in_progress(project1, True)

    try:
        with pytest.raises(ValueError, match="build is in progress"):
            await startup_client(_Context(_LifespanContext(project1, None)))

        other_ctx = _Context(_LifespanContext(project2, None))
        await startup_client(other_ctx)

        assert other_ctx.request_context.lifespan_context.client is patched_clients[0]
        assert len(patched_clients) == 1
    finally:
        set_build_in_progress(project1, False)


def test_close_shared_client_kills_process_group(tmp_path: Path) -> None:
    """close_shared_client() is the post-event-loop exit path: it must
    terminate synchronously via the transport's process-group kill."""
    project = _make_project(tmp_path / "proj")
    client = _MockAioClient(project)
    attach_shared_client(project, client)

    close_shared_client()

    assert client._transport.kill_calls == 1
    assert client_utils._shared_clients == {}


def test_close_shared_client_suppresses_error(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    client = _MockAioClient(project)

    def _boom() -> None:
        raise PermissionError("operation not permitted")

    client._transport._kill_group = _boom
    attach_shared_client(project, client)

    close_shared_client()  # should not raise
    assert client_utils._shared_clients == {}


def test_resolve_file_path_uses_project_root_for_relative(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    target = project / "src" / "Example.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem t : True := by trivial")

    ctx = _Context(_LifespanContext(project, None))
    resolved = resolve_file_path(ctx, "src/Example.lean")
    assert resolved == target.resolve()


@pytest.mark.asyncio
async def test_start_client_passes_command_to_async_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = MagicMock(start=AsyncMock())
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr(client_utils, "AsyncLeanLSPClient", constructor)
    command = ("lake", "serve", "--", "-DautoImplicit=false")

    assert await client_utils._start_client(tmp_path, command) is client

    constructor.assert_called_once_with(
        str(tmp_path), max_workers=config.max_open_files(), server_command=list(command)
    )
    client.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_server_option_change_restarts_client_but_order_does_not(
    tmp_path: Path,
    patched_clients: list[_MockAioClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path / "proj")
    ctx = _Context(_LifespanContext(project, None))
    monkeypatch.setenv(
        config.SERVER_OPTIONS_ENV, '{"autoImplicit":false,"maxHeartbeats":400000}'
    )
    first = await startup_client(ctx)
    assert first.server_command == (
        "lake",
        "serve",
        "--",
        "-DautoImplicit=false",
        "-DmaxHeartbeats=400000",
    )

    monkeypatch.setenv(
        config.SERVER_OPTIONS_ENV, '{"maxHeartbeats":400000,"autoImplicit":false}'
    )
    assert await startup_client(ctx) is first
    monkeypatch.setenv(config.SERVER_OPTIONS_ENV, '{"autoImplicit":true}')
    second = await startup_client(ctx)

    assert first.closed
    assert second is not first
    assert second.server_command[-1] == "-DautoImplicit=true"
    assert len(patched_clients) == 2


def test_string_server_option_is_one_unquoted_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(config.SERVER_OPTIONS_ENV, '{"output":"a file"}')
    assert client_utils._effective_server_command(None) == (
        "lake",
        "serve",
        "--",
        "-Doutput=a file",
    )


@pytest.mark.asyncio
async def test_extra_roots_and_inherited_lean_path_invalidate_client(
    tmp_path: Path,
    patched_clients: list[_MockAioClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path / "proj")
    generated = tmp_path / "generated"
    generated.mkdir()
    lifespan = _LifespanContext(project, None)
    lifespan.external_roots_configured = True
    ctx = _Context(lifespan)
    first = await startup_client(ctx)

    lifespan.external_extra_roots = (generated,)
    second = await startup_client(ctx)
    assert second is not first
    assert first.closed
    assert second.server_command[3] == str(generated)
    assert await startup_client(ctx) is second

    monkeypatch.setenv("LEAN_PATH", "inherited")
    third = await startup_client(ctx)
    assert second.closed
    assert third.server_command[3] == f"{generated}{os.pathsep}inherited"
    assert len(patched_clients) == 3


def test_external_root_environment_is_only_changed_in_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_root = tmp_path / "theory 'quoted'"
    module_root.mkdir()
    generated = tmp_path / "generated"
    generated.mkdir()
    monkeypatch.setenv(config.MODULE_ROOT_ENV, str(module_root))
    monkeypatch.setenv(config.EXTRA_ROOTS_ENV, json.dumps([str(generated)]))
    monkeypatch.setenv("LEAN_PATH", "inherited")
    command = client_utils._effective_server_command(None)
    assert command[4:] == ("lake", "serve", "--", f"--root={module_root}")

    # Exercise the environment wrapper without starting Lake or Lean.
    child = subprocess.run(
        [
            *command[:4],
            sys.executable,
            "-c",
            "import os; print(os.environ['LEAN_PATH'])",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )

    assert child.stdout.strip() == os.pathsep.join(
        (str(module_root), str(generated), "inherited")
    )
    assert os.environ["LEAN_PATH"] == "inherited"


@pytest.mark.asyncio
async def test_attached_configured_client_is_reused(
    tmp_path: Path,
    patched_clients: list[_MockAioClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path / "proj")
    monkeypatch.setenv(config.SERVER_OPTIONS_ENV, '{"autoImplicit":false}')
    ctx = _Context(_LifespanContext(project, None))
    client = _MockAioClient(project)
    attach_shared_client(project, client, client_utils._effective_server_command(ctx))

    assert await startup_client(ctx) is client
    assert patched_clients == []


@pytest.mark.asyncio
async def test_cancelled_startup_closes_client_before_cache_insertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_project(tmp_path / "proj")
    started = asyncio.Event()

    async def pending_start() -> None:
        started.set()
        await asyncio.Future()

    client = MagicMock(start=pending_start, close=AsyncMock())
    monkeypatch.setattr(
        client_utils, "AsyncLeanLSPClient", MagicMock(return_value=client)
    )
    ctx = _Context(_LifespanContext(project, None))
    startup = asyncio.create_task(startup_client(ctx))
    await started.wait()

    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup

    client.close.assert_awaited_once()
    assert client_utils._shared_clients == {}
    assert ctx.request_context.lifespan_context.client is None


@pytest.mark.asyncio
async def test_failed_startup_closes_client_and_preserves_start_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = MagicMock(
        start=AsyncMock(side_effect=RuntimeError("startup failed")),
        close=AsyncMock(side_effect=RuntimeError("cleanup failed")),
    )
    monkeypatch.setattr(
        client_utils, "AsyncLeanLSPClient", MagicMock(return_value=client)
    )

    with pytest.raises(ValueError, match="startup failed"):
        await client_utils._start_client(tmp_path)

    client.close.assert_awaited_once()
