import os
import time
import argparse

class SysfsBuzzer:
    def __init__(self, pin=153):
        """
        初始化有源低电平触发蜂鸣器控制器。
        低电平(0) = 发声，高电平(1) = 静音。
        """
        self.pin = pin
        self.export_path = "/sys/class/gpio/export"
        self.gpio_path = f"/sys/class/gpio/gpio{pin}"
        
        self._setup()

    def _setup(self):
        print(f"[Buzzer] 初始化 GPIO {self.pin} (有源低电平触发模式)...")
        if not os.path.exists(self.gpio_path):
            try:
                with open(self.export_path, 'w') as f:
                    f.write(str(self.pin))
            except Exception:
                pass
        time.sleep(0.1)
        
        direction_path = os.path.join(self.gpio_path, "direction")
        try:
            with open(direction_path, 'w') as f:
                f.write("out")
        except Exception as e:
            print(f"初始化引脚方向失败: {e}")
            
        self.mute()  # 初始化后立即静音

    def simple_beep(self, duration=0.3):
        """有源蜂鸣器：直接拉低触发，无需 PWM 方波"""
        print(f"[Buzzer] 发声 (Duration={duration}s)")
        value_path = os.path.join(self.gpio_path, "value")
        try:
            with open(value_path, 'w') as f:
                f.write('0')  # 低电平 → 触发发声
                f.flush()
            time.sleep(duration)
        except Exception:
            pass
        self.mute()

    def beep(self, duration=0.3):
        """短促蜂鸣（有源蜂鸣器专用）"""
        self.simple_beep(duration)

    def alarm(self, count=3, interval=0.1):
        """连续警报"""
        print(f"[Buzzer] 发出 {count} 次连音警报...")
        for _ in range(count):
            self.simple_beep(0.15)
            time.sleep(interval)

    def mute(self):
        """静音: 写入 1 (高电平) 截止蜂鸣器"""
        value_path = os.path.join(self.gpio_path, "value")
        try:
            with open(value_path, 'w') as f:
                f.write('1')  # 高电平 → 静音
        except Exception:
            pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser("RK3399ProX 有源低电平触发蜂鸣器驱动")
    parser.add_argument('--pin', type=int, default=153)
    parser.add_argument('--count', type=int, default=3)
    args = parser.parse_args()

    print(f"=== 启动蜂鸣器测试 (GPIO: {args.pin}) ===")
    buzzer = SysfsBuzzer(pin=args.pin)
    
    print("-> 触发短促测试音...")
    buzzer.beep(0.3)
    time.sleep(0.5)
    
    print("-> 触发连续报警音...")
    buzzer.alarm(args.count, 0.1)
    
    print("=== 测试结束 ===")
