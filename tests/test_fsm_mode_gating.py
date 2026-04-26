"""V7.30 M2 修补测试：vision_sensor 模式下 FSM 不直接增加 good/failed。

main_claw_loop.py 的 rep-completion 逻辑深嵌在 update() 大循环里，依赖 cv2 +
torch + 实时帧。为避免这些重依赖，本测试用 AST 静态分析校验：
两处 increment 块都被 `if self._mode_cache != "vision_sensor":` 包裹。

V7.18 把它解开过；V7.30 恢复原 V7.15 设计（让 GRU 在 vision_sensor 下独占累加）。
"""
import ast
import os

MAIN_LOOP = os.path.join(
    os.path.dirname(__file__), "..", "hardware_engine", "main_claw_loop.py"
)


def _load_source():
    with open(MAIN_LOOP, "r", encoding="utf-8") as f:
        return f.read()


def test_squat_increment_guarded_by_mode_cache():
    src = _load_source()
    assert "self.good_squats += 1" in src
    idx = src.find("self.good_squats += 1")
    pre = src[max(0, idx - 600) : idx]
    assert 'self._mode_cache != "vision_sensor"' in pre, (
        "squat good/failed increment must be guarded by mode_cache != vision_sensor"
    )


def test_curl_increment_guarded_by_mode_cache():
    src = _load_source()
    assert "self._good_reps += 1" in src
    idx = src.find("self._good_reps += 1")
    pre = src[max(0, idx - 600) : idx]
    assert 'self._mode_cache != "vision_sensor"' in pre, (
        "curl good/failed increment must be guarded by mode_cache != vision_sensor"
    )


def test_module_still_parses():
    src = _load_source()
    ast.parse(src)


def test_only_two_guard_sites_total():
    src = _load_source()
    n = src.count('self._mode_cache != "vision_sensor"')
    assert n == 2, "expected exactly 2 mode-gating sites (squat + curl), got %d" % n
