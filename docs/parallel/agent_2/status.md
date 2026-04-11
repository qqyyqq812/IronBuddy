# 进度白板：Agent 2 — NPU 视觉感知强化部
> ⏱️ 汇报时间：2026-04-06 23:15

## ✅ 已完成的里程碑
- [x] 开发核心平滑算法组件 (`hardware_engine/ai_sensory/vision/filters.py`)
  - 实现 `LowPassFilter` 和 `OneEuroFilter` 结构，成功熨平毫秒级抖动噪声点。
- [x] 开发全新推流器极速核心 (`hardware_engine/ai_sensory/vision/rtmpose_publisher.py`)
  - 完成 OpenCV 流对接并彻底对接回了原厂 JSON 的 IPC 喂食系统，包含本地伪造沙盒双验证通过。
- [x] 原始模型的本地化剥离下载
  - 已将官方未缩水的 54MB `rtmpose-m_simcc-aicrowd.onnx` 下送至本地 `tools/`。
- [x] 环境基建与异常排雷 (`rknn_toolkit_docker.md`)
  - 清查出所有公开 rknn Docker 现已被封，正式落定使用纯净 Conda python3.6 本地强行构建体系，并编写了转化挂载代码 (`convert_rtmpose.py`)。

## 🔄 进行中
- [/] **RKNN 量化转换环境本机孤岛构建** — 进度 0% 
  - 等待执行：基于 Conda 下载拉起 `rknn-toolkit v1.7.3` 实体库并运行编译。

## ⏳ 待启动
- [ ] 量化转换与输出 `rtmpose_quant.rknn`
- [ ] 连接 `10.105.245.224` 板载
- [ ] 斩断旧主干代码（移除 YOLOv5 的 C++ main）
- [ ] 替入 Python 端的新 Publisher 并联调结果 

## 📢 对外依赖
- 无。独立阻击。
