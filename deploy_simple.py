# -*- coding: utf-8 -*-
import paramiko
import os

HOST = "124.223.111.59"
PORT = 22
USER = "root"
PASS = "y4694x5116D@1"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
FILES = ["pair_scan.py", "webapp.py"]

print(f"==> 连接 {USER}@{HOST}:{PORT} ...")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, port=PORT, username=USER, password=PASS, timeout=15)
print("    SSH 连接成功")

# Find remote dir
stdin, stdout, stderr = ssh.exec_command("find / -maxdepth 4 -name webapp.py -path '*/quant-system/*' 2>/dev/null | head -1")
out = stdout.read().decode().strip()
if out:
    remote_dir = os.path.dirname(out)
else:
    for candidate in ["/root/quant-system", "/opt/quant-system", "/home/quant-system"]:
        stdin, stdout, stderr = ssh.exec_command(f"test -d {candidate} && echo OK")
        if stdout.read().decode().strip() == "OK":
            remote_dir = candidate
            break
    else:
        print("ERROR: 找不到远程目录")
        ssh.close()
        exit(1)

print(f"    远程目录: {remote_dir}")

sftp = ssh.open_sftp()
for fname in FILES:
    local = os.path.join(LOCAL_DIR, fname)
    remote = f"{remote_dir}/{fname}"
    print(f"    上传 {fname} ...", end=" ")
    sftp.put(local, remote)
    print("OK")
sftp.close()

print("    重启 quant-web 服务 ...")
ssh.exec_command("systemctl restart quant-web 2>&1 || true")
import time
for i in range(3, 0, -1):
    time.sleep(1)
stdin, stdout, stderr = ssh.exec_command("systemctl is-active quant-web 2>&1")
print(f"    服务状态: {stdout.read().decode().strip()}")

stdin, stdout, stderr = ssh.exec_command("curl -sS http://127.0.0.1:8787/api/overview 2>&1 | head -c 200")
print(f"    API 响应: {stdout.read().decode().strip()[:200]}")

ssh.close()
print("==> 部署完成")
