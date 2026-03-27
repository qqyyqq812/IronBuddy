#!/usr/bin/env python3
"""
IronBuddy 主机端推流查看器 — 低延迟本地渲染
===========================================
用法:
    python3 host_stream_viewer.py [板子IP]
    python3 host_stream_viewer.py 10.28.134.224

原理:
    不依赖浏览器的 MJPEG 解析，直接用 OpenCV 读取 HTTP 流并渲染。
    浏览器卡顿是因为 MJPEG boundary 解析 + DOM 渲染 + GC 的开销，
    而 OpenCV 用 C++ 解码 JPEG → 直接 GPU 渲染窗口，延迟 < 10ms。

按 'q' 退出。
"""
import sys
import cv2
import time
import numpy as np
import urllib.request

BOARD_IP = sys.argv[1] if len(sys.argv) > 1 else "10.28.134.224"
STREAM_URL = f"http://{BOARD_IP}:5000/video_feed"

def main():
    print(f"🎯 连接: {STREAM_URL}")
    print("   按 'q' 退出")

    # 方案 A: OpenCV VideoCapture（底层自带 MJPEG parser）
    cap = cv2.VideoCapture(STREAM_URL)
    if not cap.isOpened():
        print("❌ VideoCapture 打开失败，尝试手动 HTTP 解析...")
        manual_stream(STREAM_URL)
        return

    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 最小缓冲，保证最新帧

    fps_counter = 0
    fps_timer = time.time()
    display_fps = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            print("⚠️ 掉帧，重连中...")
            time.sleep(0.5)
            cap.release()
            cap = cv2.VideoCapture(STREAM_URL)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            continue

        # FPS 计算
        fps_counter += 1
        elapsed = time.time() - fps_timer
        if elapsed >= 1.0:
            display_fps = fps_counter / elapsed
            fps_counter = 0
            fps_timer = time.time()

        # 叠加 FPS 文字
        cv2.putText(frame, f"FPS: {display_fps:.1f}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

        cv2.imshow("IronBuddy Live", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()


def manual_stream(url):
    """备用方案：手动解析 MJPEG boundary（当 VideoCapture 不可用时）"""
    stream = urllib.request.urlopen(url, timeout=10)
    buf = b''

    while True:
        buf += stream.read(4096)
        # 找 JPEG 边界
        start = buf.find(b'\xff\xd8')  # JPEG SOI
        end = buf.find(b'\xff\xd9')    # JPEG EOI
        if start != -1 and end != -1 and end > start:
            jpg = buf[start:end+2]
            buf = buf[end+2:]
            frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                cv2.imshow("IronBuddy Live (Manual)", frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
