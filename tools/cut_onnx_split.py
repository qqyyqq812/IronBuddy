import onnx
from onnx import utils

def split_model():
    print("Loading original model...")
    m = onnx.load('rtmpose-m_simcc-aicrowd_256x192.onnx')
    
    # We want to extract two submodels using onnx.utils.extract_model
    # Part 1: Backbone (Input to onnx::Shape_625)
    print("Extracting backbone...")
    onnx.utils.extract_model('rtmpose-m_simcc-aicrowd_256x192.onnx', 'rtmpose_backbone.onnx', ['input'], ['onnx::Shape_625'])
    
    # Part 2: Head (onnx::Shape_625 to simcc_x, simcc_y)
    print("Extracting head...")
    onnx.utils.extract_model('rtmpose-m_simcc-aicrowd_256x192.onnx', 'rtmpose_head.onnx', ['onnx::Shape_625'], ['simcc_x', 'simcc_y'])
    
    print("Checking models...")
    b = onnx.load('rtmpose_backbone.onnx')
    print("Backbone Output:", b.graph.output[0].name)
    
    h = onnx.load('rtmpose_head.onnx')
    print("Head Input:", h.graph.input[0].name)
    print("Done!")

import onnx.utils
split_model()
