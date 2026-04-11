#!/usr/bin/tclsh
# IronBuddy V3.0 一键停止 (含SSH隧道清理)
puts "\[IronBuddy\] 停止中..."
set script_dir [file dirname [info script]]
if {[catch {exec bash "$script_dir/stop_validation.sh" >@ stdout 2>@ stderr} err]} {
    puts "  停止异常: $err"
} else {
    puts "  已安全关闭。"
}
