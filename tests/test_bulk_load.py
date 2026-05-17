"""Bulk-load CLI tests — pure functions only, no Virtuoso required."""
from __future__ import annotations

from pathlib import Path

import pytest

from virtuoso_sink.bulk_load import (
    _file_uri, _list_files, _shell_quote_sql, _sparql_host_port,
    SPARQL_LOAD_MAX_BYTES,
)


def _touch(d: Path, name: str, content: str = "") -> Path:
    p = d / name
    p.write_text(content)
    return p


def test_list_files_filters_by_pattern(tmp_path: Path) -> None:
    _touch(tmp_path, "a.nt", "<a> <b> <c> .\n")
    _touch(tmp_path, "b.nt", "<a> <b> <d> .\n")
    _touch(tmp_path, "c.ttl", "@prefix : <#> . :a :b :c .\n")
    _touch(tmp_path, "README", "ignored")

    nt_files = _list_files(tmp_path, "*.nt")
    assert [p.name for p in nt_files] == ["a.nt", "b.nt"]
    ttl_files = _list_files(tmp_path, "*.ttl")
    assert [p.name for p in ttl_files] == ["c.ttl"]


def test_list_files_returns_sorted_order(tmp_path: Path) -> None:
    """Loading order must be reproducible across runs — the
    drop-and-reload pattern depends on it."""
    for name in ["c.nt", "a.nt", "b.nt"]:
        _touch(tmp_path, name)
    assert [p.name for p in _list_files(tmp_path, "*.nt")] == [
        "a.nt", "b.nt", "c.nt",
    ]


def test_list_files_raises_on_non_directory(tmp_path: Path) -> None:
    f = _touch(tmp_path, "x.nt")
    with pytest.raises(ValueError, match="Not a directory"):
        _list_files(f, "*")


def test_list_files_empty_matches_returns_empty(tmp_path: Path) -> None:
    _touch(tmp_path, "x.txt")
    assert _list_files(tmp_path, "*.nt") == []


def test_list_files_skips_subdirectories(tmp_path: Path) -> None:
    """We don't recurse — the runbook always points at a flat directory."""
    _touch(tmp_path, "a.nt")
    (tmp_path / "sub").mkdir()
    _touch(tmp_path / "sub", "b.nt")
    assert [p.name for p in _list_files(tmp_path, "*.nt")] == ["a.nt"]


def test_file_uri_renders_resolved_path(tmp_path: Path) -> None:
    f = _touch(tmp_path, "test.nt")
    uri = _file_uri(f)
    assert uri.startswith("file://")
    assert uri.endswith("/test.nt")
    # The path part should match the resolved (absolute) version.
    assert str(f.resolve()) in uri


def test_file_uri_escapes_spaces(tmp_path: Path) -> None:
    f = _touch(tmp_path, "with space.nt")
    uri = _file_uri(f)
    # %20 is the canonical encoding for a literal space inside a file URI.
    assert "%20" in uri
    assert " " not in uri.split("file://")[1]


def test_sparql_host_port_extracts_hostname() -> None:
    host, port = _sparql_host_port(
        "http://virtuoso.fontem-prod.svc.cluster.local:8890/sparql-auth"
    )
    assert host == "virtuoso.fontem-prod.svc.cluster.local"
    assert port == 1111


def test_sparql_host_port_honours_env_override(monkeypatch) -> None:
    monkeypatch.setenv("VIRTUOSO_ISQL_PORT", "1112")
    _, port = _sparql_host_port("http://x:8890/sparql")
    assert port == 1112


def test_sparql_host_port_rejects_bare_path() -> None:
    with pytest.raises(ValueError, match="Cannot parse"):
        _sparql_host_port("not-a-url")


def test_shell_quote_sql_doubles_single_quotes() -> None:
    # Virtuoso's isql expects ''' to represent a literal single quote
    # inside a quoted SQL string. Identifiers like 'O'Brien' must
    # become 'O''Brien'.
    assert _shell_quote_sql("O'Brien") == "O''Brien"
    assert _shell_quote_sql("plain") == "plain"
    assert _shell_quote_sql("two''already") == "two''''already"


def test_sparql_load_max_bytes_constant_matches_virtuoso_FA008() -> None:
    # Virtuoso 7's `LOAD <url>` reads the file as a string and returns
    # FA008 "File ... is too large (... bytes), cannot return string
    # content larger than 10485760 bytes". This constant must match.
    assert SPARQL_LOAD_MAX_BYTES == 10 * 1024 * 1024
