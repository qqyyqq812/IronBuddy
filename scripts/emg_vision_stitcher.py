import os
import time
import json
import threading
import sys
import termios
import tty

# Set default mode
current_mode = "golden"
mode_map = {
    'g': ("golden", 85, 10),      # Target: 85%, Comp: 10%
    'b': ("bad_lazy", 20, 20),    # Target: 20%, Comp: 20%
    'c': ("bad_comp", 30, 80)     # Target: 30%, Comp: 80%
}

def get_char():
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch

def keyboard_listener():
    global current_mode
    print("\n🎭 [STAGE MAGIC] 舞台魔法：后台视觉-肌电缝合器已启动 🎭")
    print("---------------------------------------")
    print("操作面板（直接在键盘上敲以下字母即可，无需按回车）：")
    print("  [g] -> 切换为 '标准深蹲 (Golden)' (目标肌 85%，无代偿)")
    print("  [b] -> 切换为 '半蹲偷懒 (Bad Lazy)' (目标肌 20%，无代偿)")
    print("  [c] -> 切换为 '代偿深蹲 (Bad Comp)' (目标肌 30%，代偿 80%)")
    print("  [q] -> 退出销毁")
    print("\n当前暗门模式: 🟢 Golden，正在静默生成心跳...\n")
    
    while True:
        try:
            ch = get_char().lower()
            if ch in mode_map:
                current_mode = mode_map[ch][0]
                emoji = "🟢" if ch == 'g' else "🔴" if ch == 'b' else "⚠️"
                print(f"\r[{time.strftime('%H:%M:%S')}] {emoji} 底层肌电阀门已悄悄切换为: {current_mode.upper()}模式".ljust(50))
            elif ch == 'q' or ch == '\x03':  # Ctrl+C
                print("\r\n[EXIT] 退出缝合模式，清理战场...")
                os._exit(0)
        except Exception:
            pass

def data_stitcher():
    global current_mode
    while True:
        now = time.time()
        
        # We need to simulate the exact JSON format that udp_emg_server outputs for squat
        _, target_pct, comp_pct = mode_map[[k for k, v in mode_map.items() if v[0] == current_mode][0]]

        exercise = "squat"
        acts = {
            "quadriceps": target_pct,
            "glutes": target_pct,
            "calves": 0,
            "biceps": comp_pct  # Mapping rule based on udp_emg_server.py fallback logic
        }
        
        out = {"activations": acts, "warnings": [], "exercise": exercise}
        
        try:
            with open('/dev/shm/muscle_activation.json.tmp', 'w') as f:
                json.dump(out, f)
            os.rename('/dev/shm/muscle_activation.json.tmp', '/dev/shm/muscle_activation.json')
            
            # Update heartbeat so that collect_training_data and Web UI know we have "hardware" alive
            with open('/dev/shm/emg_heartbeat', 'w') as f:
                f.write(str(now))
        except Exception:
            pass
        
        # 20Hz update rate
        time.sleep(0.05)

if __name__ == "__main__":
    t_stitch = threading.Thread(target=data_stitcher, daemon=True)
    t_stitch.start()
    
    # Run keyboard listener on main thread
    keyboard_listener()
