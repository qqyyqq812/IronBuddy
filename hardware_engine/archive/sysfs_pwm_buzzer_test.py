import os
import time
import argparse

def test_buzzer(pin=153, freq_hz=500, duration=2.0):
    gpio_path = f"/sys/class/gpio/gpio{pin}"
    print(f">>> 正在向 GPIO {pin} 发射 {freq_hz}Hz PWM 方波 (持续 {duration} 秒)...")
    print(">>> 如果此时发出宏亮且连贯的“滴——”声，说明您的是【无源蜂鸣器】！")

    if not os.path.exists(gpio_path):
        try:
            with open('/sys/class/gpio/export', 'w') as f:
                f.write(str(pin))
        except Exception as e:
            pass
        time.sleep(0.1)

    try:
        with open(f"{gpio_path}/direction", 'w') as f:
            f.write('out')
    except Exception as e:
        print(f"设置输出方向失败: {e}")
        return

    half_period = 1.0 / freq_hz / 2.0
    end_time = time.time() + duration

    try:
        with open(f"{gpio_path}/value", 'w') as f:
            while time.time() < end_time:
                f.write('1')
                f.flush()
                time.sleep(half_period)
                f.write('0')
                f.flush()
                time.sleep(half_period)
            
            # 停止发声并置低电平
            f.write('0')
            f.flush()
    except Exception as e:
        print(f"写入电平异常: {e}")

    print(">>> 测试结束。\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser("无源蜂鸣器频率方波诊断工具")
    parser.add_argument('--pin', type=int, default=153)
    args = parser.parse_args()
    test_buzzer(pin=args.pin)
