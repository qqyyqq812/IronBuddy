# Entity Graph & Code Topology (Last updated: 2026-04-11)

## Architecture Overview

```
Board (RK3399ProX)                     Cloud (AutoDL RTX 5090)
──────────────────                     ────────────────────────
cloud_rtmpose_client.py ──SSH tunnel──> rtmpose_http_server.py
  ├─ camera capture (USB webcam)         └─ ONNX Runtime GPU
  ├─ async HTTP POST /infer               └─ RTMPose-m body7
  ├─ simulated EMG generation           
  ├─ writes /dev/shm/pose_data.json    
  ├─ writes /dev/shm/result.jpg        
  └─ writes /dev/shm/muscle_activation.json

main_claw_loop.py (asyncio FSM)
  ├─ reads pose_data.json → SquatStateMachine / DumbbellCurlFSM
  ├─ reads muscle_activation.json → EMG features
  ├─ GRU inference (fusion_model.py, when trained)
  ├─ writes fsm_state.json
  ├─ DeepSeek via OpenClawBridge (WebSocket → WSL:18789)
  └─ auto-triggers DeepSeek at fatigue=1500, then resets

streamer_app.py (Flask :5000)
  ├─ /video_feed → MJPEG from result.jpg
  ├─ /state_feed → fsm_state.json
  ├─ /api/nn_inference → similarity/classification
  ├─ /api/chat, /trigger_deepseek → LLM interaction
  └─ /api/user_profile → exercise mode switch

voice_daemon.py
  ├─ arecord hw:Webcam,0 mono 16kHz
  ├─ Google ASR (Vosk ABI broken)
  └─ wake word "教练" → chat_input.txt

udp_emg_server.py
  ├─ listens UDP :8080 for ESP32 BLE→WiFi EMG
  └─ only writes when connected (otherwise simulation takes over)
```

## Key Files
- `start_validation.sh` — one-click deploy (cloud tunnel + 5 services)
- `cloud_deploy/rtmpose_http_server.py` — cloud ONNX inference server
- `cognitive/fusion_model.py` — 7D GRU model (similarity + 3-class)
- `tools/collect_training_data.py` — real-time data collection
- `tools/train_model.py` — GRU training pipeline

## Known Issues (2026-04-11)
- RKNN quantized NPU model has very low confidence (unusable)
- Cloud RTMPose via SSH tunnel ~100ms RTT (direct board→cloud)
- GRU model NOT YET TRAINED (no 7D training data collected)
- Vosk ABI crash on board Python 3.7 (using Google ASR fallback)
