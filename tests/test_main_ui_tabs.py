"""Static checks for the main UI tab restructure (Stage 2).

Verifies:
- 数据 tab 不再渲染 RAG / OpenCloud / 旧 code graph 容器
- switchTab 不再调用 loadDemoShowcase
- 调试 tab 有 codeGraphMount + operatorIframe + 折叠日志
- loadCodeGraph 在 logs tab 触发
"""
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(PROJECT_ROOT, "templates", "index.html")


def _read():
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        return f.read()


def test_data_tab_no_rag_or_opencloud():
    src = _read()
    assert 'id="demoShowcaseContainer"' not in src
    assert 'id="codeGraphContainer"' not in src


def test_switch_tab_does_not_call_demo_showcase():
    src = _read()
    assert "loadDemoShowcase()" not in src


def test_logs_tab_has_iframe_and_graph_slots():
    src = _read()
    assert 'id="codeGraphMount"' in src
    assert 'id="operatorIframe"' in src


def test_logs_tab_log_terminal_collapsible():
    src = _read()
    assert 'id="logTerminalDetails"' in src


def test_logs_tab_has_feedback_area():
    src = _read()
    assert 'id="feedbackNote"' in src
    assert 'id="feedbackFile"' in src
    assert "submitFeedback()" in src


def test_load_code_graph_called_in_logs_tab():
    src = _read()
    # Inside switchTab, when tabId === 'logs', loadCodeGraph should be called
    idx = src.find("function switchTab(")
    assert idx != -1
    # Look at next ~600 chars
    body = src[idx:idx + 1200]
    assert "loadCodeGraph" in body
    assert "'logs'" in body


def test_streamer_has_code_graph_endpoint():
    """Stage 4.2: /api/code_graph endpoint reads graph.json."""
    streamer_path = os.path.join(PROJECT_ROOT, "streamer_app.py")
    with open(streamer_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "@app.route('/api/code_graph')" in src
    assert "data/code_graph/graph.json" in src
    assert "IRONBUDDY_CODE_GRAPH_PATH" in src
