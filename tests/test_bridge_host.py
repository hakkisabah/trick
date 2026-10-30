"""Bridge bind policy: loopback by default, one explicit local address otherwise.

The container case is why this exists at all. Open WebUI runs in Docker and
cannot reach the host's 127.0.0.1, so the operator has to point the bridge at the
docker bridge address. A wildcard bind would also "fix" it, and would also expose
the service to every machine on the network, so the policy is tested rather than
trusted.
"""

from __future__ import annotations

import pytest

from trick.bridge import BridgeServer, validate_host
from trick.errors import ConfigError

TOKEN = "t" * 40


def test_loopback_is_accepted_without_ceremony():
    for host in ("127.0.0.1", "::1", "localhost"):
        assert validate_host(host) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "", "::", "*", " 0.0.0.0 "])
def test_wildcard_is_refused_even_when_explicitly_allowed(host):
    with pytest.raises(ConfigError):
        validate_host(host, allow_network=True)


def test_non_loopback_needs_the_explicit_flag():
    with pytest.raises(ConfigError):
        validate_host("172.17.0.1")
    assert validate_host("172.17.0.1", allow_network=True) == "172.17.0.1"


def test_bridge_refuses_a_network_bind_without_the_flag():
    with pytest.raises(ConfigError):
        BridgeServer(backend=None, token=TOKEN, host="172.17.0.1")  # type: ignore[arg-type]


def test_bridge_requires_a_token_even_on_a_network_bind():
    with pytest.raises(ConfigError):
        BridgeServer(backend=None, token="short", host="127.0.0.1")  # type: ignore[arg-type]
