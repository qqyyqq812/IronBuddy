#!/usr/bin/tclsh
# 一键启动测试引擎封装 (适配用户的 tcl 工作流偏好)
# 本质是跨系统调用精细配置过的 bash 工作流

puts "\[TCL Wrapper\] 正在分发验证环境启动指令..."
if {[catch {exec bash [file dirname [info script]]/start_validation.sh >@ stdout 2>@ stderr} results]} {
    puts "启动遇到问题: $results"
}
