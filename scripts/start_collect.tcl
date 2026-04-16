#!/usr/bin/tclsh
# IronBuddy 数据采集模式 — 一键启动
puts "\[IronBuddy\] 数据采集模式启动中..."
set script_dir [file dirname [info script]]
if {[catch {exec bash "$script_dir/start_collect.sh" >@ stdout 2>@ stderr} err]} {
    puts "  启动异常: $err"
} else {
    puts "  采集服务启动完成。"
}
