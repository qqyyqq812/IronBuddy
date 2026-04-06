import os
import time

# 查阅最新背面丝印与官方引脚表，该位置为左列自上而下第4根：
# 物理上对应丝印 GPIO4_D1_3V0 (系统编号可能是 153 或 42)
GPIO_PINS = [153, 42, 154, 41]

def setup_buzzer(pin):
    """通过系统文件映射 (Sysfs) 强行征用底层 GPIO"""
    print(f"[-] 正在尝试获取 Linux 底层 GPIO {pin} 的控制权...")
    try:
        if not os.path.exists(f"/sys/class/gpio/gpio{pin}"):
            with open("/sys/class/gpio/export", "w") as f:
                f.write(str(pin))
        # 设置为数字输出模式
        with open(f"/sys/class/gpio/gpio{pin}/direction", "w") as f:
            f.write("out")
        return True
    except OSError as e:
        print(f"   [x] GPIO {pin} 被内核拒绝或不存在: {e}")
        return False

def buzzer_on(pin):
    """
    点火！
    主板原生物理排针直接拉低到了 0V。
    蜂鸣器模块内的 PNP 三极管瞬间导通，内部晶振获得 3.3V VCC 开始剧烈长鸣！
    """
    with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f:
        f.write("0")

def buzzer_off(pin):
    """
    静音！
    主板将排针电平拉回 3.0V。对于 3.3V 模块来说这等同于数字高。
    三极管截止，死寂。
    """
    with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f:
        f.write("1")

if __name__ == "__main__":
    print(f"========== RK3399ProX 蜂鸣器物理急停器实弹测试 ==========")
    print("Agent 1 提示：请确保蜂鸣器 VCC 接入了 3V3，GND 接实，且 I/O 插入了左列向下第 4 根线！")
    
    valid_pin = None
    for p in GPIO_PINS:
        if setup_buzzer(p):
            valid_pin = p
            print(f"[√] 成功锁定系统可用针脚：GPIO {valid_pin} ！")
            break
            
    if not valid_pin:
        print("❌ 所有预设管脚（153, 42, 154, 41）均无法获得内核导出许可。")
        exit(1)
    
    # 确保上电时处于静音态
    buzzer_off(valid_pin) 
    time.sleep(1)
    
    print("\n🚨 [深蹲警告] 姿势太差，拉响警报！(BEEP ON - LOW CORD)")
    buzzer_on(valid_pin)
    time.sleep(0.5)
    
    print("🔇 停止警报。(BEEP OFF)")
    buzzer_off(valid_pin)
    time.sleep(0.5)
    
    print("\n🚨 [再一次警告] (BEEP ON)")
    buzzer_on(valid_pin)
    time.sleep(0.5)
    
    print("🔇 恢复静默。")
    buzzer_off(valid_pin)
    
    # 用完后释放引脚控制权以免死锁
    try:
        with open("/sys/class/gpio/unexport", "w") as f:
            f.write(str(valid_pin))
    except:
        pass
    print("========== 硬件直连通信测试完成 ==========")
