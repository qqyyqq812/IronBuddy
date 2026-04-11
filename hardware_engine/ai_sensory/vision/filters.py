import numpy as np
import time
import math

class LowPassFilter:
    def __init__(self):
        self.last_val = None

    def __call__(self, x, alpha):
        if self.last_val is None:
            self.last_val = np.copy(x)
            return self.last_val
        res = alpha * x + (1.0 - alpha) * self.last_val
        self.last_val = np.copy(res)
        return res

class OneEuroFilter:
    """
    一欧元滤波算法 (One-Euro Filter).
    针对高频跳动与延迟问题的最优解：速度慢时重度平滑抗锯齿，速度快时减轻平滑防止动作拖影。
    """
    def __init__(self, mincutoff=1.0, beta=0.007, dcutoff=1.0):
        """
        :param mincutoff: 减小此值能让低速运动（如肢体悬停停顿）更加平滑，稳如泰山，但可能增加一点延迟。
        :param beta:      调高此值能减少高速抓举深蹲时的延迟，但可能会让动作中段引入高频抖动。
        :param dcutoff:   用于速度导数的低频截止频率，通常 1.0 即可。
        """
        self.mincutoff = mincutoff
        self.beta = beta
        self.dcutoff = dcutoff
        self.x_filter = LowPassFilter()
        self.dx_filter = LowPassFilter()
        self.last_time = None

    def smoothing_factor(self, t_e, cutoff):
        r = 2.0 * math.pi * cutoff * t_e
        return r / (r + 1.0)

    def __call__(self, x, t=None):
        if t is None:
            t = time.time()
            
        x = np.asarray(x, dtype=float)
        
        if self.last_time is None:
            self.last_time = t
            self.dx_filter(np.zeros_like(x), 1.0)
            return self.x_filter(x, 1.0)

        dt = t - self.last_time
        if dt <= 1e-5: 
            dt = 1e-5
            
        if self.x_filter.last_val is None:
            dx = np.zeros_like(x)
        else:
            dx = (x - self.x_filter.last_val) / dt
            
        alpha_dx = self.smoothing_factor(dt, self.dcutoff)
        edx = self.dx_filter(dx, alpha_dx)
        
        # 以速度差值的绝对值作为判定因子，调节截止频率
        edx_abs = np.abs(edx)
        cutoff = self.mincutoff + self.beta * edx_abs
        alpha = self.smoothing_factor(dt, cutoff)
        
        res = self.x_filter(x, alpha)
        self.last_time = t
        return res

class PoseSmoother:
    """专门为 17 点姿态关键点包转的平滑外壳工具"""
    def __init__(self, mincutoff=1.0, beta=0.01, dcutoff=1.0):
        # 初始化一个 1EuroFilter，由于支持 Numpy，能够一次性吃进所有坐标做矩阵化平滑
        self.filter = OneEuroFilter(mincutoff, beta, dcutoff)
        
    def process(self, kpts, timestamp=None):
        """
        :param kpts: numpy 数组, shape为 (17, 3), 即 [x, y, conf]
        :param timestamp: 传入当前帧的高精度时间戳
        :return: 平滑纠正过后的 (17, 3) 张量
        """
        # 我们只平滑坐标 (x, y)，置信度 (conf) 保持原版输出无需平滑
        xy = kpts[..., :2]
        smoothed_xy = self.filter(xy, timestamp)
        
        # 覆写源数据坐标区
        out_kpts = np.copy(kpts)
        out_kpts[..., :2] = smoothed_xy
        return out_kpts
