"""
IronBuddy PPT 配图生成器
生成 G01-G14 架构图 + matplotlib 图 + S01-S08 占位图
输出到 ../assets/
"""
import os
import sys
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Circle, Polygon
from matplotlib.lines import Line2D
import matplotlib.path as mpath
import numpy as np

ASSETS = Path(__file__).parent.parent / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

# 中文字体设置（优先尝试多种字体）
from matplotlib import font_manager
CJK_CANDIDATES = [
    "Noto Sans CJK SC", "Noto Sans CJK", "Noto Sans CJK TC",
    "Noto Serif CJK SC", "Source Han Sans SC", "Source Han Sans CN",
    "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Microsoft YaHei",
    "PingFang SC", "SimHei", "SimSun",
]
_installed = set(f.name for f in font_manager.fontManager.ttflist)
CJK = next((c for c in CJK_CANDIDATES if c in _installed), None)
if CJK:
    plt.rcParams["font.sans-serif"] = [CJK, "DejaVu Sans"]
else:
    print("WARN: no CJK font found, will render 方块", file=sys.stderr)
plt.rcParams["axes.unicode_minus"] = False

# 色板（玻璃拟态 + 深蓝金）
C_BG = "#0f1724"       # 主背景
C_CARD = "#1e2a3a"     # 卡片底
C_ACCENT = "#d4a04a"   # 金
C_PRIMARY = "#4a90e2"  # 蓝
C_SUCCESS = "#7ed321"  # 绿
C_DANGER = "#d0021b"   # 红
C_MUTED = "#8b96a8"    # 灰
C_TEXT_DARK = "#1a1a1a"
C_TEXT_LIGHT = "#f5f7fa"
C_BORDER = "#2d3e54"


def _save(fig, name, dpi=180, bg=C_BG):
    fp = ASSETS / f"{name}.png"
    fig.savefig(fp, dpi=dpi, bbox_inches="tight", facecolor=bg, edgecolor="none")
    plt.close(fig)
    print(f"  → {fp.name}")


def _card(ax, x, y, w, h, title=None, subtitle=None,
          face=C_CARD, edge=C_BORDER, title_color=C_TEXT_LIGHT,
          subtitle_color=C_MUTED, title_size=12, subtitle_size=9, lw=1.5):
    box = FancyBboxPatch((x, y), w, h,
                         boxstyle="round,pad=0.02,rounding_size=0.08",
                         facecolor=face, edgecolor=edge, linewidth=lw)
    ax.add_patch(box)
    if title:
        ax.text(x + w / 2, y + h - 0.12, title, ha="center", va="top",
                fontsize=title_size, color=title_color, fontweight="bold")
    if subtitle:
        ax.text(x + w / 2, y + h / 2 - 0.05, subtitle, ha="center", va="center",
                fontsize=subtitle_size, color=subtitle_color, wrap=True)


def _arrow(ax, p1, p2, color=C_ACCENT, lw=1.8, style="->", curve=0):
    arr = FancyArrowPatch(p1, p2, arrowstyle=style,
                          mutation_scale=14, color=color, linewidth=lw,
                          connectionstyle=f"arc3,rad={curve}")
    ax.add_patch(arr)


# ────────────────────────────────────────────────────────────────
# G01 封面 —— 抽象科技感背景 + 标题
# ────────────────────────────────────────────────────────────────
def g01_cover():
    fig, ax = plt.subplots(figsize=(16, 9))
    ax.set_xlim(0, 16); ax.set_ylim(0, 9)
    ax.set_facecolor(C_BG); fig.patch.set_facecolor(C_BG)
    ax.axis("off")
    # 左：剪影
    silhouette = np.array([
        [3.2, 1.8], [3.6, 1.8], [3.8, 2.6], [3.4, 3.4], [3.2, 4.4],
        [3.1, 5.2], [3.0, 6.2], [3.2, 6.8], [3.6, 7.0], [3.5, 6.2],
        [3.4, 5.2], [3.6, 4.4], [3.8, 3.4], [4.2, 2.6], [4.6, 1.8],
        [4.2, 1.8]
    ])
    ax.add_patch(Polygon(silhouette, closed=True, facecolor="#2a3a55", alpha=0.8, zorder=2))
    # 右：骨架点 + 连线
    joints = {
        "head": (11, 7), "neck": (11, 6.2), "lsh": (10.2, 6.0), "rsh": (11.8, 6.0),
        "lel": (9.6, 5.0), "rel": (12.4, 5.0), "lwr": (9.2, 4.0), "rwr": (12.8, 4.0),
        "lhip": (10.4, 4.6), "rhip": (11.6, 4.6), "lkn": (10.2, 3.0), "rkn": (11.8, 3.0),
        "lank": (10.0, 1.4), "rank": (12.0, 1.4),
    }
    bones = [("head", "neck"), ("neck", "lsh"), ("neck", "rsh"), ("lsh", "lel"),
             ("lel", "lwr"), ("rsh", "rel"), ("rel", "rwr"), ("neck", "lhip"),
             ("neck", "rhip"), ("lhip", "lkn"), ("lkn", "lank"), ("rhip", "rkn"),
             ("rkn", "rank"), ("lhip", "rhip")]
    for a, b in bones:
        x1, y1 = joints[a]; x2, y2 = joints[b]
        ax.plot([x1, x2], [y1, y2], color=C_PRIMARY, lw=2.2, alpha=0.75, zorder=3)
    for p in joints.values():
        ax.plot(*p, "o", color=C_ACCENT, markersize=10, zorder=4)
    # 波形
    xs = np.linspace(8.5, 13.5, 400)
    wave = 0.3 * np.sin(xs * 4) * np.exp(-(xs - 11)**2 / 12) + \
           0.15 * np.sin(xs * 11) * np.exp(-(xs - 11)**2 / 10)
    ax.plot(xs, wave + 2.2, color=C_DANGER, lw=1.8, alpha=0.85, zorder=3)
    ax.plot(xs, wave * 0.6 + 2.0, color=C_SUCCESS, lw=1.4, alpha=0.7, zorder=3)
    # 标题
    ax.text(8, 8.0, "IronBuddy", ha="center", va="center",
            fontsize=58, color=C_ACCENT, fontweight="bold", alpha=0.95)
    ax.text(8, 0.75, "端云协同 · 双模态感知 · 嵌入式健身教练",
            ha="center", va="center", fontsize=18, color=C_TEXT_LIGHT, alpha=0.85)
    ax.text(8, 0.25, "2026-04 · 嵌入式系统综合实验",
            ha="center", va="center", fontsize=11, color=C_MUTED)
    _save(fig, "G01_cover")


# ────────────────────────────────────────────────────────────────
# G02 目录五边形闭环
# ────────────────────────────────────────────────────────────────
def g02_toc():
    fig, ax = plt.subplots(figsize=(14, 8))
    ax.set_xlim(0, 14); ax.set_ylim(0, 8)
    ax.axis("off"); ax.set_facecolor(C_BG); fig.patch.set_facecolor(C_BG)

    cx, cy, R = 7, 4, 2.6
    labels = ["痛点\n背景", "硬件\n通信", "算法\n心路", "落地\n实证", "总结\n展望"]
    colors = [C_DANGER, C_PRIMARY, C_ACCENT, C_SUCCESS, "#9b59b6"]
    angles = [90 + i * 72 for i in range(5)]
    pts = []
    for a, l, c in zip(angles, labels, colors):
        x = cx + R * math.cos(math.radians(a))
        y = cy + R * math.sin(math.radians(a))
        pts.append((x, y))
        circ = Circle((x, y), 0.72, facecolor=c, edgecolor=C_TEXT_LIGHT, lw=2.2, zorder=3)
        ax.add_patch(circ)
        ax.text(x, y, l, ha="center", va="center", fontsize=13,
                color=C_TEXT_LIGHT, fontweight="bold", zorder=4)
    for i in range(5):
        p1 = pts[i]; p2 = pts[(i + 1) % 5]
        _arrow(ax, p1, p2, color=C_MUTED, lw=1.6, curve=0.15)
    ax.text(cx, cy, "IronBuddy\n技术叙事", ha="center", va="center",
            fontsize=17, color=C_ACCENT, fontweight="bold")
    ax.text(cx, 7.3, "汇报目录", ha="center", va="top",
            fontsize=22, color=C_TEXT_LIGHT, fontweight="bold")
    _save(fig, "G02_toc")


# ────────────────────────────────────────────────────────────────
# G03 CV 盲区对比图
# ────────────────────────────────────────────────────────────────
def g03_cv_blind():
    fig, axes = plt.subplots(1, 2, figsize=(14, 7))
    fig.patch.set_facecolor(C_BG)
    titles = ["纯视觉：骨架姿态合规", "双模态：肌电透视深层代偿"]
    for idx, ax in enumerate(axes):
        ax.set_xlim(-1, 4); ax.set_ylim(-0.5, 7)
        ax.set_facecolor(C_CARD); ax.set_aspect("equal")
        ax.set_xticks([]); ax.set_yticks([])
        for sp in ax.spines.values():
            sp.set_edgecolor(C_BORDER); sp.set_linewidth(1.5)
        ax.set_title(titles[idx], color=C_TEXT_LIGHT, fontsize=13, pad=14)
        joints = {
            "head": (1.5, 6.2), "neck": (1.5, 5.4), "lsh": (0.8, 5.2), "rsh": (2.2, 5.2),
            "lel": (0.5, 4.0), "rel": (2.5, 4.0), "lhip": (1.1, 3.8), "rhip": (1.9, 3.8),
            "lkn": (0.9, 2.2), "rkn": (2.1, 2.2), "lank": (0.7, 0.3), "rank": (2.3, 0.3),
        }
        bones = [("head","neck"),("neck","lsh"),("neck","rsh"),("lsh","lel"),
                 ("rsh","rel"),("neck","lhip"),("neck","rhip"),("lhip","rhip"),
                 ("lhip","lkn"),("lkn","lank"),("rhip","rkn"),("rkn","rank")]
        for a, b in bones:
            x1, y1 = joints[a]; x2, y2 = joints[b]
            ax.plot([x1,x2],[y1,y2], color=C_SUCCESS, lw=2.5, alpha=0.9, zorder=3)
        for p in joints.values():
            ax.plot(*p, "o", color=C_ACCENT, markersize=9, zorder=4)
        if idx == 1:
            # 叠加代偿肌群红色热力
            heat_spots = [((1.5, 2.3), 0.8, 0.9, C_DANGER, "腓肠代偿↑"),
                          ((1.5, 4.1), 0.7, 0.3, C_PRIMARY, "股四无力↓")]
            for (x,y), r, alpha, col, lab in heat_spots:
                ax.add_patch(Circle((x,y), r, facecolor=col, alpha=alpha, zorder=2, edgecolor="none"))
                ax.text(x, y - r - 0.3, lab, ha="center", color=col, fontsize=11,
                        fontweight="bold", zorder=5)
            ax.text(1.5, -0.3, "✗ 看上去一样，内部完全不同",
                    ha="center", color=C_DANGER, fontsize=11, fontweight="bold")
        else:
            ax.text(1.5, -0.3, "✓ 姿态合规，但肌群状态未知",
                    ha="center", color=C_MUTED, fontsize=11)
    plt.suptitle("G03 · CV 感知盲区", color=C_TEXT_LIGHT, fontsize=16, y=0.98, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.94])
    _save(fig, "G03_cv_blind")


# ────────────────────────────────────────────────────────────────
# G04 系统分层拓扑
# ────────────────────────────────────────────────────────────────
def g04_architecture():
    fig, ax = plt.subplots(figsize=(15, 9))
    ax.set_xlim(0, 15); ax.set_ylim(0, 9)
    ax.axis("off"); ax.set_facecolor(C_BG); fig.patch.set_facecolor(C_BG)

    layers = [
        ("硬件层", 0.3, "#2d3e54", ["RK3399ProX NPU", "ESP32 MCU", "sEMG 差分贴片×2", "ES7243 麦阵列", "HDMI 屏"]),
        ("通信层", 1.9, "#1f4e79", ["WiFi UDP 透传", "/dev/shm + atomic rename", "SSH 隧道（云端）"]),
        ("感知层", 3.5, "#2d5f3f", ["YOLOv5-Pose NPU", "RTMPose RTX5090", "Biquad + FFT + RMS"]),
        ("认知层", 5.1, "#6b4e1f", ["FSM 状态机", "GRU 7D 三头", "DeepSeek 实时", "OpenClaw 常驻"]),
        ("交互层", 6.7, "#5c2d54", ["HDMI 零延迟", "PWA + MJPEG", "百度 AipSpeech", "飞书推送"]),
    ]
    for name, y, col, items in layers:
        ax.add_patch(FancyBboxPatch((0.5, y), 14, 1.2,
                                    boxstyle="round,pad=0.02,rounding_size=0.06",
                                    facecolor=col, edgecolor=C_ACCENT, lw=1.5, alpha=0.9))
        ax.text(1.0, y + 0.6, name, fontsize=16, color=C_ACCENT, fontweight="bold", va="center")
        step = 12.0 / len(items)
        for i, it in enumerate(items):
            bx = 3 + i * step + step * 0.05
            bw = step * 0.9
            ax.add_patch(FancyBboxPatch((bx, y + 0.2), bw, 0.8,
                                        boxstyle="round,pad=0.015,rounding_size=0.04",
                                        facecolor=C_CARD, edgecolor=C_BORDER, lw=1.0))
            ax.text(bx + bw / 2, y + 0.6, it, ha="center", va="center",
                    fontsize=9.5, color=C_TEXT_LIGHT)
    # 层间箭头
    for i in range(4):
        _arrow(ax, (7.5, layers[i][1] + 1.2), (7.5, layers[i + 1][1]),
               color=C_ACCENT, lw=2.5)
    ax.text(7.5, 8.5, "IronBuddy 五层架构", ha="center", va="center",
            fontsize=18, color=C_TEXT_LIGHT, fontweight="bold")
    _save(fig, "G04_architecture")


# ────────────────────────────────────────────────────────────────
# G05 通信拓扑双图
# ────────────────────────────────────────────────────────────────
def g05_comm_topology():
    fig, axes = plt.subplots(1, 2, figsize=(16, 8))
    fig.patch.set_facecolor(C_BG)

    # 左：WiFi UDP 链路
    ax = axes[0]
    ax.set_xlim(0, 10); ax.set_ylim(0, 8); ax.axis("off"); ax.set_facecolor(C_BG)
    ax.set_title("对外：WiFi UDP 透传", color=C_ACCENT, fontsize=15, fontweight="bold", pad=10)
    _card(ax, 0.5, 5, 3, 1.8, "ESP32", "肌电 ASCII 封包\n~1 kHz 采样", face="#1f4e79")
    _card(ax, 6.5, 5, 3, 1.8, "RK3399ProX", "AP 热点接收\n+ 外网 LLM", face="#2d5f3f")
    # UDP 包
    for i, x in enumerate([3.8, 4.7, 5.6]):
        ax.add_patch(FancyBboxPatch((x - 0.3, 5.6), 0.6, 0.6,
                                    boxstyle="round,pad=0.01",
                                    facecolor=C_ACCENT, edgecolor="none", alpha=0.9 - i * 0.2))
        ax.text(x, 5.9, "UDP", ha="center", va="center", fontsize=8, color=C_TEXT_DARK, fontweight="bold")
    _arrow(ax, (3.5, 5.9), (6.5, 5.9), color=C_ACCENT, lw=2.5)
    ax.text(5, 4.5, "16-bit ASCII · < 1 ms RTT · 零握手开销",
            ha="center", color=C_TEXT_LIGHT, fontsize=10)
    ax.text(5, 3.5, "为什么抛弃 BLE：", ha="center", color=C_MUTED, fontsize=11, fontweight="bold")
    ax.text(5, 2.8, "• 4 通道 1 kHz 即撑爆带宽", ha="center", color=C_MUTED, fontsize=10)
    ax.text(5, 2.2, "• 掉包 / 连接不稳", ha="center", color=C_MUTED, fontsize=10)
    ax.text(5, 1.6, "• 双网卡架构：AP 接肌电 + 外网接 LLM", ha="center", color=C_MUTED, fontsize=10)

    # 右：/dev/shm 辐射图
    ax = axes[1]
    ax.set_xlim(0, 10); ax.set_ylim(0, 8); ax.axis("off"); ax.set_facecolor(C_BG)
    ax.set_title("对内：/dev/shm 原子 IPC", color=C_ACCENT, fontsize=15, fontweight="bold", pad=10)
    cx, cy = 5, 4.2
    ax.add_patch(Circle((cx, cy), 1.0, facecolor=C_ACCENT, edgecolor=C_TEXT_LIGHT, lw=2))
    ax.text(cx, cy + 0.15, "/dev/shm", ha="center", va="center", fontsize=12,
            color=C_TEXT_DARK, fontweight="bold")
    ax.text(cx, cy - 0.25, "atomic rename", ha="center", va="center", fontsize=9, color=C_TEXT_DARK)
    procs = [("vision", 0), ("streamer\n(Flask)", 72), ("fsm", 144),
             ("emg", 216), ("voice", 288)]
    R = 2.8
    for name, a in procs:
        x = cx + R * math.cos(math.radians(a + 90))
        y = cy + R * math.sin(math.radians(a + 90))
        ax.add_patch(Circle((x, y), 0.75, facecolor=C_PRIMARY, edgecolor=C_TEXT_LIGHT, lw=1.8))
        ax.text(x, y, name, ha="center", va="center", fontsize=10, color=C_TEXT_LIGHT, fontweight="bold")
        _arrow(ax, (cx, cy), (x, y), color=C_MUTED, lw=1.2, style="<->")
    ax.text(5, 7.2, "20+ 信号文件：pose_data / fsm_state /",
            ha="center", color=C_TEXT_LIGHT, fontsize=10)
    ax.text(5, 6.8, "muscle_activation / vision_mode / chat_input ...",
            ha="center", color=C_TEXT_LIGHT, fontsize=10)
    ax.text(5, 0.8, "绕过 Python GIL → HDMI 后 Flask CPU 30% → <5%",
            ha="center", color=C_SUCCESS, fontsize=10, fontweight="bold")
    _save(fig, "G05_comm_topology")


# ────────────────────────────────────────────────────────────────
# G06 双视觉引擎
# ────────────────────────────────────────────────────────────────
def g06_vision_dual():
    fig, ax = plt.subplots(figsize=(14, 8))
    ax.set_xlim(0, 14); ax.set_ylim(0, 8); ax.axis("off"); ax.set_facecolor(C_BG); fig.patch.set_facecolor(C_BG)
    ax.text(7, 7.5, "视觉双引擎：热切换架构", ha="center", fontsize=17, color=C_TEXT_LIGHT, fontweight="bold")

    _card(ax, 0.5, 3, 2.2, 2, "摄像头", "HDMI 直连", face=C_CARD)
    # 本地 NPU 路径
    ax.text(4, 6.5, "本地 NPU（默认）", ha="center", fontsize=13, color=C_PRIMARY, fontweight="bold")
    _card(ax, 3, 4.5, 2.5, 1.6, "YOLOv5-Pose", "RKNN uint8\nconf=0.08", face="#1f4e79")
    _card(ax, 6, 4.5, 2.2, 1.6, "~107 ms/帧", "零延迟 HDMI", face="#2d5f3f")
    _arrow(ax, (2.7, 4.2), (3, 5.3), color=C_PRIMARY, lw=2)
    _arrow(ax, (5.5, 5.3), (6, 5.3), color=C_PRIMARY, lw=2)
    # 云端 GPU 路径
    ax.text(4, 2.2, "云端 GPU（可切）", ha="center", fontsize=13, color=C_ACCENT, fontweight="bold")
    _card(ax, 3, 0.4, 2.5, 1.6, "RTMPose-m", "ONNX FP32\nRTX 5090", face="#6b4e1f")
    _card(ax, 6, 0.4, 2.2, 1.6, "~30 ms+网络", "高精度兜底", face="#2d5f3f")
    _arrow(ax, (2.7, 3.8), (3, 1.2), color=C_ACCENT, lw=2)
    _arrow(ax, (5.5, 1.2), (6, 1.2), color=C_ACCENT, lw=2)

    # 热切换控制
    _card(ax, 9.5, 3, 4, 2, "/dev/shm/vision_mode.json",
          '{"mode":"local"\n     /"cloud"}', face=C_ACCENT, title_color=C_TEXT_DARK,
          subtitle_color=C_TEXT_DARK, title_size=11)
    _arrow(ax, (8.2, 5.3), (9.5, 4.5), color=C_MUTED, lw=1.2)
    _arrow(ax, (8.2, 1.2), (9.5, 3.5), color=C_MUTED, lw=1.2)
    ax.text(11.5, 2.6, "前端写文件即切换\n云端超时自动降级 NPU",
            ha="center", color=C_MUTED, fontsize=10)
    _save(fig, "G06_vision_dual")


# ────────────────────────────────────────────────────────────────
# G07 Mid-Fusion 流水线
# ────────────────────────────────────────────────────────────────
def g07_mid_fusion():
    fig, ax = plt.subplots(figsize=(15, 8))
    ax.set_xlim(0, 15); ax.set_ylim(0, 8); ax.axis("off"); ax.set_facecolor(C_BG); fig.patch.set_facecolor(C_BG)
    ax.text(7.5, 7.5, "Mid-Fusion 中继融合流水线", ha="center",
            fontsize=17, color=C_TEXT_LIGHT, fontweight="bold")
    # 视觉流
    y1 = 5.2
    _card(ax, 0.3, y1, 2.2, 1.3, "摄像头", "1080p", face=C_CARD)
    _card(ax, 2.9, y1, 2.5, 1.3, "YOLOv5-Pose", "17 关键点", face="#1f4e79")
    _card(ax, 5.9, y1, 2.5, 1.3, "角度 / 相位", "降维 → 标量", face="#2d5f3f")
    for (a, b) in [((2.5, y1+0.65), (2.9, y1+0.65)), ((5.4, y1+0.65), (5.9, y1+0.65))]:
        _arrow(ax, a, b, color=C_PRIMARY, lw=2)
    ax.text(1.4, y1 + 1.5, "视觉分支", ha="center", color=C_PRIMARY, fontsize=12, fontweight="bold")

    # 肌电流
    y2 = 2.0
    _card(ax, 0.3, y2, 2.2, 1.3, "ESP32 sEMG", "2 通道 1kHz", face=C_CARD)
    _card(ax, 2.9, y2, 2.5, 1.3, "Biquad 带通", "20/50/150 Hz", face="#6b4e1f")
    _card(ax, 5.9, y2, 2.5, 1.3, "FFT + RMS", "频域+能量", face="#2d5f3f")
    for (a, b) in [((2.5, y2+0.65), (2.9, y2+0.65)), ((5.4, y2+0.65), (5.9, y2+0.65))]:
        _arrow(ax, a, b, color=C_DANGER, lw=2)
    ax.text(1.4, y2 + 1.5, "肌电分支", ha="center", color=C_DANGER, fontsize=12, fontweight="bold")

    # 中继融合
    _card(ax, 9, 3.4, 2.4, 1.8, "7D 特征向量",
          "角度 / 角速 / 角加速\n目标RMS / 代偿RMS\n对称分 / 相位进度",
          face=C_ACCENT, title_color=C_TEXT_DARK, subtitle_color=C_TEXT_DARK, title_size=11, subtitle_size=9)
    _arrow(ax, (8.4, y1 + 0.65), (9, 5.0), color=C_PRIMARY, lw=2, curve=-0.2)
    _arrow(ax, (8.4, y2 + 0.65), (9, 3.6), color=C_DANGER, lw=2, curve=0.2)

    # GRU 三头
    _card(ax, 12, 2.8, 2.7, 3.0, "GRU(hidden=16)",
          "1488 参数\n<1 MB\n\n3 输出头：\n相似度 / 分类 / 相位",
          face="#5c2d54", title_color=C_TEXT_LIGHT, subtitle_color=C_TEXT_LIGHT, title_size=12)
    _arrow(ax, (11.4, 4.3), (12, 4.3), color=C_ACCENT, lw=2.5)

    # 底部对比
    ax.text(7.5, 0.7, "对比端到端：4 GB RAM 几秒 OOM | Mid-Fusion：CPU 微秒级推理",
            ha="center", color=C_SUCCESS, fontsize=11, fontweight="bold")
    _save(fig, "G07_mid_fusion")


# ────────────────────────────────────────────────────────────────
# G08 数据集决策树
# ────────────────────────────────────────────────────────────────
def g08_dataset_tree():
    fig, ax = plt.subplots(figsize=(16, 8))
    ax.set_xlim(0, 16); ax.set_ylim(0, 8); ax.axis("off"); ax.set_facecolor(C_BG); fig.patch.set_facecolor(C_BG)
    ax.text(8, 7.5, "数据集抉择：3 天的时间线", ha="center",
            fontsize=17, color=C_TEXT_LIGHT, fontweight="bold")
    # 时间轴
    ax.plot([0.5, 15.5], [6.5, 6.5], color=C_MUTED, lw=2)
    for x, d in [(1, "4/17"), (5, "4/18 上午"), (8, "4/18 中午"),
                 (11, "4/18 下午"), (14, "4/19")]:
        ax.plot(x, 6.5, "o", color=C_ACCENT, markersize=10)
        ax.text(x, 6.8, d, ha="center", color=C_MUTED, fontsize=10, fontweight="bold")

    def fail_card(x, y, name, reason):
        _card(ax, x - 1.1, y - 0.8, 2.2, 1.6, name, reason, face="#4a1f1f",
              title_color=C_DANGER, subtitle_color=C_TEXT_LIGHT, title_size=11, subtitle_size=9)
        ax.plot([x - 0.4, x + 0.4], [y - 0.4, y + 0.4], color=C_DANGER, lw=3)
        ax.plot([x - 0.4, x + 0.4], [y + 0.4, y - 0.4], color=C_DANGER, lw=3)

    def ok_card(x, y, name, reason):
        _card(ax, x - 1.1, y - 0.8, 2.2, 1.6, name, reason, face="#1f4a1f",
              title_color=C_SUCCESS, subtitle_color=C_TEXT_LIGHT, title_size=11, subtitle_size=9)

    fail_card(1, 5, "Ninapro DB2", "手部抓握\n任务不对口")
    fail_card(5, 5, "FLEX (NeurIPS25)", "License 24-72h\n明日等不到")
    fail_card(8, 5, "EMAHA-DB", "是步态ADL\n不是弯举")
    ok_card(11, 5, "MIA (ICCV23)", "964 clip 下肢\n→ 深蹲可用")
    ok_card(14, 5, "本地自采\n+ 10× augment", "弯举兜底\nval acc 94→100%")

    for i, (x1, x2) in enumerate([(1, 5), (5, 8), (8, 11), (11, 14)]):
        _arrow(ax, (x1 + 1.2, 5), (x2 - 1.2, 5), color=C_MUTED, lw=1.5)

    ax.text(8, 2.5, "教训：业界缺乏「错误动作 + 同步肌电」数据集",
            ha="center", color=C_ACCENT, fontsize=13, fontweight="bold")
    ax.text(8, 1.8, "本地化不是妥协 —— 反而与 ESP32 廉价硬件域完美对齐",
            ha="center", color=C_TEXT_LIGHT, fontsize=11)
    ax.text(8, 1.1, "深蹲：MIA 预训练 val acc 94.4%  ·  弯举：10× augment + 动态 MVC",
            ha="center", color=C_SUCCESS, fontsize=11)
    _save(fig, "G08_dataset_tree")


# ────────────────────────────────────────────────────────────────
# G09 双轨 LLM
# ────────────────────────────────────────────────────────────────
def g09_dual_llm():
    fig, ax = plt.subplots(figsize=(15, 8))
    ax.set_xlim(0, 15); ax.set_ylim(0, 8); ax.axis("off"); ax.set_facecolor(C_BG); fig.patch.set_facecolor(C_BG)
    ax.text(7.5, 7.5, "双轨双核 LLM：实时 × 常驻", ha="center",
            fontsize=17, color=C_TEXT_LIGHT, fontweight="bold")

    # 上轨 DeepSeek
    _card(ax, 0.5, 5, 2.5, 1.3, "语音 / 按钮", "疲劳 1500", face=C_CARD)
    _card(ax, 4, 5, 2.5, 1.3, "DeepSeek", "短视 REST + SSE", face="#1f4e79",
          subtitle_color=C_TEXT_LIGHT)
    _card(ax, 7.5, 5, 2.5, 1.3, "TTS", "≤ 2 s", face="#2d5f3f")
    _card(ax, 11, 5, 2.5, 1.3, "飞书 App", "训练总结", face="#6b4e1f")
    for (a, b) in [((3, 5.65), (4, 5.65)), ((6.5, 5.65), (7.5, 5.65)),
                   ((10, 5.65), (11, 5.65))]:
        _arrow(ax, a, b, color=C_PRIMARY, lw=2)
    ax.text(1.7, 6.6, "前端实时（延迟敏感）", color=C_PRIMARY, fontsize=12,
            fontweight="bold")

    # 下轨 OpenClaw
    _card(ax, 0.5, 1.5, 2.5, 1.3, "Cron 触发", "09/20/23 点", face=C_CARD)
    _card(ax, 4, 1.5, 2.5, 1.3, "OpenClaw", "常驻 asyncio", face="#5c2d54",
          subtitle_color=C_TEXT_LIGHT)
    _card(ax, 7.5, 1.5, 2.5, 1.3, "全量上下文", "14 日 llm_log", face="#6b4e1f")
    _card(ax, 11, 1.5, 2.5, 1.3, "飞书 Webhook", "日 / 周报", face="#2d5f3f")
    for (a, b) in [((3, 2.15), (4, 2.15)), ((6.5, 2.15), (7.5, 2.15)),
                   ((10, 2.15), (11, 2.15))]:
        _arrow(ax, a, b, color=C_ACCENT, lw=2)
    ax.text(1.7, 3.1, "后端常驻（无延迟约束）", color=C_ACCENT, fontsize=12,
            fontweight="bold")

    # 中间 SQLite 桥
    _card(ax, 6, 3.3, 3, 0.9, "SQLite · 8 张表", "training_sessions / rep_events / llm_log / preference_history ...",
          face=C_ACCENT, title_color=C_TEXT_DARK, subtitle_color=C_TEXT_DARK,
          title_size=10, subtitle_size=7)
    _arrow(ax, (7.5, 5), (7.5, 4.2), color=C_MUTED, style="<->", lw=1.5)
    _arrow(ax, (7.5, 3.3), (7.5, 2.8), color=C_MUTED, style="<->", lw=1.5)

    ax.text(7.5, 0.5, "分工清晰：一个 LLM 做不了实时陪练 + 长期记忆两件事",
            ha="center", color=C_SUCCESS, fontsize=11, fontweight="bold")
    _save(fig, "G09_dual_llm")


# ────────────────────────────────────────────────────────────────
# G10 5 进程 × /dev/shm IPC 网状
# ────────────────────────────────────────────────────────────────
def g10_ipc_mesh():
    fig, ax = plt.subplots(figsize=(12, 10))
    ax.set_xlim(-6, 6); ax.set_ylim(-6, 6); ax.axis("off"); ax.set_facecolor(C_BG); fig.patch.set_facecolor(C_BG)
    ax.text(0, 5.5, "5 进程 × /dev/shm 原子 IPC 网状", ha="center",
            fontsize=17, color=C_TEXT_LIGHT, fontweight="bold")

    # 中心
    ax.add_patch(Circle((0, 0), 1.3, facecolor=C_ACCENT, edgecolor=C_TEXT_LIGHT, lw=2.5))
    ax.text(0, 0.2, "/dev/shm", ha="center", va="center", fontsize=13,
            color=C_TEXT_DARK, fontweight="bold")
    ax.text(0, -0.25, "20+ JSON", ha="center", va="center", fontsize=10, color=C_TEXT_DARK)

    procs = [
        ("vision", C_PRIMARY, ["pose_data.json", "result.jpg", "hdmi_status.json"]),
        ("streamer\n(Flask)", "#5c2d54", ["trigger_deepseek", "fsm_reset"]),
        ("fsm", C_SUCCESS, ["fsm_state.json", "llm_reply.txt", "chat_reply.txt"]),
        ("emg", C_DANGER, ["muscle_activation.json", "emg_heartbeat"]),
        ("voice", "#d4a04a", ["chat_input.txt", "mute_signal", "exercise_mode.json"]),
    ]
    R = 3.8
    for i, (name, col, files) in enumerate(procs):
        a = 90 - i * 72
        x = R * math.cos(math.radians(a)); y = R * math.sin(math.radians(a))
        ax.add_patch(Circle((x, y), 0.85, facecolor=col, edgecolor=C_TEXT_LIGHT, lw=1.8))
        ax.text(x, y, name, ha="center", va="center", fontsize=11,
                color=C_TEXT_LIGHT, fontweight="bold")
        _arrow(ax, (0, 0), (x * 0.78, y * 0.78), color=col, lw=1.5, style="<->")
        # 文件标签
        for j, f in enumerate(files):
            fx = x * 1.35; fy = y * 1.35 - 0.35 + j * 0.28
            ax.text(fx, fy, f, ha="center", va="center", fontsize=7.5,
                    color=C_MUTED, style="italic")
    ax.text(0, -5.2, "atomic rename · 崩一个不影响其他 · HDMI 后 Flask CPU < 5%",
            ha="center", color=C_SUCCESS, fontsize=11, fontweight="bold")
    _save(fig, "G10_ipc_mesh")


# ────────────────────────────────────────────────────────────────
# G11 α-β 映射效果图
# ────────────────────────────────────────────────────────────────
def g11_alpha_beta():
    np.random.seed(42)
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    fig.patch.set_facecolor(C_BG)

    # 原始分布
    n = 400
    esp32 = np.random.gamma(2, 12, n) + 4  # 低幅值集中
    mia = np.random.gamma(2.5, 22, n) + 8  # 高幅值分散
    # alpha beta 映射
    alpha, beta = 2.12, -21.4
    mapped = alpha * esp32 + beta

    for ax in axes:
        ax.set_facecolor(C_CARD)
        for sp in ax.spines.values():
            sp.set_edgecolor(C_BORDER); sp.set_linewidth(1.2)
        ax.tick_params(colors=C_MUTED)
        ax.xaxis.label.set_color(C_TEXT_LIGHT)
        ax.yaxis.label.set_color(C_TEXT_LIGHT)
        ax.title.set_color(C_TEXT_LIGHT)

    axes[0].scatter(range(n), esp32, s=10, c=C_DANGER, alpha=0.55, label="ESP32 实测")
    axes[0].scatter(range(n), mia, s=10, c=C_PRIMARY, alpha=0.55, label="MIA Delsys")
    axes[0].set_title("原始：ESP32 vs MIA（域差异显著）", fontsize=13)
    axes[0].set_xlabel("样本点"); axes[0].set_ylabel("RMS 幅值")
    axes[0].legend(facecolor=C_CARD, edgecolor=C_BORDER, labelcolor=C_TEXT_LIGHT)

    axes[1].scatter(range(n), mapped, s=10, c=C_SUCCESS, alpha=0.55, label=f"映射后 α={alpha} β={beta}")
    axes[1].scatter(range(n), mia, s=10, c=C_PRIMARY, alpha=0.55, label="MIA Delsys")
    axes[1].set_title("α·x+β 映射后：p05/p95 对齐", fontsize=13)
    axes[1].set_xlabel("样本点"); axes[1].set_ylabel("RMS 幅值")
    axes[1].legend(facecolor=C_CARD, edgecolor=C_BORDER, labelcolor=C_TEXT_LIGHT)

    plt.suptitle("G11 · 硬件域对齐：不动权重，输入侧骗过模型",
                 color=C_ACCENT, fontsize=15, y=1.0, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.95])
    _save(fig, "G11_alpha_beta")


# ────────────────────────────────────────────────────────────────
# G12 技术栈金字塔
# ────────────────────────────────────────────────────────────────
def g12_pyramid():
    fig, ax = plt.subplots(figsize=(14, 9))
    ax.set_xlim(0, 14); ax.set_ylim(0, 9); ax.axis("off"); ax.set_facecolor(C_BG); fig.patch.set_facecolor(C_BG)
    ax.text(7, 8.4, "全栈技术闭环", ha="center", fontsize=18, color=C_TEXT_LIGHT, fontweight="bold")
    layers = [
        ("应用层", "PWA · SSE 流式 · 飞书 · HDMI 全屏", "#5c2d54", 7.5),
        ("系统层", "5 进程 · /dev/shm · systemd-ready", "#6b4e1f", 6.2),
        ("AI 层",  "YOLOv5-Pose RKNN · GRU 7D 三头 · DeepSeek SSE", "#2d5f3f", 4.9),
        ("信号层", "Biquad IIR · FFT · 滑动 RMS · MVC 校准 · α·β 对齐", "#1f4e79", 3.6),
        ("硬件层", "RK3399ProX NPU · ESP32 C · 差分 PCB · 锂电", "#2d3e54", 2.3),
    ]
    # 金字塔形宽度递减
    widths = [5.5, 7.0, 9.0, 11.0, 13.0]
    for (name, stack, col, y), w in zip(layers, widths):
        x = (14 - w) / 2
        ax.add_patch(FancyBboxPatch((x, y - 0.5), w, 1.0,
                                    boxstyle="round,pad=0.02,rounding_size=0.06",
                                    facecolor=col, edgecolor=C_ACCENT, lw=1.5, alpha=0.92))
        ax.text(x + 0.4, y, name, fontsize=14, color=C_ACCENT,
                fontweight="bold", va="center")
        ax.text(7, y, stack, ha="center", va="center", fontsize=11, color=C_TEXT_LIGHT)
    ax.text(7, 1.0, "从 C 语言电路到 PWA 前端：全栈贯通",
            ha="center", color=C_SUCCESS, fontsize=12, fontweight="bold")
    _save(fig, "G12_pyramid")


# ────────────────────────────────────────────────────────────────
# G13 行业展望三联
# ────────────────────────────────────────────────────────────────
def g13_outlook():
    fig, axes = plt.subplots(1, 3, figsize=(16, 6))
    fig.patch.set_facecolor(C_BG)
    specs = [
        ("柔性印刷电极阵列", "从 2 通道 → N 通道阵列\n覆盖全身肌群网络\n术后复健无创量化",
         C_PRIMARY, "🩺"),
        ("开源大规模数据集", "错误动作 + 同步肌电\n本项目 augment 沙盒\n即为雏形",
         C_ACCENT, "📊"),
        ("边缘端侧大模型", "期待 7B/14B 模型\n量化落地 NPU\n腰包即私人教练",
         C_SUCCESS, "🧠"),
    ]
    for ax, (title, body, col, icon) in zip(axes, specs):
        ax.set_facecolor(C_CARD); ax.set_xlim(0, 10); ax.set_ylim(0, 10); ax.axis("off")
        for sp in ax.spines.values():
            sp.set_edgecolor(C_BORDER); sp.set_linewidth(1.5)
        ax.add_patch(FancyBboxPatch((0.3, 0.3), 9.4, 9.4,
                                    boxstyle="round,pad=0.02,rounding_size=0.1",
                                    facecolor=C_CARD, edgecolor=col, lw=2.5, alpha=0.95))
        ax.text(5, 8.3, icon, ha="center", fontsize=50)
        ax.text(5, 6, title, ha="center", fontsize=15, color=col, fontweight="bold")
        ax.text(5, 3.5, body, ha="center", fontsize=11, color=C_TEXT_LIGHT, linespacing=1.6)
    plt.suptitle("行业展望：双模态感知的 3 条前路",
                 color=C_ACCENT, fontsize=16, y=0.98, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.93])
    _save(fig, "G13_outlook")


# ────────────────────────────────────────────────────────────────
# G14 结束页 Thanks
# ────────────────────────────────────────────────────────────────
def g14_thanks():
    fig, ax = plt.subplots(figsize=(16, 9))
    ax.set_xlim(0, 16); ax.set_ylim(0, 9); ax.axis("off"); ax.set_facecolor(C_BG); fig.patch.set_facecolor(C_BG)
    ax.text(8, 5.5, "Thanks", ha="center", va="center", fontsize=100,
            color=C_ACCENT, fontweight="bold", alpha=0.95)
    ax.text(8, 3.3, "感谢评审老师与同组同学的指导",
            ha="center", fontsize=20, color=C_TEXT_LIGHT)
    ax.text(8, 2.3, "github.com/qqyyqq812/IronBuddy",
            ha="center", fontsize=14, color=C_MUTED, style="italic")
    ax.text(8, 1.2, "Q & A",
            ha="center", fontsize=26, color=C_PRIMARY, fontweight="bold")
    _save(fig, "G14_thanks")


# ────────────────────────────────────────────────────────────────
# S01-S08 占位图
# ────────────────────────────────────────────────────────────────
def placeholders():
    specs = [
        ("S01", "PCB 实物（俯视）", "待棚拍 · 双路差分采集板 + ESP32 + 电池仓"),
        ("S02", "穿戴腰包佩戴图", "待棚拍 · 模特正面半身 · 线缆可见"),
        ("S03", "RK3399ProX 板端", "待棚拍 · 接口特写"),
        ("S04", "HDMI 实时骨架", "待现场拍 · 蹲底瞬间抓帧"),
        ("S05", "/database 页面", "待截图 · 8 表 Tab + voice_sessions 条目"),
        ("S06", "MVC 校准 UI", "待截图 · 3.5s 进度 + 结果回显"),
        ("S07", "PWA 完整前端", "待截图 · 视频 + HUD + 4 Tab + 状态栏"),
        ("S08", "语音命令联动", "待截图 · voice_daemon 日志 + UI 同步"),
    ]
    for code, name, hint in specs:
        fig, ax = plt.subplots(figsize=(12, 7))
        ax.set_xlim(0, 12); ax.set_ylim(0, 7); ax.axis("off")
        ax.set_facecolor("#ffffff"); fig.patch.set_facecolor("#ffffff")
        # 虚线占位边框
        ax.add_patch(Rectangle((0.5, 0.5), 11, 6, facecolor="#f0f2f5",
                               edgecolor=C_MUTED, linewidth=2, linestyle="--"))
        # 图标
        ax.text(6, 4.5, "📷", ha="center", fontsize=80)
        ax.text(6, 3.0, code, ha="center", fontsize=42, color="#2d3e54", fontweight="bold")
        ax.text(6, 2.1, name, ha="center", fontsize=18, color="#4a5568")
        ax.text(6, 1.3, hint, ha="center", fontsize=11, color="#718096", style="italic")
        ax.text(6, 0.75, "【明早替换此占位图】", ha="center",
                fontsize=10, color=C_DANGER, fontweight="bold")
        _save(fig, code, bg="#ffffff")


if __name__ == "__main__":
    print("Building figures...")
    g01_cover()
    g02_toc()
    g03_cv_blind()
    g04_architecture()
    g05_comm_topology()
    g06_vision_dual()
    g07_mid_fusion()
    g08_dataset_tree()
    g09_dual_llm()
    g10_ipc_mesh()
    g11_alpha_beta()
    g12_pyramid()
    g13_outlook()
    g14_thanks()
    print("\nPlaceholders...")
    placeholders()
    print("\nAll done. Output: ", ASSETS)
