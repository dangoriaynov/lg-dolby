import pathlib

import pytest
import yaml

COMPOSE = yaml.safe_load(
    (pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]
# Свій логін і потрібні з VPN — лише ці два слухають на всіх адресах LXC.
OPEN = {("jellyfin", "8096:8096"), ("jellyseerr", "5055:5055")}


def published():
    for name, svc in SERVICES.items():
        for port in svc.get("ports", []):
            yield pytest.param(name, str(port), id="%s-%s" % (name, port))


@pytest.mark.parametrize("name,port", list(published()))
def test_only_logged_in_apps_listen_beyond_loopback(name, port):
    if (name, port) in OPEN:
        return
    assert port.startswith("127.0.0.1:"), "%s publishes %s on every LXC address" % (name, port)


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_service_forbids_privilege_gain(name):
    assert "no-new-privileges:true" in SERVICES[name].get("security_opt", [])


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_image_is_pinned_to_a_version(name):
    image = SERVICES[name]["image"]
    assert ":" in image.rsplit("/", 1)[-1] and not image.endswith(":latest")


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_no_service_gets_the_gpu(name):
    svc = SERVICES[name]
    assert svc.get("runtime") != "nvidia" and "deploy" not in svc
