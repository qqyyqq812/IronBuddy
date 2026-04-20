#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seed model_registry + feature_embeddings.

- 读 models/ 下真实 .pt 文件，写元数据到 model_registry
- 为 bicep_curl / squat 生成 2D 散点种子（标准/代偿/非标准三类，可分但有重叠）

幂等：is_demo_seed=1 的行重跑会先清掉。执行前自动备份数据库。

用法：
  python3 scripts/seed_models_and_embeddings.py        # 开发机
  python3 scripts/seed_models_and_embeddings.py --db /path/to/other.db
"""
import argparse
import math
import os
import random
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "ironbuddy.db")
MIGRATION_SQL = os.path.join(ROOT, "scripts", "migrate_2026_04_20_models.sql")


# -------- 模型元数据（对齐 models/ 下真实文件）--------
MODELS = [
    dict(
        name="extreme_fusion_gru_curl",
        exercise="bicep_curl",
        path="models/extreme_fusion_gru_curl.pt",
        arch="CompensationGRU 7D->similarity+3class",
        params_m=0.18,
        train_acc=1.00,
        val_acc=1.00,
        epochs=20,
        dataset="augmented_curl v4.7 33k rows (3 seed × 11 aug)",
        trained_at="2026-04-19T10:20:00",
        active=1,
        notes="⚠️ val_acc=1.0 有数据泄漏（augment 同源混入 val）；板端现场 A/B 实测补偿",
        is_demo_seed=1,
    ),
    dict(
        name="extreme_fusion_gru_squat",
        exercise="squat",
        path="models/extreme_fusion_gru_squat.pt",
        arch="CompensationGRU 7D->similarity+3class",
        params_m=0.18,
        train_acc=0.96,
        val_acc=0.92,
        epochs=30,
        dataset="MIA_squat_raw + V3 golden/lazy/bad 手采",
        trained_at="2026-04-18T22:10:00",
        active=1,
        notes="V3 7D 稳定版，板端 NPU 推理 ~22ms",
        is_demo_seed=1,
    ),
    dict(
        name="yolov8n_pose",
        exercise="pose",
        path="models/yolov8n-pose.pt",
        arch="YOLOv8n-Pose (COCO 17-kpt)",
        params_m=3.3,
        train_acc=None,
        val_acc=None,
        epochs=None,
        dataset="COCO Keypoints 2017 (Ultralytics 预训练)",
        trained_at="2024-01-01T00:00:00",
        active=1,
        notes="视觉前端：RKNN uint8 量化到板端 NPU，person_score 阈值 ~0.08",
        is_demo_seed=1,
    ),
]


def _fill_size_kb(models):
    for m in models:
        p = os.path.join(ROOT, m["path"])
        if os.path.exists(p):
            m["size_kb"] = round(os.path.getsize(p) / 1024.0, 1)
        else:
            m["size_kb"] = None


def _gaussian(cx, cy, sx, sy, n, seed):
    """在 (cx,cy) 处按 (sx,sy) 方差生成 n 个二维高斯点。"""
    rng = random.Random(seed)
    pts = []
    for _ in range(n):
        # Box-Muller
        u1 = max(rng.random(), 1e-9)
        u2 = rng.random()
        z0 = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
        z1 = math.sqrt(-2 * math.log(u1)) * math.sin(2 * math.pi * u2)
        pts.append((round(cx + z0 * sx, 4), round(cy + z1 * sy, 4)))
    return pts


def _curl_embeddings():
    """弯举：standard 左下聚集 / compensating 右中 / non_standard 右上分散。"""
    pts = []
    for x, y in _gaussian(-1.2, -0.8, 0.45, 0.40, 32, seed=11):
        pts.append(("bicep_curl", "standard", x, y))
    for x, y in _gaussian(0.9, 0.1, 0.55, 0.50, 28, seed=12):
        pts.append(("bicep_curl", "compensating", x, y))
    for x, y in _gaussian(1.4, 1.3, 0.80, 0.75, 22, seed=13):
        pts.append(("bicep_curl", "non_standard", x, y))
    return pts


def _squat_embeddings():
    """深蹲：standard 上方 / compensating 中间 / non_standard 下方分散。"""
    pts = []
    for x, y in _gaussian(-0.3, 1.2, 0.50, 0.45, 34, seed=21):
        pts.append(("squat", "standard", x, y))
    for x, y in _gaussian(0.2, -0.1, 0.55, 0.60, 30, seed=22):
        pts.append(("squat", "compensating", x, y))
    for x, y in _gaussian(0.6, -1.4, 0.85, 0.70, 24, seed=23):
        pts.append(("squat", "non_standard", x, y))
    return pts


def run(db_path):
    if not os.path.exists(db_path):
        print("DB 文件不存在:", db_path)
        sys.exit(1)

    bak = db_path + ".bak_" + str(int(time.time()))
    shutil.copy2(db_path, bak)
    print("备份:", bak)

    # 先跑 migration
    with open(MIGRATION_SQL, "r", encoding="utf-8") as f:
        sql = f.read()
    conn = sqlite3.connect(db_path)
    conn.executescript(sql)
    conn.commit()
    print("migration OK: model_registry + feature_embeddings")

    # 清演示种子
    conn.execute("DELETE FROM model_registry WHERE is_demo_seed=1")
    # embeddings 没有 is_demo_seed 列（全部当种子用）
    conn.execute("DELETE FROM feature_embeddings WHERE source='seed_pca'")
    conn.commit()

    # 灌模型
    _fill_size_kb(MODELS)
    for m in MODELS:
        conn.execute(
            "INSERT INTO model_registry "
            "(name,exercise,path,arch,params_m,size_kb,train_acc,val_acc,"
            " epochs,dataset,trained_at,active,notes,is_demo_seed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["name"], m["exercise"], m["path"], m["arch"], m["params_m"],
             m["size_kb"], m["train_acc"], m["val_acc"], m["epochs"],
             m["dataset"], m["trained_at"], m["active"], m["notes"],
             m["is_demo_seed"]),
        )
    conn.commit()

    # 灌 embeddings
    rows = _curl_embeddings() + _squat_embeddings()
    for ex, lbl, x, y in rows:
        conn.execute(
            "INSERT INTO feature_embeddings (exercise,label,x,y,source,notes) "
            "VALUES (?,?,?,?,?,?)",
            (ex, lbl, x, y, "seed_pca", "demo 种子 · 模拟 7D→PCA 2D"),
        )
    conn.commit()

    # 自检
    print("\n--- 自检 ---")
    n_models = conn.execute(
        "SELECT COUNT(*) FROM model_registry WHERE is_demo_seed=1"
    ).fetchone()[0]
    n_emb = conn.execute(
        "SELECT COUNT(*) FROM feature_embeddings WHERE source='seed_pca'"
    ).fetchone()[0]
    print("model_registry 种子行:", n_models, "(期望 3)")
    print("feature_embeddings 种子行:", n_emb, "(期望", len(rows), ")")
    for r in conn.execute(
        "SELECT name,exercise,val_acc,size_kb FROM model_registry "
        "ORDER BY id"
    ):
        print("  ", r)
    for r in conn.execute(
        "SELECT exercise,label,COUNT(*) FROM feature_embeddings "
        "GROUP BY exercise,label ORDER BY exercise,label"
    ):
        print("  ", r)
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    run(args.db)
