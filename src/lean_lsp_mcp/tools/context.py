"""Project and external-module context tools."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from lean_lsp_mcp import server
from lean_lsp_mcp.client_utils import bind_lean_project_path


def _require_directory(path: str, field_name: str) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise server.LeanToolError(
            f"{field_name} '{path}' is not an existing directory."
        ) from exc
    if not resolved.is_dir():
        raise server.LeanToolError(
            f"{field_name} '{resolved}' is not an existing directory."
        )
    return resolved


@server.mcp.tool(
    "lean_set_project_context",
    annotations=ToolAnnotations(
        title="Set Lean Project Context",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def set_project_context(
    ctx: Context,
    lean_project_path: Annotated[
        str, Field(description="Path to the Lake project providing imports")
    ],
    module_root: Annotated[
        Optional[str],
        Field(description="External Lean source root passed to the server as --root"),
    ] = None,
    extra_roots: Annotated[
        Optional[list[str]],
        Field(description="Additional source or generated .olean roots for LEAN_PATH"),
    ] = None,
) -> dict[str, str | list[str] | None]:
    """Bind a Lake project and optional external module roots for later file tools."""
    resolved_module_root = (
        _require_directory(module_root, "module_root")
        if module_root is not None
        else None
    )
    resolved_extra_roots: list[Path] = []
    for root in extra_roots or []:
        resolved = _require_directory(root, "extra_roots")
        if resolved != resolved_module_root and resolved not in resolved_extra_roots:
            resolved_extra_roots.append(resolved)

    try:
        project_root = bind_lean_project_path(ctx, lean_project_path)
    except ValueError as exc:
        raise server.LeanToolError(str(exc)) from exc

    lifespan = ctx.request_context.lifespan_context
    lifespan.external_roots_configured = True
    lifespan.external_module_root = resolved_module_root
    lifespan.external_extra_roots = tuple(resolved_extra_roots)
    return {
        "lean_project_path": str(project_root),
        "module_root": (
            str(resolved_module_root) if resolved_module_root is not None else None
        ),
        "extra_roots": [str(root) for root in resolved_extra_roots],
    }
