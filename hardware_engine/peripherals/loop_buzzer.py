import os
import time

PIN = 153
print(f"[*] 起博器持续运行中... 给 GPIO {PIN} 发送 1Hz 脉冲")
try:
    if not os.path.exists(f"/sys/class/gpio/gpio{PIN}"):
        with open("/sys/class/gpio/export", "w") as f:
            f.write(str(PIN))
    with open(f"/sys/class/gpio/gpio{PIN}/direction", "w") as f:
        f.write("out")
except Exception as e:
    print(e)

while True:
    try:
        with open(f"/sys/class/gpio/gpio{PIN}/value", "w") as f:
            f.write("0") # ON (Low level trigger)
        time.sleep(0.5)
        with open(f"/sys/class/gpio/gpio{PIN}/value", "w") as f:
            f.write("1") # OFF
        time.sleep(0.5)
    except:
        pass
