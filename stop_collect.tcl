#!/usr/bin/tclsh
# IronBuddy 数据采集模式 — 一键停止
puts "\[IronBuddy\] 停止数据采集中..."
set script_dir [file dirname [info script]]
if {[catch {exec bash "$script_dir/stop_collect.sh" >@ stdout 2>@ stderr} err]} {
    puts "  停止异常: $err"
} else {
    puts "  采集服务已安全关闭。"
}
