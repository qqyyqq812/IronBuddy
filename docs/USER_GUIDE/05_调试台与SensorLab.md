# 05 调试台与 SensorLab

本章说明调试台和 SensorLab 的使用边界。调试台用于看系统状态，SensorLab
用于看传感器、波形、采集和训练链路。

## 调试台入口

管理和调试能力主要由主页面、管理页和后台 API 提供。现场只展示必要状态，
避免把内部日志当成主要卖点。

- 管理页：`http://<BOARD_IP>:5000/admin`
- 服务状态：`/api/admin/services`
- 系统信息：`/api/admin/system_info`
- 日志接口：`/api/admin/logs`
- 调试工作台数据：`/api/demo/debug_workbench`

## SensorLab 入口

SensorLab 是本机调试界面，默认用于采集、分析和训练个人数据。它和主页面
不是同一个端口。

```bash
python tools/ironbuddy_sensor_lab.py --board-ip <BOARD_IP>
```

启动后通常访问：

```text
http://127.0.0.1:8766/
```

## 重点观察项

调试时先看链路是否在线，再看模型结果是否合理。

- 板端连接状态。
- 主页面优先展示原始 ADC 波形，用来确认真实传感器是否在刷新。
- SensorLab 继续展示原始 EMG 和滤波后 EMG 波形，适合深入排查。
- FSM 角度和 rep 计数。
- GRU 推理窗口和分类结果。
- 采集、导出、训练和部署按钮状态。

## 状态说明

调试台里的 **CPU 温度** 来自板端 `thermal_zone0`。**视觉 / GPU** 优先看
主页面视觉帧是否在刷新；如果只显示 SSH GPU 未确认，不等于视频推理一定离线。

## 验收建议

如果现场时间有限，SensorLab 只展示一个完整证据链即可。

1. 打开 SensorLab。
2. 确认板端状态为在线。
3. 展示原始和处理后波形。
4. 录制一小组动作。
5. 查看组结果和导出状态。

## 截图占位

以下截图位先使用本地占位图，后续替换为真实页面截图。

| 截图位 | 画面 | 占位 |
| --- | --- | --- |
| 05-1 | 管理页服务状态 | <img src="images/screenshot-placeholder.svg" alt="管理页服务状态截图占位" width="220"> |
| 05-2 | 调试指标卡片 | <img src="images/screenshot-placeholder.svg" alt="调试指标卡片截图占位" width="220"> |
| 05-3 | 合并日志视图 | <img src="images/screenshot-placeholder.svg" alt="合并日志视图截图占位" width="220"> |
| 05-4 | SensorLab 波形 | <img src="images/screenshot-placeholder.svg" alt="SensorLab 波形截图占位" width="220"> |
| 05-5 | SensorLab 录制结果 | <img src="images/screenshot-placeholder.svg" alt="SensorLab 录制结果截图占位" width="220"> |

## 下一步

调试链路确认后，继续阅读 [06 硬件部署](06_硬件部署.md)。
