#!/usr/bin/env python3
"""
IronBuddy V2.2 — 性能 Benchmark 脚本
在板端执行，测量所有关键管线的延迟和资源占用。
用法: python3 benchmark.py
"""
import os
import sys
import time
import json
import subprocess
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = {}

def bench(name, fn, iterations=30, warmup=5):
    """通用 benchmark 封装"""
    times = []
    for i in range(warmup + iterations):
        t0 = time.perf_counter()
        fn()
        dt = (time.perf_counter() - t0) * 1000
        if i >= warmup:
            times.append(dt)
    avg = np.mean(times)
    p95 = np.percentile(times, 95)
    RESULTS[name] = {"avg_ms": round(avg, 1), "p95_ms": round(p95, 1), "min_ms": round(min(times), 1)}
    print(f"  {name}: avg={avg:.1f}ms  p95={p95:.1f}ms  min={min(times):.1f}ms")


def bench_npu_snapshot():
    """测试 NPU 输出帧读取速度"""
    def fn():
        try:
            with open("/dev/shm/result.jpg", "rb") as f:
                f.read()
        except FileNotFoundError:
            pass
    bench("NPU 帧读取 (shm)", fn, iterations=100, warmup=5)


def bench_3d_lifting():
    """测试 VideoPose3D ONNX 推理延迟"""
    try:
        from biomechanics.lifting_3d import Lifting3D
        model_path = "/home/toybrick/biomechanics/checkpoints/videopose3d_243f_causal.onnx"
        if not os.path.exists(model_path):
            print("  ⚠️ ONNX 模型不存在, 跳过 3D lifting benchmark")
            return
        lifter = Lifting3D(model_path)
        # 填满缓冲区
        for _ in range(lifter.num_frames):
            lifter.update(np.random.randn(17, 2).astype(np.float32) * 0.3)
        bench("3D Lifting (ONNX)", lambda: lifter.update(np.random.randn(17, 2).astype(np.float32) * 0.3))
    except ImportError as e:
        print(f"  ⚠️ 3D Lifting 依赖缺失: {e}")


def bench_vosk():
    """测试 Vosk 离线 ASR 识别延迟"""
    try:
        from vosk import Model, KaldiRecognizer
        import wave
        model_path = "/home/toybrick/vosk-model-small-cn-0.22"
        if not os.path.exists(model_path):
            print("  ⚠️ Vosk 模型不存在, 跳过 ASR benchmark")
            return
        model = Model(model_path)

        # 生成 3 秒静音测试音频
        test_wav = "/tmp/bench_silence.wav"
        samples = np.zeros(16000 * 3, dtype=np.int16)
        with wave.open(test_wav, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(samples.tobytes())

        def fn():
            rec = KaldiRecognizer(model, 16000)
            with wave.open(test_wav, 'rb') as wf:
                while True:
                    data = wf.readframes(4000)
                    if len(data) == 0:
                        break
                    rec.AcceptWaveform(data)
                rec.FinalResult()

        bench("Vosk ASR (3s 音频)", fn, iterations=10, warmup=2)
        os.remove(test_wav)
    except ImportError:
        print("  ⚠️ Vosk 未安装, 跳过 ASR benchmark")


def bench_memory():
    """测量当前系统内存占用"""
    try:
        result = subprocess.run(
            ["ps", "aux", "--sort=-rss"],
            capture_output=True, text=True, timeout=5
        )
        ironbuddy_procs = []
        for line in result.stdout.split('\n'):
            if any(kw in line for kw in ['main_claw', 'streamer', 'voice_daemon', 'yolo_test/build/main']):
                parts = line.split()
                if len(parts) >= 6:
                    ironbuddy_procs.append({
                        "process": parts[10] if len(parts) > 10 else parts[-1],
                        "rss_mb": round(int(parts[5]) / 1024, 1),
                        "cpu_pct": parts[2]
                    })
        total_rss = sum(p["rss_mb"] for p in ironbuddy_procs)
        RESULTS["内存占用"] = {"total_rss_mb": round(total_rss, 1), "processes": ironbuddy_procs}
        print(f"  总 RSS: {total_rss:.1f} MB ({len(ironbuddy_procs)} 个进程)")
        for p in ironbuddy_procs:
            print(f"    {p['process']}: {p['rss_mb']} MB, CPU {p['cpu_pct']}%")
    except Exception as e:
        print(f"  ⚠️ 内存测量失败: {e}")


def main():
    print("=" * 60)
    print("  IronBuddy V2.2 Performance Benchmark")
    print("=" * 60)
    print()

    print("[1/4] NPU 帧读取...")
    bench_npu_snapshot()

    print("[2/4] 3D Lifting ONNX 推理...")
    bench_3d_lifting()

    print("[3/4] Vosk 离线 ASR...")
    bench_vosk()

    print("[4/4] 内存占用...")
    bench_memory()

    print()
    print("=" * 60)
    print("  Benchmark 结果")
    print("=" * 60)
    print(json.dumps(RESULTS, ensure_ascii=False, indent=2))

    # 保存结果
    out_path = "/tmp/ironbuddy_benchmark.json"
    with open(out_path, "w") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存到: {out_path}")


if __name__ == "__main__":
    main()
