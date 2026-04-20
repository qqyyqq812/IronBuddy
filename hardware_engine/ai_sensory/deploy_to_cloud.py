import pexpect
import sys

password = 'OmloGl2JXBK0'
host = 'root@connect.westd.seetacloud.com'
port = '42924'  # V4.5 2026-04-18 新实例端口

# 1. SCP 上传 Cloud_RTMPose_Receiver.py
print("开始上传 Receiver 到云端...")
child = pexpect.spawn(f'scp -o StrictHostKeyChecking=no -P {port} hardware_engine/ai_sensory/Cloud_RTMPose_Receiver.py {host}:/root/')
idx = child.expect(['assword:', pexpect.EOF, pexpect.TIMEOUT], timeout=15)
if idx == 0:
    child.sendline(password)
    child.expect(pexpect.EOF)
    print("上传完成。")
else:
    print("上传失败。")

# 2. 远端执行 Systemd 挂载（使用一键 heredoc 写入配置并重载）
print("开始配置远端 Systemd 服务...")
ssh_child = pexpect.spawn(f'ssh -o StrictHostKeyChecking=no -p {port} {host}')
idx = ssh_child.expect(['assword:', pexpect.EOF, pexpect.TIMEOUT], timeout=15)
if idx == 0:
    ssh_child.sendline(password)
    ssh_child.expect('#') # 假设 root 登录提示符为 #
    
    # 写入 systemd
    setup_cmds = """
cat << 'EOF' > /etc/systemd/system/rtmpose.service
[Unit]
Description=RTMPose UDP Bridge Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root
ExecStart=/usr/bin/python3 /root/Cloud_RTMPose_Receiver.py
Restart=on-failure
RestartSec=5s
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=rtmpose-bridge

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable rtmpose
systemctl restart rtmpose
"""
    ssh_child.sendline(setup_cmds)
    ssh_child.expect('#')
    ssh_child.sendline('exit')
    ssh_child.expect(pexpect.EOF)
    print("远端 Systemd 部署并重启成功！")
else:
    print("远端连接失败。")
