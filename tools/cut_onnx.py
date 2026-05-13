import onnx
import numpy as np
from onnx import helper, numpy_helper

print("Loading ONNX model...")
m = onnx.load('rtmpose-m_simcc-aicrowd_256x192.onnx')

print("Extracting SimCC head weights...")
weight_x = None
weight_y = None

# 找到 Initializer 里的权重
for init in m.graph.initializer:
    if init.name == 'onnx::MatMul_872':
        weight_x = numpy_helper.to_array(init)
    elif init.name == 'onnx::MatMul_873':
        weight_y = numpy_helper.to_array(init)

if weight_x is not None:
    np.save('simcc_weight_x.npy', weight_x)
    print(f"Saved simcc_weight_x.npy, shape: {weight_x.shape}")
if weight_y is not None:
    np.save('simcc_weight_y.npy', weight_y)
    print(f"Saved simcc_weight_y.npy, shape: {weight_y.shape}")

print("Modifying ONNX graph outputs...")
# 我们只需要 onnx::MatMul_687 这个输出
# 找到原有对应的 ValueInfo
feature_info = None
for val in m.graph.value_info:
    if val.name == 'onnx::MatMul_687':
        feature_info = val
        break

# 如果找不到，可以基于已知创建
if feature_info is None:
    print("Creating new ValueInfo for onnx::MatMul_687...")
    feature_info = helper.make_tensor_value_info('onnx::MatMul_687', onnx.TensorProto.FLOAT, None)

# 清除旧的 output
while len(m.graph.output) > 0:
    m.graph.output.pop()

# 添加新的 output
m.graph.output.extend([feature_info])

# 我们可以选择安全地删除最后两个 MatMul 节点，或者让 ONNX Optimizer / RKNN parser 死鸟不理它
# 为了干净，删掉
nodes_to_remove = []
for n in m.graph.node:
    if n.name in ['MatMul_252', 'MatMul_253']:
        nodes_to_remove.append(n)

for n in nodes_to_remove:
    m.graph.node.remove(n)

new_model_path = 'headless_rtmpose.onnx'
print(f"Saving modified ONNX to {new_model_path} ...")
onnx.save(m, new_model_path)
print("Done!")
