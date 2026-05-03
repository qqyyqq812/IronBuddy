"""Stage 6 tests: debug tab code graph replaced with GitHub external links."""
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(PROJECT_ROOT, "templates", "index.html")


def _read():
    with open(INDEX, "r", encoding="utf-8") as f:
        return f.read()


def test_force_graph_cdn_removed():
    src = _read()
    assert "force-graph@1.43.5" not in src
    assert "ForceGraph()" not in src


def test_github_link_cards_present():
    src = _read()
    assert 'id="codeGraphLinkRepo"' in src
    assert 'id="codeGraphLinkBranch"' in src
    assert 'id="codeGraphLinkCommit"' in src
    assert 'id="codeGraphLinkDeps"' in src
    assert "https://github.com/qqyyqq812/IronBuddy" in src
    # dependency graph uses /network/dependencies
    assert "/network/dependencies" in src


def test_load_code_graph_updates_commit_href():
    src = _read()
    assert "codeGraphCommitLabel" in src
    # function body must update commit href dynamically
    idx = src.find("async function loadCodeGraph(")
    assert idx != -1
    body = src[idx:idx + 1800]
    assert "/commit/" in body
    assert "fetch('/api/code_graph'" in body