"""Audit point 2: account email must not be interpolated into a JS string
context in accounts.html.

`onclick="reauth('{{ a.email|e }}')"` HTML-escapes the email, but the browser
HTML-decodes attribute values before the JS parser runs, so an email like
`x');alert(1);//` would break out of the string and execute. The safe pattern
is to carry the value in a data-* attribute (HTML-attribute context, correctly
autoescaped by Jinja) and read it via dataset in JS — never inline it into code.
"""
import os

os.environ.setdefault('ALLOW_DEFAULT_CREDENTIALS', '1')

BASE = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def test_email_not_inlined_into_js_string():
    html = _read("templates/accounts.html")
    # No variant of the email being dropped straight into a JS call argument.
    assert "reauth('{{ a.email" not in html
    assert 'reauth("{{ a.email' not in html


def test_reauth_uses_data_attribute():
    html = _read("templates/accounts.html")
    assert 'data-email="{{ a.email }}"' in html
    # And it is wired up without an inline handler that re-introduces the issue.
    assert "querySelectorAll('.reauth')" in html or 'querySelectorAll(".reauth")' in html


def test_rendered_template_escapes_malicious_email():
    """End-to-end: a hostile email rendered through Jinja must be HTML-escaped
    inside the data-email attribute, never left as a quote that closes it."""
    import app
    from flask import render_template
    hostile = 'x"><script>alert(1)</script>@e.com'
    accounts = [{
        'id': 1, 'label': 'L', 'email': hostile, 'account_type': 'max',
        'is_active': 1, 'created_at': None, 'last_polled_at': None, 'last_error': None,
    }]
    with app.app.test_request_context():
        rendered = render_template('accounts.html', version='x', accounts=accounts)
    assert '<script>alert(1)</script>' not in rendered
    assert 'data-email="x&#34;&gt;&lt;script&gt;' in rendered
