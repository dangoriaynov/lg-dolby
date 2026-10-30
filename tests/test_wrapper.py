import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_upload_goes_through_a_private_temp_file(tmp_path):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "calls"
    (stubs / "ssh").write_text('#!/bin/bash\nshift 3\nexec bash -c "$1"\n')
    # lxc tar: список вмісту архіву й шлях, з якого прийшов stdin
    (stubs / "lxc").write_text(
        '#!/bin/bash\nif [ "$4" = tar ]; then\n'
        '  echo "src=$(readlink /proc/$$/fd/0)" >> "%s"; tar -tf - >> "%s"\n'
        'else echo "run: $*" >> "%s"; fi\n' % (log, log, log))
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    env = dict(os.environ, PATH="%s:%s" % (stubs, os.environ["PATH"]), TMPDIR=str(tmp_path))
    subprocess.run(["bash", str(ROOT / "grab"), "it's"], check=True, env=env)
    calls = log.read_text()
    src = next(l for l in calls.splitlines() if l.startswith("src="))[4:]
    assert src.startswith(str(tmp_path)) and src != "/tmp/grab.tar"
    assert not os.path.exists(src), "temp archive left behind"
    assert "scripts/grab.py" in calls and "scripts/subs-translate.py" in calls
    assert "grab.py it's" in calls
