import fcntl
import http.server
import threading

import pytest


@pytest.mark.parametrize("raw,shown", [
    pytest.param("Title\x1b]0;pwned\x07 (1999)", "Title ]0;pwned  (1999)", id="osc-escape"),
    pytest.param("line\nforged log line", "line forged log line", id="newline"),
    pytest.param("over\rwrite", "over write", id="carriage-return"),
    pytest.param("Мої сусіди Ямада", "Мої сусіди Ямада", id="cyrillic-kept"),
    pytest.param(None, "", id="none"),
])
def test_tracker_text_reaches_the_terminal_as_one_plain_line(grab, raw, shown):
    assert grab.clean(raw) == shown


@pytest.mark.parametrize("url", [
    pytest.param("file:///etc/shadow", id="file"),
    pytest.param("ftp://example.org/x.torrent", id="ftp"),
    pytest.param("data:,d4:infoe", id="data"),
])
def test_release_link_must_be_http(grab, url):
    with pytest.raises(SystemExit, match="не http"):
        grab.fetch_release(url)


def test_magnet_is_passed_through(grab):
    assert grab.fetch_release("magnet:?xt=urn:btih:abc") == ("magnet", "magnet:?xt=urn:btih:abc")


@pytest.fixture
def server():
    body = {"payload": b""}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body["payload"])

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield body, "http://127.0.0.1:%d/dl" % httpd.server_address[1]
    httpd.shutdown()


def test_oversized_download_is_refused(grab, server, monkeypatch):
    body, url = server
    monkeypatch.setattr(grab, "TORRENT_MAX_BYTES", 1024)
    body["payload"] = b"d" + b"x" * 4096
    with pytest.raises(SystemExit, match="не .torrent"):
        grab.fetch_release(url)


def test_torrent_within_limit_is_returned(grab, server):
    body, url = server
    body["payload"] = b"d4:infoe"
    assert grab.fetch_release(url) == ("torrent", b"d4:infoe")


def hold(grab):
    handle = open(grab.LOCK_PATH, "w")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def test_cron_finish_skips_while_another_grab_holds_the_state(grab, monkeypatch, capsys):
    monkeypatch.setattr(grab, "load_state", lambda: pytest.fail("state read without the lock"))
    monkeypatch.setattr("sys.argv", ["grab", "finish", "--quiet"])
    holder = hold(grab)
    try:
        assert grab.main() == 0
    finally:
        holder.close()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("argv", [
    pytest.param(["аріетті"], id="search"),
    pytest.param(["0"], id="take-release"),
    pytest.param(["import", "part", "--to", "movie:1"], id="import"),
])
def test_state_writers_wait_for_the_lock(grab, monkeypatch, argv):
    ran = threading.Event()
    for name in ("cmd_search", "cmd_get", "cmd_import"):
        monkeypatch.setattr(grab, name, lambda *a, **k: ran.set() or 0)
    monkeypatch.setattr("sys.argv", ["grab"] + argv)
    holder = hold(grab)
    worker = threading.Thread(target=grab.main)
    worker.start()
    assert not ran.wait(0.3), "ran while another grab held the lock"
    holder.close()
    worker.join(5)
    assert ran.is_set()


@pytest.mark.parametrize("argv", [
    pytest.param([], id="status"),
    pytest.param(["subs", "arrietty"], id="subs"),
])
def test_readers_do_not_wait_for_the_lock(grab, monkeypatch, argv):
    monkeypatch.setattr(grab, "cmd_status", lambda state: 0)
    monkeypatch.setattr(grab, "cmd_subs", lambda query, state: 0)
    monkeypatch.setattr("sys.argv", ["grab"] + argv)
    holder = hold(grab)
    try:
        assert grab.main() == 0
    finally:
        holder.close()


def test_unconfigured_service_is_a_clear_refusal(grab, tmp_path, monkeypatch):
    (tmp_path / "radarr").mkdir()
    (tmp_path / "radarr" / "config.xml").write_text("<Config></Config>")
    monkeypatch.setattr(grab, "CONFIG_ROOT", str(tmp_path))
    with pytest.raises(SystemExit, match="не налаштований"):
        grab.arr_key("radarr")
