"""Bulk-load CLI tests — no Virtuoso required; httpx and subprocess stubbed."""
from __future__ import annotations

# pylint: disable=protected-access

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from virtuoso_sink import bulk_load
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


def test_sparql_load_max_bytes_constant_matches_virtuoso_fa008() -> None:
    # Virtuoso 7's `LOAD <url>` reads the file as a string and returns
    # FA008 "File ... is too large (... bytes), cannot return string
    # content larger than 10485760 bytes". This constant must match.
    assert SPARQL_LOAD_MAX_BYTES == 10 * 1024 * 1024


# ── the loaders ───────────────────────────────────────────────────────────
# Everything above this line is a pure helper. The three functions that do
# the actual work — _load_one, _load_via_isql, load_directory — had no
# tests at all: 77 of 112 lines uncovered, which is what held
# fontem-virtuoso-sink under the 80% gate. No Virtuoso required; httpx and
# subprocess are stubbed.

def test_load_one_issues_a_sparql_load_into_the_target_graph() -> None:
    client = MagicMock()
    client.post.return_value = MagicMock(status_code=200, text="")
    bulk_load._load_one(client, "http://v/sparql-auth",
                        "file:///d/a.rdf", "http://g", 30.0)
    sent = client.post.call_args
    assert sent.args[0] == "http://v/sparql-auth"
    assert sent.kwargs["data"] == {
        "query": "LOAD <file:///d/a.rdf> INTO GRAPH <http://g>"}
    assert sent.kwargs["timeout"] == 30.0


def test_load_one_raises_on_an_error_status() -> None:
    """Virtuoso answers 4xx/5xx with a body that names the fault; swallowing
    it would report a successful load that never happened."""
    client = MagicMock()
    client.post.return_value = MagicMock(status_code=500, text="FA008 boom")
    with pytest.raises(RuntimeError, match="HTTP 500"):
        bulk_load._load_one(client, "http://v/sparql", "u", "g", 1.0)


def test_load_via_isql_refuses_when_the_binary_is_absent(tmp_path: Path) -> None:
    with patch.object(bulk_load, "ISQL_BIN", str(tmp_path / "nope")):
        with pytest.raises(RuntimeError, match="isql binary not found"):
            bulk_load._load_via_isql(
                host="h", port=1111, user="u", password="p",
                directory=tmp_path, pattern="*.rdf", graph="g",
                timeout_s=1.0)


def test_load_via_isql_builds_ld_dir_and_returns_stdout(tmp_path: Path) -> None:
    isql = _touch(tmp_path, "isql", "#!/bin/sh\n")
    with patch.object(bulk_load, "ISQL_BIN", str(isql)), \
         patch.object(bulk_load.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        out = bulk_load._load_via_isql(
            host="h", port=1111, user="dba", password="pw",
            directory=tmp_path, pattern="*.rdf", graph="http://g",
            timeout_s=5.0)
    assert out == "ok"
    cmd = run.call_args.args[0]
    assert cmd[:4] == [str(isql), "h:1111", "dba", "pw"]
    assert "ld_dir(" in cmd[4] and "rdf_loader_run();" in cmd[4]


def test_load_via_isql_raises_on_non_zero_return(tmp_path: Path) -> None:
    isql = _touch(tmp_path, "isql", "#!/bin/sh\n")
    with patch.object(bulk_load, "ISQL_BIN", str(isql)), \
         patch.object(bulk_load.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=2, stdout="", stderr="nope")
        with pytest.raises(RuntimeError, match="rc=2"):
            bulk_load._load_via_isql(
                host="h", port=1111, user="u", password="p",
                directory=tmp_path, pattern="*.rdf", graph="g",
                timeout_s=5.0)


def _dir_with(tmp_path: Path, *names: str, size: int = 10) -> Path:
    for n in names:
        _touch(tmp_path, n, "x" * size)
    return tmp_path


def test_load_directory_no_matches_is_not_an_error(tmp_path: Path) -> None:
    out = bulk_load.load_directory(
        directory=tmp_path, pattern="*.rdf", graph="g",
        endpoint="http://v/sparql", auth=("u", "p"), mode="file")
    assert out == {"files": 0, "elapsed_s": 0.0, "errors": 0}


def test_load_directory_file_mode_sends_a_file_uri(tmp_path: Path) -> None:
    _dir_with(tmp_path, "a.rdf")
    with patch.object(bulk_load, "_load_one") as one:
        out = bulk_load.load_directory(
            directory=tmp_path, pattern="*.rdf", graph="http://g",
            endpoint="http://v/sparql", auth=("u", "p"), mode="file")
    assert out["files"] == 1 and out["errors"] == 0
    assert one.call_args.args[2].startswith("file://")


def test_load_directory_http_mode_appends_the_quoted_filename(tmp_path: Path) -> None:
    _dir_with(tmp_path, "a b.rdf")
    with patch.object(bulk_load, "_load_one") as one:
        bulk_load.load_directory(
            directory=tmp_path, pattern="*.rdf", graph="http://g",
            endpoint="http://v/sparql", auth=("u", "p"), mode="http",
            url_prefix="http://files/")
    assert one.call_args.args[2] == "http://files/a%20b.rdf"


def test_load_directory_http_mode_requires_a_url_prefix(tmp_path: Path) -> None:
    """Without it the LOAD would target a URL Virtuoso cannot resolve."""
    _dir_with(tmp_path, "a.rdf")
    with pytest.raises(ValueError, match="url-prefix"):
        bulk_load.load_directory(
            directory=tmp_path, pattern="*.rdf", graph="g",
            endpoint="http://v/sparql", auth=("u", "p"), mode="http")


def test_load_directory_rejects_an_unknown_mode(tmp_path: Path) -> None:
    _dir_with(tmp_path, "a.rdf")
    with pytest.raises(ValueError, match="Unknown mode"):
        bulk_load.load_directory(
            directory=tmp_path, pattern="*.rdf", graph="g",
            endpoint="http://v/sparql", auth=("u", "p"), mode="ftp")


def test_load_directory_counts_a_failed_file_and_keeps_going(tmp_path: Path) -> None:
    """One bad file must not abandon the rest of the batch."""
    _dir_with(tmp_path, "a.rdf", "b.rdf", "c.rdf")
    with patch.object(bulk_load, "_load_one",
                      side_effect=[httpx.HTTPError("x"), None, None]):
        out = bulk_load.load_directory(
            directory=tmp_path, pattern="*.rdf", graph="g",
            endpoint="http://v/sparql", auth=("u", "p"), mode="file")
    assert out == {"files": 2, "errors": 1, "elapsed_s": out["elapsed_s"]}


def test_load_directory_switches_to_isql_over_the_size_ceiling(tmp_path: Path) -> None:
    """LOAD <url> reads the whole file into memory and dies with FA008 past
    10 MiB, so anything larger has to go through the streaming loader."""
    _dir_with(tmp_path, "big.rdf", size=bulk_load.SPARQL_LOAD_MAX_BYTES + 1)
    with patch.object(bulk_load, "_load_via_isql",
                      return_value="done") as isql, \
         patch.object(bulk_load, "_load_one") as one:
        out = bulk_load.load_directory(
            directory=tmp_path, pattern="*.rdf", graph="g",
            endpoint="http://virt:8890/sparql", auth=("dba", "pw"),
            mode="file")
    isql.assert_called_once()
    one.assert_not_called()
    assert out["files"] == 1 and out["errors"] == 0


def test_load_directory_marks_the_whole_batch_failed_when_isql_dies(tmp_path: Path) -> None:
    """isql loads the directory in one call, so its failure fails everything."""
    _dir_with(tmp_path, "big.rdf", size=bulk_load.SPARQL_LOAD_MAX_BYTES + 1)
    with patch.object(bulk_load, "_load_via_isql",
                      side_effect=RuntimeError("boom")):
        out = bulk_load.load_directory(
            directory=tmp_path, pattern="*.rdf", graph="g",
            endpoint="http://virt:8890/sparql", auth=("dba", "pw"),
            mode="file")
    assert out["errors"] == 1 and out["files"] == 0
