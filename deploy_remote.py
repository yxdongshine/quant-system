# -*- coding: utf-8 -*-
"""部署脚本：将本地 quant-system 更新推送到远程服务器并重启服务。"""
import paramiko
import os
import sys
import glob

HOST = os.environ.get("DEPLOY_HOST", "124.223.111.59")
PORT = int(os.environ.get("DEPLOY_PORT", "22"))
USER = os.environ.get("DEPLOY_USER", "root")
PASS = os.environ.get("DEPLOY_PASS", "")
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))

# 需要上传的核心文件列表
UPLOAD_FILES = [
    "webapp.py",
    "config.py",
    "signals.py",
    "datafeed.py",
    "indicators.py",
    "backtest.py",
    "prediction.py",
    "sector_rotation.py",
    "pair_scan.py",
    "watchlist.json",
    "params.json",
]


def ssh_exec(ssh, cmd):
    """执行远程命令并返回输出。"""
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return out, err


def main():
    print(f"==> 连接 {USER}@{HOST}:{PORT} ...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PASS, timeout=15)
    print("    SSH 连接成功")

    # 1) 查找远程 quant-system 目录
    out, err = ssh_exec(ssh, "find / -maxdepth 4 -name webapp.py -path '*/quant-system/*' 2>/dev/null | head -1")
    if out:
        remote_dir = os.path.dirname(out)
        print(f"    远程目录: {remote_dir}")
    else:
        # 尝试常见路径
        for candidate in ["/root/quant-system", "/opt/quant-system", "/home/quant-system"]:
            out2, _ = ssh_exec(ssh, f"test -d {candidate} && echo OK")
            if out2 == "OK":
                remote_dir = candidate
                print(f"    远程目录: {remote_dir}")
                break
        else:
            print("ERROR: 找不到远程 quant-system 目录，请手动指定")
            ssh.close()
            sys.exit(1)

    # 2) 上传核心文件
    sftp = ssh.open_sftp()
    uploaded = 0

    for fname in UPLOAD_FILES:
        local_path = os.path.join(LOCAL_DIR, fname)
        remote_path = f"{remote_dir}/{fname}"
        if os.path.exists(local_path):
            print(f"    上传 {fname} ...", end=" ")
            sftp.put(local_path, remote_path)
            print("OK")
            uploaded += 1

    # 3) 上传所有 CSV 数据文件（全市场扫描需要完整数据）
    data_dir = os.path.join(LOCAL_DIR, "data")
    remote_data_dir = f"{remote_dir}/data"
    ssh_exec(ssh, f"mkdir -p {remote_data_dir}")

    csv_files = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    print(f"    本地 CSV 文件数: {len(csv_files)}")

    # 检查远程已有 CSV，只上传新增/修改的
    remote_csvs_str, _ = ssh_exec(ssh, f"ls {remote_data_dir}/*.csv 2>/dev/null | xargs -I{{}} basename {{}}")
    remote_csvs = set(remote_csvs_str.split()) if remote_csvs_str else set()
    print(f"    远程已有 CSV: {len(remote_csvs)} 个")

    upload_count = 0
    for csv_path in csv_files:
        fname = os.path.basename(csv_path)
        remote_path = f"{remote_data_dir}/{fname}"
        if fname not in remote_csvs:
            print(f"    上传 data/{fname} ...", end=" ")
            sftp.put(csv_path, remote_path)
            print("OK")
            upload_count += 1
            uploaded += 1

    print(f"    新上传 CSV: {upload_count} 个, 跳过已存在: {len(csv_files) - upload_count} 个")

    # 4) 上传预测数据文件
    pred_file = os.path.join(data_dir, "predictions.json")
    if os.path.exists(pred_file):
        print(f"    上传 data/predictions.json ...", end=" ")
        sftp.put(pred_file, f"{remote_data_dir}/predictions.json")
        print("OK")
        uploaded += 1

    # 4b) 上传预测准确率文件
    acc_file = os.path.join(data_dir, "prediction_accuracy.json")
    if os.path.exists(acc_file):
        print(f"    上传 data/prediction_accuracy.json ...", end=" ")
        sftp.put(acc_file, f"{remote_data_dir}/prediction_accuracy.json")
        print("OK")
        uploaded += 1

    # 4c) 上传板块轮动扫描结果
    rot_file = os.path.join(data_dir, "sector_rotation.json")
    if os.path.exists(rot_file):
        print(f"    上传 data/sector_rotation.json ...", end=" ")
        sftp.put(rot_file, f"{remote_data_dir}/sector_rotation.json")
        print("OK")
        uploaded += 1

    print(f"    共上传 {uploaded} 个文件")

    sftp.close()

    # 4) 重启服务
    print("    重启 quant-web 服务 ...")
    out, err = ssh_exec(ssh, "systemctl restart quant-web 2>&1 || true")
    if err:
        print(f"    systemctl restart 输出: {err}")
    import time as _t
    _t.sleep(3)
    out, err = ssh_exec(ssh, "systemctl is-active quant-web 2>&1")
    print(f"    服务状态: {out}")

    # 5) 验证
    _t.sleep(2)
    out, err = ssh_exec(ssh, f"curl -sS http://127.0.0.1:8787/api/overview 2>&1 | head -c 200")
    print(f"    API 响应: {out[:200]}")

    # 6) 验证市场扫描 API
    _t.sleep(1)
    out, err = ssh_exec(ssh, f"curl -sS http://127.0.0.1:8787/api/market_scan 2>&1 | head -c 300")
    print(f"    市场扫描 API 响应: {out[:300]}")

    ssh.close()
    print(f"==> 部署完成！访问 http://{HOST}:8787")


if __name__ == "__main__":
    main()
