FROM ubuntu:18.04

# 声明为非交互模式防止卡死
ENV DEBIAN_FRONTEND=noninteractive

# 【防呆提速补丁】替换阿里云镜像源，解决国内拉取极其缓慢导致卡死几百秒的问题
RUN sed -i s@/archive.ubuntu.com/@/mirrors.aliyun.com/@g /etc/apt/sources.list && \
    sed -i s@/security.ubuntu.com/@/mirrors.aliyun.com/@g /etc/apt/sources.list

# 安装必要的古董级底层 C++ 库
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.6 python3.6-dev python3-pip \
    libglib2.0-0 libsm6 libxext6 libxrender-dev \
    wget zip unzip curl git cmake build-essential \
    && rm -rf /var/lib/apt/lists/*

# 为 python3.6 设置系统默认别名
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.6 1 && \
    update-alternatives --install /usr/bin/python python /usr/bin/python3.6 1

# 更新古董版本 pip 适配 TLS 环境 (挂载清华源加速)
RUN python3 -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --upgrade pip==21.3.1 setuptools==59.6.0 wheel==0.37.1

# 提前布置好 RKNN v1 所高度依赖的旧版本 AI 包底层
# 锁定 TF 1.14 与 numpy 1.16 是能够驱动 rknn-toolkit 1.7 的基石
RUN python3 -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    tensorflow==1.14.0 \
    numpy==1.16.6 \
    opencv-python==4.3.0.38 \
    scipy==1.4.1 \
    h5py==2.10.0 \
    grpcio==1.32.0 \
    protobuf==3.19.0

# 配置外挂车间挂载点
WORKDIR /workspace

# 留门给总监：进入后可手工安装 rknn-toolkit-1.7.5 的私有 WHL 轮子
CMD ["/bin/bash"]
