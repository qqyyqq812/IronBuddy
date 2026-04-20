# IronBuddy V4.3 明日训练 SOP（2026-04-18）

**版本**：v4.3（单动作瘦身 + 全面拥抱 FLEX + 域迁移微调）
**目标**：哑铃弯举单动作完成 base pretrain + 本地 DA + holdout 验证 + 视频预录
**必读伴随**：[V4.3 plan](file:///home/qq/.claude/plans/flex-curl-only-pivot.md) / [数据采集与训练指南.md](数据采集与训练指南.md) / [decisions.md §XIII](decisions.md)

**硬约束**：
- 只剩 2 贴片（CH0 肱二头肌 + CH1 小臂）→ 4 贴片类动作砍掉，本次单弯举
- FLEX 做 base pretrain（数据量），本地做 domain adaptation（真实分布）
- 架构不动：GRU(hidden=6)×8d，fusion head 664 参数

---

## §1 网上公开数据预准备战略

### §1.0 核心决策更新（2026-04-18 早晨 pivot）

经过对 FLEX / MIA / EMAHA-DB4 / EMAHA-DB5 / Mendeley 8j2p29hnbv / HSBI 六个候选数据集的深度调查（参见 [`公开数据集/README.md`](../../公开数据集/README.md)），**没有一个公开免账号直接可下的高质量哑铃弯举 sEMG 数据集**能支撑 V4.2 的 11D 特征 + 三类代偿标签预训练。**明天弯举 pipeline 不使用任何公开数据集做 base pretrain，改为 本地自采 + 10× augment**。

FLEX 仍是未来首选，license 到位后激活已写好的 `tools/flex_preprocess.py`；MIA 52.9GB 下载继续，留给 V4.4 深蹲子项目。

### §1.1 候选数据集评估表速查

| 数据集 | 弯举？ | 可下？ | 采样率 | 阻塞点 |
|---|---|---|---|---|
| FLEX (NeurIPS 2025) | 是（多变体） | 否 | 200Hz | Google Form license 24–72h 审批中，明天拿不到 |
| MIA (ICCV 2023) | **零弯举**（16 动作全下肢 + 武术） | 是（52.9GB 下载中） | 2000Hz | 任务错配；有 GoodSquat/BadSquat 标签 → 留给 V4.4 深蹲 |
| EMAHA-DB4 (Harvard Dataverse) | 否（ADL：坐/站/走） | 是 | - | 文件清单全是 `Sub0X_ADL_HONOR_{SIT,STANDING,WALKING}.mat` |
| EMAHA-DB5（含弯举版） | 是 | **否** | - | 全网搜不到公开发布 |
| Mendeley 8j2p29hnbv | 疲劳 biceps | 是 | **1 Hz（envelope）** | 无法算 MDF/MNF/ZCR 四个频域列 |
| HSBI biceps (Bielefeld) | 是 | 否（Anubis JS 反爬） | - | 只能浏览器手点 Download |

### §1.2 今天上午需要用户操作（异步、非阻塞）

- **（可选）FLEX 备案**：提交 Google Form，等未来 24–72h 审批
  ```bash
  open https://docs.google.com/forms/d/e/1FAIpQLSdVWgFO3XSlkvKnaLIEaCkedJ-QDb4wclVgE2LhLQ4nGPBmRQ/viewform
  # 用途填写：Academic research on sEMG-based fitness coaching at edge devices, ZJU embedded competition.
  ```
- **（可选）HSBI 浏览器手动下载**：Bielefeld 站有 Anubis JS 反爬命令行拿不到，只能手点 Download 保存到 `data/external/hsbi_biceps/`（仅做 J1/J2 MDF/MNF 基线参考，不进训练）
- **MIA 52.9GB 下载继续**：**留给未来** V4.4 深蹲子项目，不参与明天弯举 pipeline

### §1.3 本地 augment 替代方案（明天弯举主路径）

**前置条件**：[§2 线下实测数据](#2-线下实测数据具体测算脚本与数量要求) 全部执行完，`data/v42/user_01..03/curl/` 有 ~270 rep 原始数据。

```bash
# 10× 数据增强（时间轴 warp + EMG 幅值/信噪比扰动 + 关节角度微抖）
python tools/augment_local.py --data-root data/v42 --multiplier 10 --holdout-user user_04
# 期望：原 ~270 rep → augmented ~2970 rep（写入 data/v42/_augmented/）

# Encoder 预训练（纯本地数据源，不走 FLEX 分支）
python tools/pretrain_encoders.py --source local --exercise curl --data-root data/v42 --epochs 30

# Fusion head 训练 + LOSO 3 折
python tools/train_fusion_head.py --exercise curl --data-root data/v42 --epochs 50
```

**期望产出**：`weights/v42_fusion_head_curl.pt + _metrics.json`，LOSO 3 折 avg val_f1 ≥ 0.60（纯本地 augment 下限，比 FLEX 加持版的 0.65 低 0.05）。

### §1.4 未来激活路径（FLEX / MIA 到位后）

**FLEX license 到位（任意时间）**：已写好的脚本直接复活，无需重写：
```bash
python tools/flex_preprocess.py --flex-root <解压目录> --out data/flex --validate-channels
python tools/pretrain_encoders.py --source flex --epochs 30
python tools/finetune_with_local.py \
    --pretrained-encoder hardware_engine/cognitive/weights/emg_encoder_flex_pretrained.pt \
    --freeze-base --epochs 30 --data-root data/v42
# 期望：LOSO avg val_f1 从 ≥0.60 抬升到 ≥0.65
```

**MIA 52.9GB 下完（V4.4 深蹲子项目开工时）**：独立立项，需新写 `tools/mia_preprocess.py`（参考 MIA `inference_scripts/retrieval_id_nocond_exercises_posetoemg.py:119-141` 的 exercise 字典 + GoodSquat/BadSquat 二分类标签）。板端弯举路径零影响。

---

## §2 线下实测数据具体测算脚本与数量要求

### §2.1 数量精确答案

**3 人 × 3 类 × 1 段 60s = 9 段（≈ 270 rep 等效）**
- 每段 60s ≈ 25–30 rep（rep ~ 2–2.5s 节拍）
- 三类 × 1 段足以表达类内分布（DA 文献下限 30 rep / 类）
- 总录制 9 分钟 + 贴片/MVC/重测 30 分钟/人 → **半天搞定**

**第 4 人 holdout**：1 人 × 3 类 × 1 段 60s = **3 段**，仅测试不参与训练。

**可选升级**：若时间充裕每类 +1 段 → 18 段。默认 9 段。

### §2.2 板端旧脚本拿取（用户操作）

```bash
ssh -i ~/.ssh/id_rsa_toybrick toybrick@10.105.245.224 \
  "ls -la /home/toybrick/streamer_v3/ && \
   echo '=== start_collect.sh ===' && cat /home/toybrick/streamer_v3/start_collect.sh && \
   echo '=== collect_one.sh ===' && cat /home/toybrick/streamer_v3/collect_one.sh && \
   echo '=== sample output ===' && ls /home/toybrick/streamer_v3/output/ 2>/dev/null | head -10 && \
   head -5 \$(ls /home/toybrick/streamer_v3/output/*.csv 2>/dev/null | head -1)"
```

**期望产出**：两脚本源码 + 一个样本 csv 前 5 行。贴回后我做 5 行补丁到 `tools/upgrade_collect_7d_to_11d.py` 的 `parse_old_csv()`。

### §2.3 7D → 11D 升级路径（旁路升级，zero-invasion）

**保留板端黄金代码不动**，本地 wrapper 做升级：
1. `scp` 拉板端 7D CSV + raw EMG
2. raw EMG (200Hz × 60s = 12000 sample) 算 4 频域列（MDF/MNF/ZCR/Raw_Unfilt，`scipy.signal.welch`）
3. 60s 7D CSV 按 `Angle` peak-finding 切 rep（相邻局部最小值为 1 rep，窗口 [1.5s, 3s] 有效）
4. 每 rep 追加 4 列 → `rep_001.csv ... rep_030.csv`（11D 13 列）

**raw EMG 拿不到的兜底**：
- ZCR：用 `Target_RMS` 滑动窗口估算（精度 -20% 但不阻塞）
- MDF/MNF：用 RMS 包络近似
- Raw_Unfilt：`Target_RMS × 0.85` 系数兜底

### §2.4 命令清单（现场原样复制粘贴）

#### Step A · 启动板端裸服务（不开 UI/FSM/voice）

```bash
ssh toybrick "cd /home/toybrick/streamer_v3 && bash start_collect.sh"
ssh toybrick "pgrep -af '[u]dp_emg_server'"   # 期望有 PID
ssh toybrick "ls /dev/shm/emg_heartbeat"      # 期望文件存在
```
**失败 fallback**：`ssh toybrick "pkill -9 -f 'streamer_app|main_claw_loop|voice_daemon'"` 清理残留再重启。

#### Step B · 极简 MVC 校准

```bash
python tools/mvc_cli.py --user user_01 --duration 5 --reps 3 --board toybrick@10.105.245.224
```
**期望产出**：`data/v42/user_01/mvc_calibration.json`（格式与 V4.2 UI 模态框一致：`{"protocol":"SENIAM-2000","peak_mvc":{"ch0":...,"ch1":...}}`）。3 次 peak RMS **取最大值**（按 SENIAM，非平均）。

#### Step C · 三类各 60s 采集（共 9 段）

```bash
for u in user_01 user_02 user_03; do
  for c in standard compensation bad_form; do
    echo "=== $u × curl × $c ==="
    ssh toybrick "bash /home/toybrick/streamer_v3/collect_one.sh $u curl-$c 60"
    sleep 30   # 用户休息 30s
  done
done
```
**期望产出**：板端 `/home/toybrick/streamer_v3/output/` 下 9 个 CSV + 9 个 raw 文件。

#### Step D · 7D → 11D 升级 + 切 rep（笔记本批量）

```bash
for u in user_01 user_02 user_03; do
  for c in standard compensation bad_form; do
    python tools/upgrade_collect_7d_to_11d.py \
        --board-host toybrick@10.105.245.224 \
        --user $u --label $c --segment-len 60 --rep-len 2.0 \
        --out data/v42/$u/curl/$c/
  done
done
```
**期望产出**：`data/v42/user_01..03/curl/{standard,compensation,bad_form}/rep_001.csv ... rep_030.csv`。

#### Step E · 完整性验证

```bash
python tools/validate_v42_dataset.py --data-root data/v42 --min-reps-per-class 25
```
**期望产出**：3 人 × 3 类 × ~30 rep ≈ 270 rep，全 PASS，exit 0。
**失败 fallback**：若某类 < 25 rep，回 Step C 补采该类单段。

#### Step F · DA 微调

```bash
# 主路径（FLEX 已 pretrain）
python tools/finetune_with_local.py \
    --pretrained-encoder hardware_engine/cognitive/weights/emg_encoder_flex_pretrained.pt \
    --freeze-base --epochs 30 --data-root data/v42

# Fallback C（FLEX 未到位）
python tools/pretrain_encoders.py --source local --exercise curl --data-root data/v42 --epochs 30
python tools/train_fusion_head.py --exercise curl --data-root data/v42 --epochs 50
```
**期望产出**：`weights/v42_fusion_head_curl_da.pt + _metrics.json`；过拟合闸门 gap ≤ 0.15。

#### Step G · 第 4 人 holdout 推理

```bash
# 评委采集（同 Step C，user=user_04）
for c in standard compensation bad_form; do
  ssh toybrick "bash /home/toybrick/streamer_v3/collect_one.sh user_04 curl-$c 60"
done
for c in standard compensation bad_form; do
  python tools/upgrade_collect_7d_to_11d.py --user user_04 --label $c \
      --board-host toybrick@10.105.245.224 --segment-len 60 --rep-len 2.0 \
      --out data/v42/user_04/curl/$c/
done

# Holdout 推理（不重训）
python tools/infer_holdout.py \
    --weights hardware_engine/cognitive/weights/v42_fusion_head_curl_da.pt \
    --data-root data/v42 --user user_04
```
**期望产出**：混淆矩阵 + per-class 召回：compensation 召回 ≥ 70%，bad_form 召回 ≥ 80%。

---

## 附录

### 物品清单

- 2 张一次性 sEMG 电极贴片（备用 +2）
- 酒精棉片 × 10（贴前皮肤脱脂）
- 卷尺（定位肱二头肌隆起中点 / 小臂中段）
- 哑铃 5kg × 1 + 8kg × 1（两种负载可选）
- 笔记本（跑 upgrade + DA 脚本）
- 移动电源 / 板端充电线
- Toybrick 板 + USB Webcam（板载麦损坏）
- 笔 + 纸（记录 MVC 峰值 / 重测理由）

### 现场 troubleshooting 速查

1. **板端 `pgrep udp_emg_server` 无 PID** → `pkill -9 -f streamer_app` 后重跑 start_collect.sh；查 `/dev/shm/` 是否有残留 lock 文件
2. **EMG 信号全 0 / 饱和** → 电极贴片松动或皮肤未脱脂，撕掉重贴；检查 CH0/CH1 连线未对调
3. **60s 段后期 RMS 下降**（疲劳污染 standard） → 允许中间 5s 休息；后处理把后段切给 compensation 类
4. **ssh 拉 raw 文件失败** → 走兜底（§2.3 末），upgrade 脚本内置从 7D 估频域
5. **MVC 三次差异 > 30%** → 电极位置不稳，重贴 + 重测；取最大值不取平均

### 成功验收（明天结束时）

- [ ] `data/v42/user_01..03/curl/{standard,compensation,bad_form}/` 各 ≥ 25 rep
- [ ] `validate_v42_dataset.py` exit 0
- [ ] `weights/v42_fusion_head_curl_da.pt` 存在，LOSO avg val_f1 ≥ 0.55（fallback）/ ≥ 0.65（FLEX）
- [ ] 第 4 人 holdout compensation 召回 ≥ 70%，bad_form 召回 ≥ 80%
- [ ] 视频预录（后备演示素材）完成
