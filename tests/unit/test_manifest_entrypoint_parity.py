"""The standalone entry point must provision secrets under the manifest's namespace.

Both sides are READ FROM THE FILES — config/agent.yaml is parsed and
src/api/server.py is AST-walked for the secrets_factory(...) call — so a
test that hard-coded the expected value twice could not pass while the
two files drifted apart again.
"""

import ast
import pathlib

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MANIFEST = _ROOT / "config" / "agent.yaml"
_SERVER = _ROOT / "src" / "api" / "server.py"


def _manifest() -> dict:
    loaded = yaml.safe_load(_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _secrets_factory_call() -> dict[str, str]:
    """Return the keyword literals of the ONE secrets_factory(...) call in server.py."""
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"), filename=str(_SERVER))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "secrets_factory")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "factory")
        )
    ]
    assert len(calls) == 1, f"expected exactly one secrets_factory(...) call, found {len(calls)}"
    keywords: dict[str, str] = {}
    for keyword in calls[0].keywords:
        assert isinstance(keyword.value, ast.Constant) and isinstance(
            keyword.value.value, str
        ), f"secrets_factory({keyword.arg}=...) must be a string literal so the manifest parity is checkable"
        keywords[keyword.arg or ""] = keyword.value.value
    return keywords


def test_entrypoint_namespace_matches_the_manifest():
    manifest = _manifest()
    call = _secrets_factory_call()
    assert call["namespace"] == manifest["namespace"], (
        f"src/api/server.py provisions secrets under namespace={call['namespace']!r} "
        f"but config/agent.yaml declares namespace={manifest['namespace']!r}"
    )


def test_manifest_namespace_is_the_lowercased_industry():
    manifest = _manifest()
    assert manifest["namespace"] == str(manifest["industry"]).lower()


def test_entrypoint_agent_name_matches_the_manifest_class():
    manifest = _manifest()
    call = _secrets_factory_call()
    assert call["agent_name"] == str(manifest["class"]).rsplit(".", 1)[-1]
