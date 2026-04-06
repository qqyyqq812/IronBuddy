import numpy as np
import csv
import matplotlib.pyplot as plt
import os

print("🚀 Worker_2 启动肌电离线验证游乐场...")

# 1. 构造“还原真实环境的带噪原始波形”
fs = 1000 # 1kHz 采样率，符合 ESP32 采集频率
duration = 10 # 10秒长序列
t = np.linspace(0, duration, fs * duration)

# 毛刺: 肌电静息态白噪声
noise_base = np.random.normal(0, 0.05, len(t))

# 极低频漂移: 模拟呼吸和电极位移带来的基准电平漂移 < 2Hz
baseline_wander = 0.3 * np.sin(2 * np.pi * 0.5 * t) + 0.1 * np.sin(2 * np.pi * 0.1 * t)

# 工频干扰: 电网 50Hz 强感应
powerline_hum = 0.4 * np.sin(2 * np.pi * 50 * t)

# 真实肌肉发力: 模拟两组深蹲发力 (2-4秒 和 6.5-8.5秒)
envelope_true = np.zeros_like(t)
envelope_true[(t > 2) & (t < 4)] = np.hanning(fs * 2) * 1.5
envelope_true[(t > 6.5) & (t < 8.5)] = np.hanning(fs * 2) * 1.2

# 用高斯白噪声乘上发光包络，形成肌电丛烈
emg_bursts = envelope_true * np.random.normal(0, 1, len(t))

# 最终叠加组装为野生原始信号
raw_emg = noise_base + baseline_wander + powerline_hum + emg_bursts

# 持久化为老板要求的 CSV 数据集文件
csv_path = "/home/qq/projects/embedded-fullstack/sandbox/realistic_raw_emg.csv"
with open(csv_path, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(["time", "raw_emg"])
    for ti, re in zip(t, raw_emg):
        writer.writerow([round(ti, 3), round(re, 4)])
print(f"✅ 已生成并保存仿真野生数据集: {csv_path}")

# ===============================================
# 2. 核心算法流脱机压测 (模拟逐点送入)
# ===============================================
extracted_envelope = []
sum_hist = 0.0
history = []
WINDOW_SIZE = 50 # 50ms MAV 窗口

calib_limit = 500
# 模拟系统开机瞬间，抓取前 500 点来获得初始基线偏移
dc_offset = np.mean(raw_emg[:calib_limit])

min_env = 9999.0
max_env = 0.01

for val in raw_emg:
    # 【去直流】
    centered = val - dc_offset
    # 【全波整流】
    rectified = abs(centered)
    
    # 【MAV 滑动绝对平均】自动平滑，充当低通滤波
    history.append(rectified)
    sum_hist += rectified
    if len(history) > WINDOW_SIZE:
        old = history.pop(0)
        sum_hist -= old
    
    mav = sum_hist / len(history)
    
    # 【极其坚固的动态自适应边界追踪】
    if mav < min_env: 
        min_env = mav
    max_env = max(mav, max_env * 0.9999) # 缓慢衰减机制(自适应)
    
    # 【0-100 归一化】
    m_diff = max_env - min_env
    if m_diff < 0.2: # 噪音底噪锁定，防止分母过小放大噪声
        m_diff = 0.2 
        
    act = ((mav - min_env) / m_diff) * 100.0
    act = max(0, min(100, act))
    
    extracted_envelope.append(act)

extracted_envelope = np.array(extracted_envelope)

# ===============================================
# 3. 极客感对撞绘图 (Visual Proof!)
# ===============================================
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
fig.patch.set_facecolor('#1e1e1e')
for ax in (ax1, ax2):
    ax.set_facecolor('#2d2d2d')
    ax.tick_params(colors='white')
    ax.xaxis.label.set_color('white')
    ax.yaxis.label.set_color('white')
    ax.title.set_color('white')
    ax.grid(True, linestyle=':', color='#555555')

ax1.plot(t, raw_emg, color='#a9b7c6', alpha=0.9, linewidth=0.5, label='Raw sEMG (Noisy, 50Hz, Baseline Drift)')
ax1.set_title("1. Simulated Wild Raw sEMG Signal with Hum and Drift", fontsize=14, pad=10)
ax1.set_ylabel("Amplitude (mV)")
ax1.legend(facecolor='#1e1e1e', edgecolor='#555555', labelcolor='white')

ax2.plot(t, extracted_envelope, color='#ff5252', linewidth=2, label='Processed MAV Envelope (0-100%)')
ax2.fill_between(t, 0, extracted_envelope, color='#ff5252', alpha=0.3)
ax2.set_title("2. Real-time Extracted Muscle Activation Envelopes via Worker 2 Offline Filter", fontsize=14, pad=10)
ax2.set_ylabel("Activation Level (%)")
ax2.set_xlabel("Time (Seconds)")
ax2.set_ylim(-5, 110)
ax2.legend(facecolor='#1e1e1e', edgecolor='#555555', labelcolor='white')

plt.tight_layout()
out_png = "/home/qq/projects/embedded-fullstack/sandbox/emg_proof.png"
plt.savefig(out_png, dpi=200, facecolor=fig.get_facecolor(), bbox_inches='tight')
print(f"📈 Plot saved at: {out_png}")
