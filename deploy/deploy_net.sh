#!/bin/sh
# 打通网络：Docker 端口转发（socat sidecar）+ nginx HTTPS 反向代理
# 幂等：重复跑不会重复插入。会先备份 compose.yml 与 nginx 站点文件。
#
# 用法：
#   VB_HOST=ubuntu@1.2.3.4 VB_KEY=~/.ssh/your.pem DOMAIN=your.domain sh deploy/deploy_net.sh
#
# 可选环境变量：
#   VB_HOST       SSH 目标（必填）
#   VB_KEY        SSH 私钥（可选）
#   VB_PORT       SSH 端口（默认 22）
#   VB_COMPOSE    服务器上 compose.yml 路径（默认 ~/qqbot/compose.yml）
#   VB_NGINX      服务器上 nginx 站点文件（默认 /etc/nginx/sites-available/sama-chat）
#   DOMAIN        作为反代入口的域名（仅用于提示，实际插到站点文件里）
#   VB_SERVICE    compose 里的服务名（默认 astrbot）
#   VB_CONTAINER  目标容器名（默认 astrbot）
#   BRIDGE_PORT   内嵌服务端口（默认 8899）
#   PATH_PREFIX   对外路径前缀（默认 /pet/）
set -e

VB_HOST="${VB_HOST:?请设置 VB_HOST，例如 VB_HOST=ubuntu@1.2.3.4}"
VB_KEY="${VB_KEY:-}"
VB_PORT="${VB_PORT:-22}"
VB_COMPOSE="${VB_COMPOSE:-~/qqbot/compose.yml}"
VB_NGINX="${VB_NGINX:-/etc/nginx/sites-available/sama-chat}"
VB_SERVICE="${VB_SERVICE:-astrbot}"
VB_CONTAINER="${VB_CONTAINER:-astrbot}"
BRIDGE_PORT="${BRIDGE_PORT:-8899}"
PATH_PREFIX="${PATH_PREFIX:-/pet/}"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15"
[ -n "$VB_KEY" ] && SSH_OPTS="$SSH_OPTS -i $VB_KEY"
[ "$VB_PORT" != "22" ] && SSH_OPTS="$SSH_OPTS -p $VB_PORT"
SSH="ssh $SSH_OPTS"

echo "==> 1/5 备份 compose.yml"
$SSH "$VB_HOST" "sudo cp $VB_COMPOSE $VB_COMPOSE.bak.vb-\$(date +%s)"

echo "==> 2/5 插入端口转发服务（幂等）"
$SSH "$VB_HOST" "sudo BRIDGE_PORT=$BRIDGE_PORT VB_SERVICE=$VB_SERVICE VB_COMPOSE=$VB_COMPOSE python3 -" <<'PYEOF'
import os, sys
p = os.environ["VB_COMPOSE"].replace("~", os.path.expanduser("~"))
s = open(p, encoding="utf-8").read()
if "pet-bridge:" in s:
    print("already patched"); sys.exit(0)
port = os.environ.get("BRIDGE_PORT", "8899")
svc_name = os.environ.get("VB_SERVICE", "astrbot")
svc = f"""  pet-bridge:
    image: alpine/socat
    container_name: pet-bridge
    restart: always
    security_opt:
      - no-new-privileges:true
    command: tcp-listen:{port},fork,reuseaddr tcp:{svc_name}:{port}
    ports:
      - "127.0.0.1:{port}:{port}"
    networks:
      - astrbot_network
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

"""
idx = s.rindex("\nnetworks:")
open(p, "w", encoding="utf-8").write(s[:idx+1] + svc + s[idx+1:])
print("patched")
PYEOF

echo "==> 3/5 启动端口转发"
$SSH "$VB_HOST" "cd \$(dirname $VB_COMPOSE) && sudo docker compose config >/dev/null && sudo docker compose up -d pet-bridge && sleep 2 && sudo docker ps --format '{{.Names}}|{{.Status}}|{{.Ports}}' | grep -E 'pet-bridge|$VB_CONTAINER'"

echo "==> 4/5 写 nginx 片段"
$SSH "$VB_HOST" "cat > /tmp/pet-location.conf <<'NGEOF'
    location $PATH_PREFIX {
        proxy_pass http://127.0.0.1:$BRIDGE_PORT/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \"upgrade\";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
NGEOF
echo written"

echo "==> 5/5 插入站点文件（幂等）并 reload"
$SSH "$VB_HOST" "sudo PATH_PREFIX=$PATH_PREFIX VB_NGINX=$VB_NGINX python3 -" <<'PYEOF'
import os, shutil
p = os.environ["VB_NGINX"]
prefix = os.environ.get("PATH_PREFIX", "/pet/")
s = open(p, encoding="utf-8").read()
if f"location {prefix}" in s:
    print("already patched")
else:
    frag = open("/tmp/pet-location.conf", encoding="utf-8").read()
    i = s.index("    location / {")
    shutil.copy(p, p + ".bak.vb")
    open(p, "w", encoding="utf-8").write(s[:i] + frag + "\n" + s[i:])
    print("patched")
PYEOF
$SSH "$VB_HOST" "sudo nginx -t && sudo systemctl reload nginx && echo NGINX-RELOADED"

echo "✅ 完成。手机访问：https://${DOMAIN:-<你的域名>}$PATH_PREFIX"
