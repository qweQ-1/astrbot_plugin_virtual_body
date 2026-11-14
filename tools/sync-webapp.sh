#!/bin/sh
# 把 iOS 应用仓库里的前端同步到本插件（插件要托管这份前端给 PWA 用）
#
# 用法：
#   sh tools/sync-webapp.sh                     # 默认找 ../virtual-body-ios
#   sh tools/sync-webapp.sh /path/to/app-repo    # 指定应用仓库路径
set -e

HERE="$(cd "$(dirname "$0")/.." && pwd)"
APPDIR="${1:-$HERE/../virtual-body-ios}"
SRC="$APPDIR/Resources/WebApp"

if [ ! -f "$SRC/index.html" ]; then
  echo "找不到前端：$SRC/index.html" >&2
  echo "请传应用仓库路径：sh tools/sync-webapp.sh /path/to/virtual-body-ios" >&2
  exit 1
fi

cd "$HERE"
if command -v rsync >/dev/null 2>&1; then
  # sw.js 只属于插件（PWA 缓存），应用仓库不带它，所以排除掉不让它被删
  rsync -a --delete --exclude 'sw.js' "$SRC/" webapp/
else
  for f in index.html app.js creature.js style.css icon.svg manifest.webmanifest; do
    cp "$SRC/$f" webapp/
  done
fi
echo "✅ 前端已同步：$SRC → $HERE/webapp"
ls -la webapp
