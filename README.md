# 虚拟身体 · AstrBot 插件（astrbot_plugin_virtual_body）

把 AstrBot 变成一个**有身体的虚拟生命**：它住在手机 App / 网页里，可以被手指触摸，
并且「虚拟」与「现实」双向互通 —— **在 App 里摸它会播报到 QQ，你在 QQ 里说的话会从它嘴里说出来**。

> 配套客户端（iOS 原生 ipa / PWA）在另一个仓库：**[qweQ-1/virtual-body-ios](https://github.com/qweQ-1/virtual-body-ios)**。
> 本插件内嵌 HTTP + WebSocket 服务，同时托管那份网页前端（`webapp/`），所以只装插件也能用 PWA 玩。

---

## 一、它到底做了什么

| 方向 | 发生什么 |
|---|---|
| **虚 → 现** | 在 App 里摸头 / 抱抱 / 挠痒 / 投喂 / 抛起来 → 角色生成表情、动作、台词（可选交给大模型）→ 同一时间 QQ 收到一条播报：「📱 阿蓝 在 App 里摸了摸你的头顶。小虚：「唔……好舒服。」」 |
| **现 → 虚** | 你在 QQ 里说话 → 角色在 App 里"听见"（气泡提示）→ AstrBot 的回复会被**同一个角色在 App 里说出口**（带口型和动作） |
| **App 内对话** | App 里打字 → 角色回应 → 同时镜像到 QQ，让你在现实里也看到这段对话 |
| **QQ 操控一切** | 说话、动作、表情、换装、场景、睡觉、散步、改名、静音、解绑、重置……全都能在 QQ 里用文字指挥 |

角色还带一整套养成数值（亲密度 / 心情 / 精力 / 饱食 / 清洁 / 信任 / 等级经验），
**离线也会缓慢衰减**（饿到一定程度会自己变"饿"的表情）。

---

## 二、安装

```bash
# 1. 放到 AstrBot 的插件目录（容器内是 /AstrBot/data/plugins）
cp -r astrbot_plugin_virtual_body <AstrBot>/data/plugins/

# 2. 重启 AstrBot（或在 WebUI「插件管理」里重载）
#    日志里看到这行就成了：
#    [virtual-body] 虚现互通已就绪：0.0.0.0:8899
```

依赖：**无额外依赖**（用的是 AstrBot 自带的 aiohttp）。`requirements.txt` 是空的占位。

---

## 三、网络：让手机能连进来

插件在容器内监听 `8899`，需要把它暴露出来。两种常见做法：

### A. Docker 端口转发（最省事，加到 compose.yml）

```yaml
  pet-bridge:
    image: alpine/socat
    container_name: pet-bridge
    restart: always
    command: tcp-listen:8899,fork,reuseaddr tcp:astrbot:8899
    ports:
      - "127.0.0.1:8899:8899"     # 只给本机 nginx 用，不直接暴露公网
    networks:
      - astrbot_network
```

也可以直接在 `astrbot` 服务上加 `- "8899:8899"`（改完需要重建容器）。

### B. 反向代理 + HTTPS（强烈建议，iOS 需要）

```nginx
location /pet/ {
    proxy_pass http://127.0.0.1:8899/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;     # WebSocket 必需
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;                        # 事件流不要缓冲
    proxy_read_timeout 3600s;
}
```

装好之后手机就能打开 `https://你的域名/pet/`（PWA 入口），App 里也填这个地址。

`deploy/` 下有两个脚本可以把上面两步在服务器上自动做完（**幂等**，会先备份配置）：

```bash
VB_HOST=ubuntu@1.2.3.4 VB_KEY=~/.ssh/your.pem sh deploy/deploy_plugin.sh   # 传插件
VB_HOST=ubuntu@1.2.3.4 VB_KEY=~/.ssh/your.pem DOMAIN=your.domain \
  sh deploy/deploy_net.sh                                                   # 端口转发 + nginx
```

---

## 四、QQ 指令表

```
【绑定】  虚现 绑定码              生成 6 位绑定码（10 分钟有效，一次一码）
【看它】  虚现 状态                状态卡（等级/心情/亲密/饱食/精力…）
【摸它】  虚现 摸头 / 抱抱 / 戳戳 / 挠痒 / 亲亲 / 拍拍 / 抛 / 牵手
          虚现 喂 <食物>            投喂（例：虚现 喂 草莓）
          虚现 说 <内容>            让它在 App 里说出这句话
          虚现 做 <动作>            跳 / 挥手 / 转圈 / 跳舞 / 坐下 / 打滚 / 思考 / 慌张
          虚现 表情 <情绪>          happy/calm/shy/sad/angry/sleepy/hungry/excited/love/bored
【安排它】虚现 睡觉 / 起床 / 玩 / 散步 / 跳舞 / 打滚 / 来
          虚现 名字 <新名字>        改名
          虚现 换装 <装扮>          default / pajamas / sport / raincoat / hamster
          虚现 场景 <场景>          day / night / rain / party / beach
【设置】  虚现 静音 / 开声          App 端语音开关
          虚现 在线                 看已绑定的设备
          虚现 重启服务             端口被占用/服务挂了时重启内嵌服务
          虚现 解绑                 作废所有 App token
          虚现 重置 确认            角色回到出厂状态
          虚现 帮助                 全部指令
```

**权限模型**：第一个发送 `虚现 绑定码` 的 QQ 号成为**房主**；房主、管理员、配置里的白名单可以操控角色，
其他人只能查看状态。房主的**普通聊天消息**（不带指令前缀）会被镜像到 App，让角色"听见"。

---

## 五、配置项（WebUI → 插件配置）

| 键 | 默认 | 说明 |
|---|---|---|
| `port` / `host` | `8899` / `0.0.0.0` | 内嵌服务监听地址 |
| `public_base` | `https://你的域名/pet` | 只在 QQ 里展示给用户的地址 |
| `persona` | 黏人、嘴硬心软… | 角色性格，写进每次生成反应的系统提示词 |
| `token_required` | `true` | 是否强制绑定校验（关掉仅限本地调试） |
| `code_ttl` | `600` | 绑定码有效期（秒） |
| `touch_to_qq` | `true` | App 触摸是否播报到 QQ（互通的一半） |
| `touch_qq_interval` | `30` | 触摸播报的最小间隔（秒），防止狂摸刷屏 |
| `mirror_app_to_qq` | `true` | App 里的对话是否镜像到 QQ |
| `force_reply_private` | `true` | 房主私聊随便说话都让 AstrBot 正常回话 |
| `touch_llm_cooldown` | `6` | 同类触摸调用大模型的冷却（秒），冷却期内用规则台词 |
| `llm_timeout` | `12` | 大模型超时，超时自动退回规则台词 |
| `whitelist` | `[]` | 额外可以操控角色的 QQ 号 |

---

## 六、接口协议

插件内嵌服务的完整契约见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)：

- `GET /api/hello`、`POST /api/bind`（绑定码换 token）
- `GET /api/state`、`GET /api/events?since=`（断线降级轮询）
- `POST /api/touch`（核心：触摸）、`POST /api/chat`、`POST /api/action`
- `WS /ws?token=`（实时事件流：`say` / `anim` / `effect` / `qq` / `state` / `notice` / `levelup`）

自己写客户端的话，只需要实现 `hello` → `bind` → `ws` + `touch` 这四步就能跑通。

---

## 七、自检

不需要 AstrBot 也能跑（验证状态机、反应引擎、事件流、协议字段）：

```bash
python3 tests/selftest.py     # 66 项，全部应通过
```

---

## 八、常见问题

- **手机连不上 / 一直"重连中"**：先确认 `8899` 真的转发出来了（`curl http://127.0.0.1:8899/api/hello`），
  再确认反代带了 `Upgrade` / `Connection` 头（WebSocket 握手失败只影响实时推送，HTTP 仍然可用，会降级成轮询）。
- **App 提示绑定失效**：房主执行过 `虚现 解绑`，或者角色被 `虚现 重置 确认` 过。重新 `虚现 绑定码` 即可。
- **角色反应很单调**：多半是大模型不可用（超时/未配置），此时自动退回内置规则台词库，功能不受影响。
- **不想让它播报到 QQ**：关掉 `touch_to_qq`，或把 `touch_qq_interval` 调大。
- **端口冲突**：改配置里的 `port`，同时改端口转发，然后 `虚现 重启服务`。

## 许可证

MIT
