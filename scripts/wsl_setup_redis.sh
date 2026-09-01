#!/usr/bin/env bash
set -euo pipefail

echo "=== (1) WSL 内网 IP ==="
WSL_IP="$(hostname -I | awk '{print $1}')"
echo "WSL_IP=$WSL_IP"

echo ""
echo "=== (2) 修改 /etc/redis/redis.conf 放通 Windows 访问 ==="
CONF="/etc/redis/redis.conf"
if [ ! -f "$CONF.bak" ]; then
    cp "$CONF" "$CONF.bak"
    echo "已备份 $CONF -> $CONF.bak"
fi

sed -i.bak \
    -e 's|^bind 127\.0\.0\.1 -::1$|bind 0.0.0.0 -::1|' \
    -e 's|^protected-mode yes$|protected-mode no|' \
    "$CONF"

echo "修改后的关键配置："
grep -E '^(bind|protected-mode) ' "$CONF" || true

echo ""
echo "=== (3) 重启 Redis 服务 ==="
if command -v systemctl >/dev/null 2>&1 && systemctl list-units --type=service 2>/dev/null | grep -q redis-server; then
    systemctl restart redis-server
    systemctl is-active redis-server
else
    service redis-server restart || true
    sleep 1
fi
sleep 1

echo ""
echo "=== (4) WSL 本机侧验证（127.0.0.1:6379） ==="
redis-cli -h 127.0.0.1 -p 6379 ping

echo ""
echo "=== (5) WSL IP 侧验证（$WSL_IP:6379） ==="
redis-cli -h "$WSL_IP" -p 6379 ping

echo ""
echo "=== DONE。Windows 侧把 REDIS_URL 设成：redis://$WSL_IP:6379/0 即可。 ==="
echo "export WSL_REDIS_IP=$WSL_IP"
