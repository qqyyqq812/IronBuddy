#!/usr/bin/tclsh
# IronBuddy V3.0 一键启动 (含云端GPU隧道)
puts "\[IronBuddy\] 启动中..."
set script_dir [file dirname [info script]]
if {[catch {exec bash "$script_dir/start_validation.sh" >@ stdout 2>@ stderr} err]} {
    puts "  启动异常: $err"
} else {
    puts "  启动完成。"
}
