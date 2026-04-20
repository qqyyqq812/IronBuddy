# coding=utf-8
"""
External Dataset Downloader (HSBI Biceps sEMG 自动尝试)
=====================================================

flex-curl-only-pivot plan §1.1 + §1.6 决策：弯举 Only，Camargo/Ninapro 淘汰，
改用 HSBI biceps brachii sEMG (免账号公开) 作为 J1/J2 baseline 兜底。

**用途级别**：L3 only
- ✅ 合法：下载原始 EMG 数据 → 由 compute_family_baselines.py 计算 J1/J2 基线
- ❌ 禁止：用于 Encoder 权重初始化（伪迁移）
- ❌ 禁止：next-step 预训练

功能：
- HSBI biceps (公开免账号)：尝试直连下载 → 解压到
  data/external/hsbi_biceps/ 。反爬/链接失效则 warning 不阻塞，写
  _download_failed.log 指向手动下载步骤。

历史备注：
- Camargo 2021 + Ninapro DB2 曾由本脚本处理，2026-04-18 弯举 Only 转向后整体
  淘汰（深蹲方案取消 + Ninapro 需账号且偏静态手势）。详见 README.md。

使用：
    # 默认：尝试自动下 HSBI
    python tools/download_external_data.py

    # 只 dry-run 看候选 URL
    python tools/download_external_data.py --dry-run

    # 自定义 timeout
    python tools/download_external_data.py --timeout 180
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from typing import List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HSBI_DIR = os.path.join(ROOT, 'data/external/hsbi_biceps')

LOG_PREFIX = "[download_external_data]"

# ---------------------------------------------------------------------------
# HSBI 候选 URL（多 fallback；Bielefeld 启用 Anubis 反爬，可能全被拦到 HTML）
# ---------------------------------------------------------------------------
# 官方记录页: https://pub.uni-bielefeld.de/record/2956029
# DOI:        10.57720/1956
# 真实直链由页面上 "Download" 按钮动态生成（带 token），无法硬编码。
# 以下候选按 Bielefeld PUB 系统的常见 URL 模板尝试，失败则降级到手动指引。
HSBI_CANDIDATE_URLS = [
    # 候选 1: PUB download endpoint（最可能，文件 ID 2956030 是常见 pattern）
    "https://pub.uni-bielefeld.de/download/2956029/2956030/hsbi_biceps.zip",
    # 候选 2: 无文件名的 endpoint（系统推断）
    "https://pub.uni-bielefeld.de/download/2956029/2956030",
    # 候选 3: record/files/ 风格（某些 PUB 实例）
    "https://pub.uni-bielefeld.de/record/2956029/files/hsbi_biceps.zip",
]

HSBI_EXPECTED_MB = 150  # 估值，真实以压缩包为准


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    """带前缀的 stdout 输出。"""
    print(f"{LOG_PREFIX} {msg}", flush=True)


def warn(msg: str) -> None:
    """显眼的 warning 输出。"""
    print(f"{LOG_PREFIX} [WARN] {msg}", flush=True)


def err(msg: str) -> None:
    """显眼的 error 输出（不 raise）。"""
    print(f"{LOG_PREFIX} [ERR]  {msg}", flush=True)


def check_disk_space(target_dir: str, required_gb: float = 2.0) -> bool:
    """检查 target_dir 所在分区剩余空间 >= required_gb。"""
    parent = os.path.dirname(target_dir) if not os.path.exists(target_dir) else target_dir
    while not os.path.exists(parent):
        parent = os.path.dirname(parent)
        if parent in ('/', ''):
            parent = '/'
            break
    usage = shutil.disk_usage(parent)
    free_gb = usage.free / (1024 ** 3)
    log(f"磁盘空间检查：{parent} 剩余 {free_gb:.2f} GB（需要 >= {required_gb} GB）")
    if free_gb < required_gb:
        err(f"磁盘空间不足！剩余 {free_gb:.2f} GB < {required_gb} GB，拒绝下载")
        return False
    return True


def _is_zip_file(path: str) -> bool:
    """快速验证是真 zip 文件（避免 Anubis HTML challenge 当成 zip）。"""
    try:
        with open(path, 'rb') as f:
            magic = f.read(4)
        # ZIP 文件魔数：PK\x03\x04 (normal) / PK\x05\x06 (empty) / PK\x07\x08 (spanned)
        return magic.startswith(b'PK')
    except OSError:
        return False


def download_with_wget(url: str, dest_path: str, timeout_s: int = 120) -> bool:
    """先尝试 wget（某些 Anubis challenge 对 wget UA 放行）。"""
    if shutil.which('wget') is None:
        return False
    log(f"  wget 尝试：{url}")
    try:
        proc = subprocess.run(
            ['wget', '--quiet', '--tries=2', '--timeout=%d' % timeout_s,
             '--user-agent=Mozilla/5.0 (X11; Linux x86_64) Chrome/120',
             '-O', dest_path, url],
            capture_output=True, text=True, timeout=timeout_s + 30)
        if proc.returncode == 0 and os.path.exists(dest_path) \
                and os.path.getsize(dest_path) > 1024:
            return True
        warn(f"  wget exit={proc.returncode}, stderr={proc.stderr[:120]}")
        return False
    except (subprocess.TimeoutExpired, OSError) as e:
        warn(f"  wget 异常：{type(e).__name__}: {str(e)[:100]}")
        return False


def download_with_urllib(url: str, dest_path: str, timeout_s: int = 120,
                        max_retries: int = 2) -> bool:
    """
    urllib 分块下载，带 timeout + 重试。
    """
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout_s)
    try:
        for attempt in range(1, max_retries + 1):
            try:
                log(f"  urllib 尝试 {attempt}/{max_retries}：{url}")
                t0 = time.time()
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120',
                })
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    with open(dest_path, 'wb') as fout:
                        chunk = resp.read(65536)
                        while chunk:
                            fout.write(chunk)
                            if time.time() - t0 > timeout_s:
                                raise socket.timeout("下载耗时超过 timeout")
                            chunk = resp.read(65536)
                elapsed = time.time() - t0
                size_mb = os.path.getsize(dest_path) / (1024 ** 2)
                log(f"  下载结束：{size_mb:.2f} MB / {elapsed:.1f}s")
                return True
            except (urllib.error.URLError, urllib.error.HTTPError,
                    socket.timeout, ConnectionError, TimeoutError) as e:
                err_msg = str(e)[:100]
                warn(f"  urllib 第 {attempt} 次失败：{err_msg}")
                if os.path.exists(dest_path):
                    try:
                        os.remove(dest_path)
                    except OSError:
                        pass
                if attempt < max_retries:
                    time.sleep(2)
            except Exception as e:
                err(f"  未知异常：{type(e).__name__}: {str(e)[:100]}")
                if os.path.exists(dest_path):
                    try:
                        os.remove(dest_path)
                    except OSError:
                        pass
                break
        return False
    finally:
        socket.setdefaulttimeout(old_timeout)


def unzip_and_cleanup(zip_path: str, extract_dir: str) -> bool:
    """解压 zip 到 extract_dir，然后删 zip 节省空间。"""
    try:
        log(f"  解压 {os.path.basename(zip_path)} -> {extract_dir}")
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)
        os.remove(zip_path)
        log(f"  解压完成，已删 zip")
        return True
    except (zipfile.BadZipFile, OSError) as e:
        err(f"  解压失败：{e}")
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass
        return False


def append_fail_log(dataset_dir: str, entry: str) -> None:
    """记录下载失败到 _download_failed.log。"""
    log_path = os.path.join(dataset_dir, '_download_failed.log')
    try:
        os.makedirs(dataset_dir, exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as f:
            ts = time.strftime('%Y-%m-%d %H:%M:%S')
            f.write(f"[{ts}] {entry}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# HSBI 下载主流程
# ---------------------------------------------------------------------------

def download_hsbi(timeout_s: int = 120, dry_run: bool = False) -> None:
    """尝试自动下载 HSBI biceps 数据集；失败 gracefully。"""
    log(f"==== HSBI Biceps sEMG 下载（timeout={timeout_s}s）====")
    log("官方记录页：https://pub.uni-bielefeld.de/record/2956029")
    log("DOI: 10.57720/1956")

    if dry_run:
        log("DRY-RUN 模式：只打印候选 URL，不实际下载")
        log(f"  预期压缩包 ~{HSBI_EXPECTED_MB} MB")
        for i, url in enumerate(HSBI_CANDIDATE_URLS, 1):
            log(f"    候选 {i}: {url}")
        return

    # 已存在数据就跳过
    if os.path.isdir(HSBI_DIR):
        # 任一子目录（S01 等）存在则认为已下载
        subdirs = [d for d in os.listdir(HSBI_DIR)
                   if os.path.isdir(os.path.join(HSBI_DIR, d))
                   and d.startswith('S')]
        if subdirs:
            log(f"HSBI 目录已有 {len(subdirs)} 个 S** 子目录，跳过下载")
            return

    if not check_disk_space(HSBI_DIR, required_gb=2.0):
        err("HSBI 下载中止")
        append_fail_log(HSBI_DIR, "disk_space_insufficient")
        return

    os.makedirs(HSBI_DIR, exist_ok=True)
    zip_path = os.path.join(HSBI_DIR, 'hsbi_biceps.zip')

    downloaded = False
    verified_zip = False
    for i, url in enumerate(HSBI_CANDIDATE_URLS, 1):
        log(f"---- 候选 {i}/{len(HSBI_CANDIDATE_URLS)} ----")

        # 先 wget
        if download_with_wget(url, zip_path, timeout_s=timeout_s):
            downloaded = True
        # wget 没 tool 或 fail，上 urllib
        elif download_with_urllib(url, zip_path, timeout_s=timeout_s,
                                  max_retries=2):
            downloaded = True

        if not downloaded:
            continue

        # 验证是真 zip（防 Anubis HTML challenge）
        if _is_zip_file(zip_path):
            verified_zip = True
            log(f"  候选 {i} 下载的是真 zip 文件，准备解压")
            break
        else:
            size = os.path.getsize(zip_path) if os.path.exists(zip_path) else 0
            warn(f"  候选 {i} 返回的不是 zip（{size} bytes，可能是 Anubis "
                 f"HTML challenge 页），丢弃")
            try:
                os.remove(zip_path)
            except OSError:
                pass
            downloaded = False
            continue

    if not verified_zip:
        err("所有候选 URL 都失败或被反爬拦截")
        append_fail_log(
            HSBI_DIR,
            f"all {len(HSBI_CANDIDATE_URLS)} candidate URLs failed / anubis blocked")
        print()
        print(f"{LOG_PREFIX} " + "=" * 60)
        warn("HSBI 自动下载失败（可能被 Anubis JS challenge 拦截）")
        warn("请按 data/external/hsbi_biceps/README.md 的【手动下载步骤】")
        warn("用浏览器打开 https://pub.uni-bielefeld.de/record/2956029")
        warn("点 Download 按钮，下载后解压到 data/external/hsbi_biceps/")
        print(f"{LOG_PREFIX} " + "=" * 60)
        print()
        return

    # 解压
    if unzip_and_cleanup(zip_path, HSBI_DIR):
        log("==== HSBI 下载 + 解压完成 ====")
    else:
        append_fail_log(HSBI_DIR, "unzip_failed")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="External dataset downloader (HSBI Biceps sEMG)"
    )
    parser.add_argument('--dataset',
                        choices=['hsbi'],
                        default='hsbi',
                        help="要处理的数据集（当前仅支持 hsbi）")
    parser.add_argument('--dry-run', action='store_true',
                        help="只打印候选 URL，不实际下载")
    parser.add_argument('--timeout', type=int, default=120,
                        help="单次下载 timeout 秒数（默认 120）")
    args = parser.parse_args()

    log(f"启动：dataset={args.dataset}, dry_run={args.dry_run}, "
        f"timeout={args.timeout}s")
    log("L3 用途声明：下载的数据只用于 J1/J2 基线 + 噪声谱增强")
    log("红线：禁止用于 Encoder 预训练 / 迁移学习 "
        "(flex-curl-only-pivot §1.6)")

    if args.dataset == 'hsbi':
        try:
            download_hsbi(timeout_s=args.timeout, dry_run=args.dry_run)
        except KeyboardInterrupt:
            warn("用户中断 HSBI 下载")
        except Exception as e:
            err(f"HSBI 下载未预期异常：{type(e).__name__}: {e}")
            append_fail_log(HSBI_DIR, f"unexpected_exception:{type(e).__name__}")

    log("完成。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
