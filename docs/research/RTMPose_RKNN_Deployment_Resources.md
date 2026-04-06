# RTMPose RKNN (Rockchip) 部署资源与可行性深度调研报告
> 🔍 搜索轮次：2 轮并广度搜索 | 生成时间：2026-04-04 

## 一句话结论
**目前互联网和开源社区中，【没有】可以直接下载并上传给 RK3399ProX 直接运行的现成 `.rknn` 模型文件。** 如果要在本项目中使用 RTMPose，我们必须自行走完 `PyTorch -> ONNX -> rknn-toolkit` 的转换与量化流程。

## 核心知识梳理（为什么没有现成的包？）

1. **硬件与工具链的严重碎片化**
   * RK3399Pro (含 Toybrick Board) 使用的是**第一代 NPU (NPU V1)**，必须使用老旧的 `rknn-toolkit` (约 1.7.x 版本) 进行边缘计算的模型转换。
   * 而市面上绝大多数的新教程、以及目前 Github 社区中私下分享的模型，都是基于 RK3588、RK3566、RV1106 使用的 **NPU V2 / rknn-toolkit2**。
   * `rknn-toolkit` 与 `rknn-toolkit2` 的 `.rknn` 格式**完全不兼容**。因此别人编译好的 RTMPose `.rknn` 我们无法在 RK3399Pro 上启动。
2. **官方模型库 (rknn_model_zoo) 的缺位**
   * Rockchip 官方主导的 `airockchip/rknn_model_zoo` 截止目前并没有原生收录 RTMPose 的官方实现，目前他们姿态估计的主力官方样例仍旧是 `YOLOv8-Pose`。
   * 开源社区的 `RK_VideoPipe` 等第三方库虽然部分支持 RTMPose 后处理，但同样不提供针对老旧 V1 NPU 的全封装二进制件。

## 方案对比：我们该如何推进？

| 方案 | 流程 | 优势 | 劣势 (针对我们的项目) |
|---|---|---|---|
| **寻找现成 `.rknn`** | Github搜索 -> 下载发布版 -> 测试 | 即插即用 | 根本找不到匹配 RK3399Pro 的版本。极大概率白费力气。 |
| **官方 rknn_model_zoo** | 拉取官方开源仓 -> 编译 C++ | 有官方技术保障 | 官方不支持 RTMPose，Issue 322 仍未解决。 |
| **自主转换部署 (必由之路)** | MMPose -> MMDeploy 转 ONNX -> x86 PC 上使用 `rknn-toolkit(v1)` 进行 INT8/FP16 量化转换 | 完全自主把控模型精度，可以采用 SimCC 解码 | 流程极其繁琐，且 RTMPose (SimCC) 使用 INT8 静态量化时有严重掉点问题，可能需要回退 FP16。 |

## 避坑清单 (INT8 量化陷阱)
- ⚠️ **SimCC INT8 精度雪崩**：据学术界 (Arxiv) 和 OpenMMLab 社区反馈，RTMPose 依赖的 SimCC 1D 张量解码结构，在经过无损 PTQ（训练后静态量化）为 INT8 时，坐标判定容易彻底失效。
- ⚠️ **解决方案**：在将其转换为 RKNN 时，对于 RK3399Pro，强烈建议在转模型代码中声明 `do_quantization=False` 或强制转为 `FP16` 以保全模型输出精度，避免出现“看得见模型，吐不出坐标”的问题。

## 进一步探索方向
因为**必定需要我们自己转换模型**，后续唯一的实施路径是：
1. 在高配独立 Linux 服务器（或者带强劲 CPU 的 x86 机器）上部署 Python `rknn-toolkit 1.7.5` 环境。
2. 下载包含 SimCC 结构的 RTMPose 预训练 `.pth` 权重，通过 `mmpose / mmdeploy` 将其静态化导出为 `.onnx`。
3. 走自定义脚本完成 RKNN 编译注入。
4. 将编译好的专属 `.rknn` 文件配合 Agent 2 提供的 `rtmpose_postprocessing.py` 上板联调。

## 参考来源
1. OpenMMLab / MMDeploy 官方文档
2. airockchip / rknn_model_zoo GitHub Issues (#322)
3. Rockchip NPU 社区针对 RTMPose 的部署与报错讨论
