#!/bin/sh
# 把插件部署到 AstrBot 服务器（可跨主机复用）
#
# 用法：
#   VB_HOST=ubuntu@1.2.3.4 VB_KEY=~/.ssh/your.pem sh deploy/deploy_plugin.sh
#
# 可选环境变量：
#   VB_HOST       SSH 目标（必填，形如 user@host）
#   VB_KEY        SSH 私钥路径（可选，默认用 ssh-agent / 默认密钥）
#   VB_PORT       SSH 端口（默认 22）
#   VB_PLUGINS    服务器上 AstrBot 插件目录（默认 ~/qqbot/data/plugins）
#   VB_CONTAINER  AstrBot 容器名（默认 astrbot，用于容器内校验）
#
# 说明：插件目录通常由容器以 root 写入，所以脚本用 sudo 安装。
set -e

NAME="astrbot_plugin_virtual_body"
VB_HOST="${VB_HOST:?请设置 VB_HOST，例如 VB_HOST=ubuntu@1.2.3.4}"
VB_KEY="${VB_KEY:-}"
VB_PORT="${VB_PORT:-22}"
VB_PLUGINS="${VB_PLUGINS:-~/qqbot/data/plugins}"
VB_CONTAINER="${VB_CONTAINER:-astrbot}"

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15"
[ -n "$VB_KEY" ] && SSH_OPTS="$SSH_OPTS -i $VB_KEY"
[ "$VB_PORT" != "22" ] && SSH_OPTS="$SSH_OPTS -p $VB_PORT"
SSH="ssh $SSH_OPTS"

echo "==> 1/4 打包插件"
rm -rf "$HERE/__pycache__"
TMPTAR="/tmp/$NAME.tar.gz"
tar czf "$TMPTAR" -C "$HERE/.." "$NAME"

echo "==> 2/4 上传"
$SSH "$VB_HOST" "rm -rf /tmp/vb-deploy && mkdir -p /tmp/vb-deploy"
$SSH "$VB_HOST" "cat > /tmp/vb-deploy/$NAME.tar.gz" < "$TMPTAR"
$SSH "$VB_HOST" "tar xzf /tmp/vb-deploy/$NAME.tar.gz -C /tmp/vb-deploy"

echo "==> 3/4 安装到插件目录（sudo）"
$SSH "$VB_HOST" "sudo rm -rf $VB_PLUGINS/$NAME && sudo cp -r /tmp/vb-deploy/$NAME $VB_PLUGINS/ && sudo chown -R root:root $VB_PLUGINS/$NAME && sudo ls $VB_PLUGINS/$NAME"

echo "==> 4/4 容器内校验"
$SSH "$VB_HOST" "sudo docker exec $VB_CONTAINER sh -c 'cd /AstrBot/data/plugins/$NAME && python -m py_compile *.py && echo PY-COMPILE-OK && ls webapp | head'"

echo "✅ 部署完成。接下来："
echo "   1) 重启 AstrBot 或在 WebUI 里重载插件"
echo "   2) 执行 deploy/deploy_net.sh 打通端口与 nginx（如果还没做）"
echo "   3) 在 QQ 里发送「虚现 绑定码」开始绑定"
