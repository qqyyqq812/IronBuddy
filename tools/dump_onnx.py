import onnx
m = onnx.load('rtmpose-m_simcc-aicrowd_256x192.onnx')
with open('nodes_dump.txt', 'w') as f:
    for i, n in enumerate(m.graph.node):
        f.write(f"[{i}] {n.name} | op: {n.op_type} | in: {n.input} | out: {n.output}\n")
print("Done dumping nodes.")
