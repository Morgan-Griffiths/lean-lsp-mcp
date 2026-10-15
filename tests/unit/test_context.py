from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_lsp_mcp import client_utils, config, server
from lean_lsp_mcp.tools.context import set_project_context


@pytest.fixture(autouse=True)
def _clear_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (config.MODULE_ROOT_ENV, config.EXTRA_ROOTS_ENV):
        monkeypatch.delenv(name, raising=False)


def _make_project(root: Path) -> Path:
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.24.0\n")
    (root / "lakefile.toml").write_text('name = "test"\n')
    return root


def _make_context(project: Path, transport: str = "stdio") -> SimpleNamespace:
    lifespan = server.AppContext(
        lean_project_path=project,
        client=None,
        rate_limit={},
        lean_search_available=False,
        active_transport=transport,
        project_switching_allowed=transport == "stdio",
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=lifespan))


def test_context_binds_external_roots_and_can_clear_environment_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_project(tmp_path / "project")
    module_root = tmp_path / "theory"
    generated = tmp_path / "generated"
    module_root.mkdir()
    generated.mkdir()
    monkeypatch.setenv(config.MODULE_ROOT_ENV, str(module_root))
    ctx = _make_context(project)
    assert client_utils._effective_module_root(ctx) == module_root

    result = set_project_context(
        ctx,
        str(project),
        str(module_root),
        [str(generated), str(module_root), str(generated)],
    )
    assert result == {
        "lean_project_path": str(project),
        "module_root": str(module_root),
        "extra_roots": [str(generated)],
    }
    assert client_utils._effective_extra_roots(ctx) == (module_root, generated)

    set_project_context(ctx, str(project))
    assert client_utils._effective_module_root(ctx) is None
    assert client_utils._effective_extra_roots(ctx) == ()


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_remote_context_cannot_expand_roots(tmp_path: Path, transport: str) -> None:
    project = _make_project(tmp_path / "project")
    other_project = _make_project(tmp_path / "other")
    external = tmp_path / "external"
    external.mkdir()
    ctx = _make_context(project, transport)
    lifespan = ctx.request_context.lifespan_context

    with pytest.raises(
        server.LeanToolError, match="External root switching is disabled"
    ):
        set_project_context(ctx, str(other_project), extra_roots=[str(external)])

    assert lifespan.lean_project_path == project
    assert lifespan.external_extra_roots == ()
    assert not lifespan.external_roots_configured


def test_remote_context_can_repeat_configured_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_project(tmp_path / "project")
    module_root = tmp_path / "theory"
    generated = tmp_path / "generated"
    module_root.mkdir()
    generated.mkdir()
    monkeypatch.setenv(config.MODULE_ROOT_ENV, str(module_root))
    monkeypatch.setenv(config.EXTRA_ROOTS_ENV, json.dumps([str(generated)]))
    ctx = _make_context(project, "streamable-http")

    set_project_context(ctx, str(project), str(module_root), [str(generated)])

    assert client_utils._effective_extra_roots(ctx) == (module_root, generated)


def test_invalid_external_root_does_not_rebind_project(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "project")
    other_project = _make_project(tmp_path / "other")
    ctx = _make_context(project)

    with pytest.raises(server.LeanToolError, match="not an existing directory"):
        set_project_context(ctx, str(other_project), str(tmp_path / "missing"))

    assert ctx.request_context.lifespan_context.lean_project_path == project
    assert not ctx.request_context.lifespan_context.external_roots_configured
