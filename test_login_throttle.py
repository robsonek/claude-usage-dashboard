"""Audit point 1: the login throttle keys on the client IP, so that IP must come
from a header the edge controls — not one the client can forge.

Behind Cloudflare, CF-Connecting-IP is set by the edge and overwrites any
client-supplied value. X-Forwarded-For's first element is attacker-controlled
(the edge APPENDS the real IP, it doesn't replace the chain), so keying the
throttle on XFF lets a brute-forcer rotate the bucket every request and also
grows the in-process fails dict without bound.
"""
import os

os.environ.setdefault('ALLOW_DEFAULT_CREDENTIALS', '1')

import app  # noqa: E402
from app import _client_ip  # noqa: E402


def test_client_ip_prefers_cf_connecting_ip_over_spoofable_xff():
    with app.app.test_request_context(headers={
        'CF-Connecting-IP': '203.0.113.7',
        'X-Forwarded-For': '1.2.3.4',  # forged by the attacker — must be ignored
    }, environ_base={'REMOTE_ADDR': '127.0.0.1'}):
        assert _client_ip() == '203.0.113.7'


def test_client_ip_ignores_xforwarded_for_when_no_edge_header():
    """Without the trusted edge header we fall back to the direct peer, NOT the
    forgeable X-Forwarded-For."""
    with app.app.test_request_context(headers={
        'X-Forwarded-For': '1.2.3.4',
    }, environ_base={'REMOTE_ADDR': '198.51.100.9'}):
        assert _client_ip() == '198.51.100.9'


def test_client_ip_falls_back_to_remote_addr():
    with app.app.test_request_context(environ_base={'REMOTE_ADDR': '198.51.100.9'}):
        assert _client_ip() == '198.51.100.9'
