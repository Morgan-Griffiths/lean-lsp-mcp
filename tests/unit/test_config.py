"""Tests for the centralized config module."""

import os

import pytest

from lean_lsp_mcp import config


def test_active_transport_default_and_normalization(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(config.ACTIVE_TRANSPORT_ENV, raising=False)
    assert config.active_transport() == "stdio"
    monkeypatch.setenv(config.ACTIVE_TRANSPORT_ENV, "  STDIO  ")
    assert config.active_transport() == "stdio"
    monkeypatch.setenv(config.ACTIVE_TRANSPORT_ENV, "")
    assert config.active_transport() == "stdio"


def test_max_open_files(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(config.MAX_OPEN_FILES_ENV, raising=False)
    assert config.max_open_files() == 4
    monkeypatch.setenv(config.MAX_OPEN_FILES_ENV, "10")
    assert config.max_open_files() == 10
    monkeypatch.setenv(config.MAX_OPEN_FILES_ENV, "not-an-int")
    assert config.max_open_files() == 4
    monkeypatch.setenv(config.MAX_OPEN_FILES_ENV, "0")
    assert config.max_open_files() == 4


def test_server_options(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(config.SERVER_OPTIONS_ENV, raising=False)
    assert config.server_options() == {}

    monkeypatch.setenv(
        config.SERVER_OPTIONS_ENV,
        '{"weak.linter.style.header":false,"maxHeartbeats":400000,"trace.Meta.Tactic.simp.rewrite":"true"}',
    )
    assert config.server_options() == {
        "weak.linter.style.header": False,
        "maxHeartbeats": 400000,
        "trace.Meta.Tactic.simp.rewrite": "true",
    }


@pytest.mark.parametrize(
    "value,match",
    [
        ("not-json", "must be a JSON object"),
        ("[]", "must be a JSON object"),
        ('{"bad name":true}', "option names"),
        ('{"bad=name":true}', "option names"),
        ('{"maxHeartbeats":1.5}', "boolean, integer, or string"),
    ],
)
def test_server_options_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch, value: str, match: str
):
    monkeypatch.setenv(config.SERVER_OPTIONS_ENV, value)
    with pytest.raises(ValueError, match=match):
        config.server_options()


def test_external_module_roots(monkeypatch: pytest.MonkeyPatch, tmp_path):
    module_root = tmp_path / "theory"
    extra_root = tmp_path / "generated"
    module_root.mkdir()
    extra_root.mkdir()
    monkeypatch.setenv(config.MODULE_ROOT_ENV, str(module_root))
    monkeypatch.setenv(config.EXTRA_ROOTS_ENV, f'["{extra_root}"]')
    monkeypatch.setenv("LEAN_PATH", "/existing/path")

    assert config.module_root() == module_root
    assert config.extra_roots() == (module_root, extra_root)
    assert config.server_arguments() == (f"--root={module_root}",)
    assert config.server_env() == {
        "LEAN_PATH": f"{module_root}{os.pathsep}{extra_root}{os.pathsep}/existing/path"
    }


@pytest.mark.parametrize("value", ["not-json", "{}", "[1]", '[""]', '["   "]'])
def test_extra_roots_reject_invalid_values(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv(config.EXTRA_ROOTS_ENV, value)
    with pytest.raises(ValueError, match="JSON array"):
        config.extra_roots()


def test_build_concurrency(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(config.BUILD_CONCURRENCY_ENV, raising=False)
    assert config.build_concurrency() == "allow"
    monkeypatch.setenv(config.BUILD_CONCURRENCY_ENV, "CANCEL")
    assert config.build_concurrency() == "cancel"
    monkeypatch.setenv(config.BUILD_CONCURRENCY_ENV, "bogus")
    assert config.build_concurrency() == "allow"


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("1", True), ("yes", True), ("", False), ("no", False)],
)
def test_truthy_flags(monkeypatch: pytest.MonkeyPatch, value, expected):
    monkeypatch.setenv(config.LOOGLE_LOCAL_ENV, value)
    assert config.loogle_local_enabled() is expected
    monkeypatch.setenv(config.REPL_ENV, value)
    assert config.repl_enabled() is expected


def test_url_defaults(monkeypatch: pytest.MonkeyPatch):
    for getter, env, default in [
        (config.loogle_url, config.LOOGLE_URL_ENV, config.DEFAULT_LOOGLE_URL),
        (
            config.state_search_url,
            config.STATE_SEARCH_URL_ENV,
            config.DEFAULT_STATE_SEARCH_URL,
        ),
        (config.hammer_url, config.HAMMER_URL_ENV, config.DEFAULT_HAMMER_URL),
    ]:
        monkeypatch.delenv(env, raising=False)
        assert getter() == default
        monkeypatch.setenv(env, "http://localhost:9000")
        assert getter() == "http://localhost:9000"


def test_is_custom_backend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(config.STATE_SEARCH_URL_ENV, raising=False)
    assert (
        config.is_custom_backend(
            config.STATE_SEARCH_URL_ENV, config.DEFAULT_STATE_SEARCH_URL
        )
        is False
    )
    monkeypatch.setenv(
        config.STATE_SEARCH_URL_ENV, config.DEFAULT_STATE_SEARCH_URL + "/"
    )
    assert (
        config.is_custom_backend(
            config.STATE_SEARCH_URL_ENV, config.DEFAULT_STATE_SEARCH_URL
        )
        is False
    )
    monkeypatch.setenv(config.STATE_SEARCH_URL_ENV, "http://localhost:8000")
    assert (
        config.is_custom_backend(
            config.STATE_SEARCH_URL_ENV, config.DEFAULT_STATE_SEARCH_URL
        )
        is True
    )


def test_repl_numeric_getters(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(config.REPL_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(config.REPL_MEM_MB_ENV, raising=False)
    assert config.repl_timeout() == 60
    assert config.repl_mem_mb() == 8192
    monkeypatch.setenv(config.REPL_TIMEOUT_ENV, "120")
    assert config.repl_timeout() == 120
