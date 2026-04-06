#!/usr/bin/tclsh
# IronBuddy V3.0 一键突击启动探针 (适配用户的 tcl 工作流偏好)
# 作者: Agent (Mode A 深度优化版)

puts "\[TCL Wrapper\] 探测到 V3.0 前端流出现断层，正在强制接管板级总控权限..."
puts "\[TCL Wrapper\] 正在唤醒底层纯净启动库 start_validation.sh ..."
if {[catch {exec bash [file dirname [info script]]/start_validation.sh >@ stdout 2>@ stderr} results]} {
    puts "⚠️  [致命] 板级神经引爆失败: $results"
} else {
    puts "🎯  \[成功\] 侦查兵撤退，请在 10.105.245.224:5000 检阅成果。"
}
