#!/usr/bin/tclsh
# 一键终止测试引擎封装

puts "\[TCL Wrapper\] 正在呼叫全局核爆清理进程..."
if {[catch {exec bash [file dirname [info script]]/stop_validation.sh >@ stdout 2>@ stderr} results]} {
    puts "清理遇到问题: $results"
}
