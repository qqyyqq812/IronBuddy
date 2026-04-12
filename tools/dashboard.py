#!/usr/bin/env python3
"""
IronBuddy 训练可视化面板 — Streamlit
=====================================
4 个标签页:
  1. 数据探索   — 浏览CSV, 按标签筛选, 对比分布
  2. 训练监控   — 读取TensorBoard日志, loss/acc曲线
  3. 模型评估   — 加载模型, 混淆矩阵 + 分类报告
  4. 实时推理   — 连接板端, 实时预测可视化

启动:
    streamlit run tools/dashboard.py
"""
import os
import sys
import glob
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go

_TOOLS_DIR  = Path(__file__).resolve().parent
_ENGINE_DIR = _TOOLS_DIR.parent / "hardware_engine"
_PROJECT    = _TOOLS_DIR.parent
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

# ---------------------------------------------------------------------------
st.set_page_config(page_title="IronBuddy 训练面板", layout="wide")
st.title("IronBuddy GRU 训练可视化面板")

tab1, tab2, tab3, tab4 = st.tabs([
    "1. 数据探索",
    "2. 训练监控",
    "3. 模型评估",
    "4. 实时推理",
])

FEATURE_COLS = ["Ang_Vel", "Angle", "Ang_Accel", "Target_RMS", "Comp_RMS",
                "Symmetry_Score", "Phase_Progress"]
FEAT_CN = {
    "Ang_Vel": "角速度", "Angle": "关节角度", "Ang_Accel": "角加速度",
    "Target_RMS": "目标肌肉EMG", "Comp_RMS": "代偿肌肉EMG",
    "Symmetry_Score": "对称性", "Phase_Progress": "动作阶段",
}
CLASS_NAMES  = ["standard", "compensating", "non_standard"]
CLASS_CN     = {"standard": "标准", "compensating": "代偿/偷懒", "non_standard": "错误"}
LABEL_CN     = {"golden": "标准(golden)", "lazy": "偷懒(lazy)", "bad": "错误(bad)"}
COLORS       = {"golden": "#22c55e", "lazy": "#f59e0b", "bad": "#ef4444",
                "standard": "#22c55e", "compensating": "#f59e0b", "non_standard": "#ef4444"}


def _load_csvs(data_dir):
    pattern = os.path.join(data_dir, "**", "train_*_*.csv")
    paths = sorted(glob.glob(pattern, recursive=True))
    if not paths:
        pattern = os.path.join(data_dir, "**", "*.csv")
        paths = sorted(glob.glob(pattern, recursive=True))
    frames = []
    for p in paths:
        try:
            df = pd.read_csv(p)
            df["_file"] = Path(p).name
            frames.append(df)
        except Exception:
            pass
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# =========================================================================
# 标签页 1: 数据探索
# =========================================================================
with tab1:
    st.header("数据探索")
    st.caption("采集完数据后，在这里检查质量、对比不同动作标签的分布差异")

    data_dir = st.text_input(
        "数据目录",
        value=str(_PROJECT / "data"),
        help="包含 train_*.csv 文件的目录"
    )

    if os.path.isdir(data_dir):
        df = _load_csvs(data_dir)
        if df.empty:
            st.warning(f"在 {data_dir} 中未找到 CSV 文件")
        else:
            st.success(f"已加载 {len(df)} 帧数据，来自 {df['_file'].nunique()} 个文件")

            labels = sorted(df["label"].dropna().unique())
            label_opts = [LABEL_CN.get(l, l) for l in labels]
            selected_display = st.multiselect("按标签筛选", label_opts, default=label_opts)
            rev_map = {v: k for k, v in LABEL_CN.items()}
            selected = [rev_map.get(s, s) for s in selected_display]
            dff = df[df["label"].isin(selected)]

            col1, col2, col3 = st.columns(3)
            col1.metric("总帧数", f"{len(dff):,}")
            col2.metric("文件数", dff["_file"].nunique())
            col3.metric("标签", ", ".join(selected_display))

            # 每个文件的摘要
            st.subheader("各文件摘要")
            summary = dff.groupby(["_file", "label"]).agg(
                帧数=("Angle", "count"),
                角度最小=("Angle", "min"),
                角度最大=("Angle", "max"),
                角度范围=("Angle", lambda x: x.max() - x.min()),
            ).reset_index()
            summary["label"] = summary["label"].map(LABEL_CN).fillna(summary["label"])
            summary = summary.rename(columns={"_file": "文件名", "label": "标签"})
            st.dataframe(summary, use_container_width=True)

            # 特征分布
            st.subheader("特征分布对比")
            st.caption("观察不同标签的特征分布是否有明显差异 — 差异越大，模型越容易学到")
            feat_options = [c for c in FEATURE_COLS if c in dff.columns]
            feat = st.selectbox("选择特征", feat_options,
                                format_func=lambda x: f"{FEAT_CN.get(x, x)} ({x})")
            fig = px.histogram(dff, x=feat, color="label", barmode="overlay",
                               color_discrete_map=COLORS, nbins=50, opacity=0.7,
                               labels={"label": "标签", feat: FEAT_CN.get(feat, feat)})
            st.plotly_chart(fig, use_container_width=True)

            # 时间序列
            st.subheader("时间序列波形")
            st.caption("逐帧查看角度、速度等变化，确认数据是否包含完整的运动周期")
            files = sorted(dff["_file"].unique())
            sel_file = st.selectbox("选择文件", files)
            df_file = dff[dff["_file"] == sel_file].reset_index(drop=True)

            feats_to_plot = st.multiselect(
                "要绘制的列",
                [c for c in FEATURE_COLS if c in df_file.columns],
                default=["Angle", "Ang_Vel"],
                format_func=lambda x: f"{FEAT_CN.get(x, x)}"
            )
            if feats_to_plot:
                fig2 = go.Figure()
                for f in feats_to_plot:
                    fig2.add_trace(go.Scatter(y=df_file[f], name=FEAT_CN.get(f, f), mode="lines"))
                fig2.update_layout(xaxis_title="帧", yaxis_title="数值", height=400)
                st.plotly_chart(fig2, use_container_width=True)

            # 相关性热力图
            st.subheader("特征相关性")
            st.caption("看哪些特征高度相关（冗余）或独立（互补信息）")
            numeric_cols = [c for c in FEATURE_COLS if c in dff.columns]
            corr = dff[numeric_cols].corr()
            corr_display = corr.rename(index=FEAT_CN, columns=FEAT_CN)
            fig3 = px.imshow(corr_display, text_auto=".2f", color_continuous_scale="RdBu_r",
                             zmin=-1, zmax=1)
            st.plotly_chart(fig3, use_container_width=True)
    else:
        st.info(f"目录不存在: {data_dir}。请先采集数据。")


# =========================================================================
# 标签页 2: 训练监控
# =========================================================================
with tab2:
    st.header("训练监控")
    st.caption("训练时自动记录到 TensorBoard，在这里或独立终端查看")
    st.code("tensorboard --logdir models/tb_logs", language="bash")

    tb_dir = st.text_input("TensorBoard 日志目录", value=str(_PROJECT / "models" / "tb_logs"))

    if os.path.isdir(tb_dir):
        try:
            from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
            runs = sorted(glob.glob(os.path.join(tb_dir, "*")))
            if runs:
                sel_run = st.selectbox("训练轮次", [Path(r).name for r in runs])
                ea = EventAccumulator(os.path.join(tb_dir, sel_run))
                ea.Reload()

                tags = ea.Tags().get("scalars", [])
                if tags:
                    tag = st.selectbox("指标", tags)
                    events = ea.Scalars(tag)
                    df_tb = pd.DataFrame([(e.step, e.value) for e in events],
                                         columns=["epoch", "value"])
                    fig = px.line(df_tb, x="epoch", y="value", title=tag,
                                  labels={"epoch": "轮次", "value": "数值"})
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.warning("该轮次中无标量数据")
            else:
                st.warning("未找到训练记录")
        except ImportError:
            st.warning("需要安装 tensorboard: `pip install tensorboard`")
    else:
        st.info("尚无 TensorBoard 日志。请先训练模型。")


# =========================================================================
# 标签页 3: 模型评估
# =========================================================================
with tab3:
    st.header("模型评估")
    st.caption("加载训练好的模型，在全部数据上运行，生成混淆矩阵和分类报告")

    model_path = st.text_input(
        "模型文件 (.pt)",
        value=str(_PROJECT / "models" / "extreme_fusion_gru.pt")
    )
    eval_data_dir = st.text_input(
        "评估数据目录",
        value=str(_PROJECT / "data"),
        key="eval_data"
    )

    if st.button("开始评估") and os.path.isfile(model_path) and os.path.isdir(eval_data_dir):
        with st.spinner("正在加载模型并运行评估..."):
            try:
                import torch
                from cognitive.fusion_model import CompensationGRU, CLASS_NAMES as CN

                model = CompensationGRU(input_size=7)
                model.load_state_dict(torch.load(model_path, map_location="cpu"))
                model.eval()

                df_eval = _load_csvs(eval_data_dir)
                if df_eval.empty:
                    st.error("未找到数据")
                else:
                    all_preds, all_labels, all_sims = [], [], []

                    for _, group in df_eval.groupby("_file"):
                        if len(group) < 31:
                            continue
                        label_str = group["label"].iloc[0]
                        label_idx = {"golden": 0, "lazy": 1, "bad": 2}.get(label_str)
                        if label_idx is None:
                            continue

                        feats = group[FEATURE_COLS].values.astype(np.float32)
                        feats[:, 1] /= 180.0
                        feats[:, 2] = np.clip(feats[:, 2] / 10.0, -1, 1)
                        feats[:, 3] /= 100.0
                        feats[:, 4] /= 100.0

                        for i in range(len(feats) - 30):
                            window = feats[i:i+30]
                            x = torch.tensor(window, dtype=torch.float32).unsqueeze(0)
                            with torch.no_grad():
                                sim, cls, _ = model(x)
                            all_preds.append(int(cls.argmax(dim=1).item()))
                            all_labels.append(label_idx)
                            all_sims.append(float(sim[0, 0].item()))

                    if not all_preds:
                        st.error("数据不足，无法评估")
                    else:
                        from sklearn.metrics import confusion_matrix, classification_report
                        import matplotlib.pyplot as plt

                        cn_labels = [CLASS_CN.get(c, c) for c in CN]

                        cm = confusion_matrix(all_labels, all_preds, labels=[0, 1, 2])
                        fig, ax = plt.subplots(figsize=(5, 4))
                        ax.imshow(cm, cmap="Blues")
                        for i in range(3):
                            for j in range(3):
                                ax.text(j, i, str(cm[i][j]), ha="center", va="center",
                                        color="white" if cm[i][j] > cm.max()/2 else "black",
                                        fontsize=14)
                        ax.set_xticks([0, 1, 2]); ax.set_yticks([0, 1, 2])
                        ax.set_xticklabels(cn_labels); ax.set_yticklabels(cn_labels)
                        ax.set_xlabel("预测"); ax.set_ylabel("真实")
                        ax.set_title("混淆矩阵")
                        plt.tight_layout()
                        st.pyplot(fig)

                        report = classification_report(all_labels, all_preds, target_names=cn_labels)
                        st.code(report)

                        st.subheader("相似度分布")
                        st.caption("三种标签的相似度直方图应尽量分开 — 重叠越少说明模型区分能力越强")
                        sim_df = pd.DataFrame({
                            "相似度": all_sims,
                            "真实类别": [CLASS_CN.get(CN[l], CN[l]) for l in all_labels],
                        })
                        fig2 = px.histogram(sim_df, x="相似度", color="真实类别",
                                            barmode="overlay", nbins=40, opacity=0.7,
                                            color_discrete_map={CLASS_CN[k]: v for k, v in COLORS.items() if k in CLASS_CN})
                        fig2.update_layout(xaxis_range=[0, 1])
                        st.plotly_chart(fig2, use_container_width=True)

                        st.subheader("各类别相似度统计")
                        stats = sim_df.groupby("真实类别")["相似度"].describe()
                        st.dataframe(stats)

            except Exception as e:
                st.error(f"评估失败: {e}")
                import traceback
                st.code(traceback.format_exc())
    elif not os.path.isfile(model_path):
        st.info("未找到训练模型。请先完成训练。")


# =========================================================================
# 标签页 4: 实时推理
# =========================================================================
with tab4:
    st.header("实时推理监控")
    st.caption("连接到板端运行中的系统，实时查看角度和模型推理结果")

    board_ip = st.text_input("板端 IP", value="10.105.245.224")
    board_port = st.number_input("端口", value=5000)
    duration = st.slider("监控时长 (秒)", 10, 120, 30)

    if st.button("开始监控"):
        import requests
        placeholder = st.empty()
        chart_data = {"角度": [], "相似度x180": [], "帧": []}
        frame_idx = 0

        for _ in range(duration * 10):
            try:
                r = requests.get(f"http://{board_ip}:{board_port}/state_feed", timeout=1)
                d = r.json()
                frame_idx += 1
                chart_data["帧"].append(frame_idx)
                chart_data["角度"].append(d.get("angle", 0))
                chart_data["相似度x180"].append(d.get("similarity", 0) * 180)

                with placeholder.container():
                    c1, c2, c3, c4, c5 = st.columns(5)
                    ex = d.get("exercise", "?")
                    c1.metric("运动", "深蹲" if ex == "squat" else "弯举" if ex == "bicep_curl" else ex)
                    state_cn = {"STAND": "站立", "DESCENDING": "下蹲中", "BOTTOM": "蹲到底",
                                "ASCENDING": "起身中", "NO_PERSON": "无人",
                                "CURLING": "弯举中", "EXTENDING": "下放中", "TOP": "顶峰"}
                    c2.metric("状态", state_cn.get(d.get("state", ""), d.get("state", "?")))
                    c3.metric("角度", f"{d.get('angle', 0):.0f}")
                    c4.metric("疲劳", f"{d.get('fatigue', 0):.0f}/1500")
                    sim_val = d.get("similarity", None)
                    c5.metric("相似度", f"{sim_val:.0%}" if sim_val else "无模型")

                    df_live = pd.DataFrame(chart_data)
                    if len(df_live) > 2:
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(x=df_live["帧"], y=df_live["角度"],
                                                 name="关节角度", line=dict(color="#3b82f6", width=2)))
                        fig.add_trace(go.Scatter(x=df_live["帧"], y=df_live["相似度x180"],
                                                 name="相似度(x180)", line=dict(color="#22c55e", width=2, dash="dot")))
                        fig.update_layout(height=350, xaxis_title="帧",
                                          yaxis_title="角度/相似度", legend=dict(orientation="h"))
                        st.plotly_chart(fig, use_container_width=True)

                time.sleep(0.1)
            except Exception:
                time.sleep(0.5)

        st.success(f"监控结束 ({duration}秒)")
    else:
        st.info("点击「开始监控」连接板端实时数据流")
