#!/usr/bin/tclsh
puts "\[TCL Wrapper\] 正在下令回收所有 V3.0 板端总控权限..."
if {[catch {exec bash [file dirname [info script]]/stop_validation.sh >@ stdout 2>@ stderr} results]} {
    puts "⚠️  回收时发生错误: $results"
} else {
    puts "🎯  \[成功\] 实验安全终止，资源已释放。"
}
