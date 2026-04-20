-- IronBuddy DB migration 2026-04-20 (models + embeddings)
-- 目的: 给 DB Viewer 增加"模型分类存储"和"多维区分度"两个视图
-- 幂等: CREATE TABLE IF NOT EXISTS
-- 回滚: DROP TABLE model_registry; DROP TABLE feature_embeddings;

-- ============================================================
-- model_registry: 训练模型元数据 (路径 / 架构 / acc / 训练时间)
-- ============================================================
CREATE TABLE IF NOT EXISTS model_registry (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  exercise      TEXT,
  path          TEXT NOT NULL,
  arch          TEXT,
  params_m      REAL,
  size_kb       REAL,
  train_acc     REAL,
  val_acc       REAL,
  epochs        INTEGER,
  dataset       TEXT,
  trained_at    TEXT,
  active        INTEGER DEFAULT 0,
  notes         TEXT,
  is_demo_seed  INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_model_exercise ON model_registry(exercise);
CREATE INDEX IF NOT EXISTS idx_model_active   ON model_registry(active);


-- ============================================================
-- feature_embeddings: 预计算的 2D 投影 (PCA/tSNE) 给前端散点图
-- 一个 exercise 的所有训练样本降维到 (x,y), 按 label 着色
-- ============================================================
CREATE TABLE IF NOT EXISTS feature_embeddings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  exercise    TEXT NOT NULL,
  label       TEXT NOT NULL,
  x           REAL NOT NULL,
  y           REAL NOT NULL,
  source      TEXT,
  notes       TEXT
);

CREATE INDEX IF NOT EXISTS idx_emb_exercise ON feature_embeddings(exercise);
CREATE INDEX IF NOT EXISTS idx_emb_label    ON feature_embeddings(label);
