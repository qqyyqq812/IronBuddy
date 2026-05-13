#!/usr/bin/tclsh
# IronBuddy V3.0 静音版一键启动 (图书馆 demo 专用)
# 与 start_validation.tcl 完全独立, 不互相影响
#
# 区别:
#   - 不启动 voice_daemon (无 arecord / 无 aplay / 无 TTS)
#   - 板端系统级静音 amixer (双保险, 即使误启 TTS 也听不到)
#   - 写入 mute_signal.json (muted=true), 第三重保险
#   - 其他 4 服务 (vision/streamer/mainloop/emg) + 云端 GPU 照常
#
# 使用: tclsh start_silent.tcl
puts "\[IronBuddy\] 静音版启动中 (图书馆模式)..."
set script_dir [file dirname [info script]]
if {[catch {exec bash "$script_dir/start_silent.sh" >@ stdout 2>@ stderr} err]} {
    puts "  静音启动异常: $err"
} else {
    puts "  静音启动完成。语音模块已跳过, 系统静音已锁定。"
}
