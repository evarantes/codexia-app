"""Static dashboard contract checks; no real users, credentials or paid providers."""
import ast
from pathlib import Path


def _dashboard_html():
    source = Path(__file__).with_name("execution.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "production_dashboard":
            returns = [part for part in node.body if isinstance(part, ast.Return)]
            assert len(returns) == 1
            return ast.literal_eval(returns[0].value)
    raise AssertionError("Dashboard route missing")


def test_login_uses_existing_codexia_token_without_persistent_browser_storage():
    html = _dashboard_html()
    assert "fetch('/token'" in html
    assert "'Authorization':'Bearer '+accessToken" in html
    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert "must_change_password" in html
    assert "accessToken=null" in html


def test_paid_generation_and_publication_are_not_automatic():
    html = _dashboard_html()
    assert "start-music?confirmed=true" in html
    assert "Confirmar geração musical?" in html
    assert "start-clip" in html
    assert "Confirmar início da geração do clipe?" in html
    assert "auto_upload_youtube" not in html
    assert "Cancelar projeto" in html
