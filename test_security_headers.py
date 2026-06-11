"""Audit point 5: baseline security response headers.

Conservative set (no strict CSP yet — the dashboard uses inline <script>, so a
real CSP needs nonces; deferred). HSTS is only emitted over HTTPS (TLS is
terminated at Cloudflare, which sets X-Forwarded-Proto) so it can't wrongly pin
localhost during plain-HTTP dev.
"""
import os

os.environ.setdefault('ALLOW_DEFAULT_CREDENTIALS', '1')

import app  # noqa: E402


def _client():
    return app.app.test_client()


def test_baseline_headers_present():
    r = _client().get('/login')
    assert r.headers.get('X-Content-Type-Options') == 'nosniff'
    assert r.headers.get('X-Frame-Options') == 'DENY'
    assert r.headers.get('Referrer-Policy') == 'no-referrer'


def test_hsts_sent_over_https():
    r = _client().get('/login', headers={'X-Forwarded-Proto': 'https'})
    assert 'max-age=' in r.headers.get('Strict-Transport-Security', '')


def test_hsts_absent_on_plain_http():
    r = _client().get('/login')
    assert 'Strict-Transport-Security' not in r.headers
