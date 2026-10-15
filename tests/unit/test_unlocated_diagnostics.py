from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from leanclient.aio import DiagnosticsReport

from lean_lsp_mcp import server
from lean_lsp_mcp.tools import diagnostics


def test_global_error_keeps_its_message_in_diagnostics() -> None:
    message = "Lean server could not load the requested module"

    result = server._process_diagnostics(
        [{"severity": 1, "message": message}], build_success=False
    )

    assert result.success is False
    assert len(result.items) == 1
    assert result.items[0].message == message
    assert result.items[0].severity == "error"
    assert (result.items[0].line, result.items[0].column) == (1, 1)


def test_global_error_survives_conversion_for_code_results() -> None:
    message = "Lean server terminated before elaboration completed"

    diagnostics = server._to_diagnostic_messages([{"message": message}])

    assert len(diagnostics) == 1
    assert diagnostics[0].message == message
    assert diagnostics[0].severity == "error"


def test_global_diagnostics_respect_severity_filter() -> None:
    result = server._process_diagnostics(
        [
            {"severity": 1, "message": "global error"},
            {"severity": 2, "message": "global warning"},
        ],
        build_success=False,
        severity="warning",
    )

    assert [(item.severity, item.message) for item in result.items] == [
        ("warning", "global warning")
    ]


def test_unlocated_build_failure_keeps_original_message() -> None:
    message = "lake setup-file failed before a source location was available"
    result = server._process_diagnostics([{"message": message}], build_success=False)
    assert result.items[0].message == message


@pytest.mark.asyncio
async def test_filtered_tool_keeps_global_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "Lean server could not load the requested module"
    client = SimpleNamespace(
        diagnostics=AsyncMock(
            return_value=DiagnosticsReport(
                items=[{"severity": 1, "message": message}], version=1
            )
        )
    )
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client))
    )
    monkeypatch.setattr(
        server, "setup_client_for_file", AsyncMock(return_value="Foo.lean")
    )
    monkeypatch.setattr(diagnostics, "open_synced", AsyncMock())

    result = await diagnostics.diagnostic_messages(
        ctx, "Foo.lean", start_line=8, end_line=12
    )

    assert result.success is False
    assert result.items[0].message == message
