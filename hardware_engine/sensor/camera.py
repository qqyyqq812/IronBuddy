# -*- coding: utf-8 -*-
import os
import time

class CameraController:
    """
    修改后的 CameraController (IPC 纯直出版)
    Agent 2 已经用 C++ 引擎（~29FPS）将原生帧打入内存盘 `/dev/shm/result.jpg`。
    因此本模块废弃所有冗杂的 CV2 占用，只负责读取内存盘推送 MJPEG 给 Web 端点。
    """
    _instance = None
    _shm_path = "/dev/shm/result.jpg"

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(CameraController, cls).__new__(cls)
        return cls._instance

    def __init__(self, device_id=None, width=640, height=480):
        # 参数预留以向后兼容
        pass

    def connect(self):
        """无需真实的物理 connect"""
        print("[Sensor API] 现已由 Agent 2 的 C++ 守护进程接管摄像头！当前为 IPC RAMDisk 旁路模式。")

    def read_frame(self):
        """兼容接口，但实际业务流建议直接用 get_mjpeg_stream"""
        if os.path.exists(self._shm_path):
            return True, None # 考虑到不需要返回 numpy 给外部，免去解码
        return False, None

    def get_mjpeg_stream(self):
        """
        供 Web / Flask 网络流直推引擎调用的流水线转码发生器。
        直接读取文件二进制并分发，CPU 占用几乎为 0。
        """
        while True:
            # 高频检测文件存在（设置一个基础的睡眠防止空转飙满单核）
            time.sleep(0.04) # ~25FPS 的轮询率
            
            if not os.path.exists(self._shm_path):
                continue
                
            try:
                # 以二进制方式急速读取
                with open(self._shm_path, "rb") as f:
                    frame_bytes = f.read()
                    
                if not frame_bytes:
                    continue
                    
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            except Exception:
                # 防止由于 C++ 在覆写时造成的短时访问冲突 (I/O Block)
                pass

    def release(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass
