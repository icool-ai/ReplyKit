#!/usr/bin/env bash
set -euo pipefail

# Apache Tika Server 一键安装 & 启动脚本（WSL2 Ubuntu 22.04 / 24.04）
# 与现有 wsl_setup_redis.sh 配套：脚本跑完把打印出的 TIKA_URL 复制到 Windows 侧 .env
#
# 依赖：
#   * openjdk-17-jre-headless（无 GUI 的 JRE，WSL2 上约 200MB）
#   * tika-server-standard-2.x.x.jar（约 80MB，首次从 Apache 官方镜像下载）
#
# 运行：
#   chmod +x scripts/wsl_setup_tika.sh && ./scripts/wsl_setup_tika.sh

TIKA_VERSION="${TIKA_VERSION:-2.9.1}"
TIKA_PORT="${TIKA_PORT:-9998}"
TIKA_DIR="$HOME/.tika"
TIKA_JAR_NAME="tika-server-standard-${TIKA_VERSION}.jar"
TIKA_JAR_PATH="${TIKA_DIR}/${TIKA_JAR_NAME}"
TIKA_DOWNLOAD_URL="https://archive.apache.org/dist/tika/${TIKA_VERSION}/${TIKA_JAR_NAME}"
TIKA_PID_FILE="${TIKA_DIR}/tika-server.pid"
TIKA_LOG_FILE="${TIKA_DIR}/tika-server.log"

echo "=== (1) 基础信息 ==="
WSL_IP="$(hostname -I | awk '{print $1}')"
echo "WSL_IP=$WSL_IP"
echo "TIKA_VERSION=$TIKA_VERSION"
echo "TIKA_PORT=$TIKA_PORT"
echo "TIKA_JAR_PATH=$TIKA_JAR_PATH"

echo ""
echo "=== (2) 安装 OpenJDK 17（仅首次） ==="
if command -v java >/dev/null 2>&1; then
    echo "检测到已安装 Java：$(java -version 2>&1 | head -n1)"
else
    export DEBIAN_FRONTEND=noninteractive
    sudo apt-get update -y
    sudo apt-get install -y --no-install-recommends openjdk-17-jre-headless ca-certificates
    echo "安装完成：$(java -version 2>&1 | head -n1)"
fi

echo ""
echo "=== (3) 下载 Apache Tika Server JAR（仅首次） ==="
mkdir -p "$TIKA_DIR"
if [ -f "$TIKA_JAR_PATH" ]; then
    echo "JAR 已存在：$TIKA_JAR_PATH"
else
    echo "从 $TIKA_DOWNLOAD_URL 下载..."
    if command -v curl >/dev/null 2>&1; then
        curl -fSL --retry 3 --retry-delay 2 "$TIKA_DOWNLOAD_URL" -o "$TIKA_JAR_PATH.tmp"
    else
        sudo apt-get install -y --no-install-recommends curl ca-certificates
        curl -fSL --retry 3 --retry-delay 2 "$TIKA_DOWNLOAD_URL" -o "$TIKA_JAR_PATH.tmp"
    fi
    mv "$TIKA_JAR_PATH.tmp" "$TIKA_JAR_PATH"
    echo "下载完成：$(du -h "$TIKA_JAR_PATH" | cut -f1)"
fi

echo ""
echo "=== (4) 停止已有 Tika Server（若有） ==="
if [ -f "$TIKA_PID_FILE" ]; then
    OLD_PID="$(cat "$TIKA_PID_FILE" 2>/dev/null || true)"
    if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
        echo "已停止旧进程 PID=$OLD_PID"
    fi
    rm -f "$TIKA_PID_FILE"
fi

echo ""
echo "=== (5) 后台启动 Tika Server（绑定 0.0.0.0，允许 Windows 侧访问） ==="
# -Xmx512m 给文档解析足够的堆；大 PDF/Office 文件可按需加到 1g/2g
nohup java \
    -Xmx512m \
    -jar "$TIKA_JAR_PATH" \
    --host 0.0.0.0 \
    --port "$TIKA_PORT" \
    > "$TIKA_LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$TIKA_PID_FILE"
disown || true
sleep 3

echo ""
echo "=== (6) 本机 127.0.0.1 健康检查 ==="
for i in $(seq 1 20); do
    HTTP_CODE="$(curl -s -o /dev/null -w "%{http_code}" \
        --connect-timeout 2 --max-time 5 \
        "http://127.0.0.1:${TIKA_PORT}/tika" || true)"
    if [ "$HTTP_CODE" = "200" ]; then
        echo "OK（HTTP $HTTP_CODE），启动用时 ${i}s"
        break
    fi
    if [ "$i" -eq 20 ]; then
        echo "启动超时。查看日志：tail -n 50 $TIKA_LOG_FILE"
        exit 1
    fi
    sleep 1
done

echo ""
echo "=== (7) WSL IP 侧健康检查（$WSL_IP:$TIKA_PORT） ==="
HTTP_CODE="$(curl -s -o /dev/null -w "%{http_code}" \
    --connect-timeout 3 --max-time 8 \
    "http://${WSL_IP}:${TIKA_PORT}/tika" || true)"
echo "结果 HTTP $HTTP_CODE"

echo ""
echo "=== DONE  ==="
echo ""
echo "Windows 侧 .env 中写入："
echo "  TIKA_ENABLED=true"
echo "  TIKA_URL=http://${WSL_IP}:${TIKA_PORT}/tika"
echo "  TIKA_TIMEOUT_SEC=60"
echo ""
echo "常用管理命令（WSL 内执行）："
echo "  查看日志： tail -f $TIKA_LOG_FILE"
echo "  停止：     kill \$(cat $TIKA_PID_FILE) && rm $TIKA_PID_FILE"
echo "  后台重启： bash scripts/wsl_setup_tika.sh"
echo ""
echo "export WSL_TIKA_URL=http://${WSL_IP}:${TIKA_PORT}/tika"
