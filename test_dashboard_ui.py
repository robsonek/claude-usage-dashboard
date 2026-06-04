import os

BASE = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def test_css_has_new_tokens():
    css = _read("static/style.css")
    for token in ("--bg:", "--panel:", "--accent:", "--ok:", "--warn:",
                  "--crit:", "--line-session:", "--line-sonnet:",
                  "--reset-marker-color:", "--mono:"):
        assert token in css, f"missing token {token}"


def test_css_has_no_light_theme():
    css = _read("static/style.css")
    assert '[data-theme="light"]' not in css
    assert ".theme-toggle" not in css
