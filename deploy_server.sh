#!/usr/bin/env bash
# 持仓纪律系统 - 服务器一键部署脚本
# 用法：sudo bash deploy_server.sh（本目录需包含全部 .py / data/ / params.json）
set -e
cd "$(dirname "$0")"
echo "==> 部署目录: $(pwd)"

# 0) 基础检查
command -v python3 >/dev/null || { echo "缺少 python3"; exit 1; }
command -v curl     >/dev/null || { echo "缺少 curl";     exit 1; }
command -v unzip >/dev/null || {
  echo "==> 安装 unzip"
  (apt-get install -y unzip 2>/dev/null || yum install -y unzip) >/dev/null 2>&1 || true
}
# 时区：cron 按北京时间 15:20 设计。任意 +0800 时区（Shanghai/Beijing 等）均可；
# 否则自动校正（幂等，需 root）。
if ! timedatectl 2>/dev/null | grep -q "+0800"; then
  timedatectl set-timezone Asia/Shanghai && echo "==> 时区已校正为 Asia/Shanghai (+0800)"
fi

# 1) Python 环境：venv 优先，缺失自动补装，最终回退系统 pip
PYBIN=$(command -v python3)
if python3 -m venv venv 2>/dev/null; then
  PIP="./venv/bin/pip"; PYRUN="$(pwd)/venv/bin/python"
else
  echo "==> venv 不可用，尝试安装 python3-venv"
  (apt-get update -qq && apt-get install -y python3-venv) >/dev/null 2>&1 \
    || yum install -y python3-virtualenv >/dev/null 2>&1 || true
  if python3 -m venv venv 2>/dev/null; then
    PIP="./venv/bin/pip"; PYRUN="$(pwd)/venv/bin/python"
  else
    echo "[warn] venv 仍不可用，回退系统 pip3"
    PIP="pip3"; PYRUN="$PYBIN"
  fi
fi
echo "==> 使用解释器: $PYRUN"
$PIP install -q -U flask pandas -i https://pypi.tuna.tsinghua.edu.cn/simple
echo "==> 依赖就绪"

# 2) 数据初始化/校验（zip 已带历史数据，拉取失败不阻断）
mkdir -p data reports logs
$PYRUN datafeed.py || echo "[warn] 数据拉取失败，使用包内缓存 CSV"

# 3) systemd 常驻 Web 服务（0.0.0.0:8787，崩溃自动重启）
cat > /etc/systemd/system/quant-web.service <<EOF
[Unit]
Description=Quant Discipline Web (8787)
After=network.target

[Service]
WorkingDirectory=$(pwd)
ExecStart=$PYRUN webapp.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now quant-web
sleep 3
systemctl is-active quant-web && echo "==> quant-web 已启动"

# 4) crontab：每交易日 15:20 主刷新 + 15:50 兜底重跑（幂等；重跑无害，数据幂等）
{
  crontab -l 2>/dev/null | grep -v 'daily_scan.py'
  echo "20 15 * * 1-5 cd $(pwd) && $PYRUN daily_scan.py >> $(pwd)/logs/scan.log 2>&1"
  echo "50 15 * * 1-5 cd $(pwd) && $PYRUN daily_scan.py >> $(pwd)/logs/scan.log 2>&1"
} | crontab -
echo "==> crontab 已注册（周一至周五 15:20 主刷新 + 15:50 兜底）"

# 5) 防火墙放行（云安全组需另行在控制台放行 8787/TCP）
if command -v firewall-cmd >/dev/null; then
  firewall-cmd --permanent --add-port=8787/tcp >/dev/null 2>&1 && firewall-cmd --reload >/dev/null 2>&1 || true
elif command -v ufw >/dev/null; then
  ufw allow 8787/tcp >/dev/null 2>&1 || true
fi

sleep 2
LOCAL_OK=$(curl -s -o /dev/null -w '%{http_code}' -m 8 http://127.0.0.1:8787/ || echo FAIL)
IP=$(curl -s -m 5 ifconfig.me || hostname -I | awk '{print $1}')
echo ""
echo "=============================================="
echo " 本机自检: HTTP $LOCAL_OK (期望 200)"
echo " 访问地址: http://${IP}:8787"
echo " 若外网不通: 云控制台 -> 安全组 -> 放行 8787/TCP"
echo " 运维: systemctl status|restart quant-web ; tail -f logs/scan.log"
echo "=============================================="
