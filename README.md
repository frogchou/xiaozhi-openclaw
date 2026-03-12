# xiaozhi-server + myapp-channel

这个仓库包含两个配套项目，用于把「设备侧的语音对话」接入 OpenClaw，并通过一个 WebSocket Bridge 让 OpenClaw 负责大模型回复生成。

- [xiaozhi-server](./xiaozhi-server)：面向设备/客户端的 WebSocket 语音对话服务端，同时提供 OpenClaw Bridge 入口。
- [myapp-channel](./myapp-channel-0.1.0/myapp-channel)：OpenClaw 自定义 Channel 插件（出站 WebSocket），连接到 `xiaozhi-server` 的 Bridge。

## 架构概览

```text
Device/Client  --(WS, Opus frames)-->  xiaozhi-server  --(WS bridge)-->  OpenClaw + myapp-channel
       ^                 |                   ^                           |
       |                 |                   |                           |
       +--(WS, Opus)-----+      reply.response(reply text)               +--(agent routing / tools)
```

在一次对话回合中：

1. 设备通过 WebSocket 发送 Opus 音频帧到 `xiaozhi-server`
2. `xiaozhi-server` 用 Whisper 做 ASR（语音转文字）
3. `xiaozhi-server` 通过 Bridge 发 `reply.request` 给 OpenClaw（由 `myapp-channel` 接收并触发 reply 生成）
4. OpenClaw 返回 `reply.response`
5. `xiaozhi-server` 用 OpenAI TTS 合成语音并回推给设备（Opus 包）

## 项目一：xiaozhi-server

**定位**
- WebSocket 服务端：接收设备音频、返回 TTS 音频
- Bridge 服务端：提供 `/openclaw-bridge`，给 OpenClaw 插件长连接接入

**主要能力**
- 多协议版本的二进制音频包解析（见 [protocol.py](./xiaozhi-server/core/protocol.py)）
- 一轮对话的完整链路：ASR → LLM(通过 OpenClaw Bridge) → TTS（见 [session.py](./xiaozhi-server/core/session.py)）
- Bridge token 鉴权、request/response 关联与超时（见 [openclaw_bridge.py](./xiaozhi-server/services/openclaw_bridge.py)）
- HTTP 健康检查：`/` 或 `/healthz`（非 WebSocket 升级请求时返回文本）（见 [server.py](./xiaozhi-server/core/server.py)）

**依赖**
- Python 3.9+
- 系统安装 `ffmpeg`（用于 Opus/Wav/Ogg 转换）

**运行**

Windows（PowerShell）：

```bash
cd xiaozhi-server
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python main.py
```

macOS / Linux：

```bash
cd xiaozhi-server
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python main.py
```

`.env` 关键配置（示例见 [.env.example](./xiaozhi-server/.env.example)）：

- `OPENAI_API_KEY`：用于 ASR/TTS
- `OPENCLAW_BRIDGE_PATH`：默认 `/openclaw-bridge`
- `OPENCLAW_BRIDGE_TOKEN`：Bridge 共享 token（需要与 OpenClaw 插件一致）
- `OPENCLAW_BRIDGE_REQUEST_TIMEOUT_SECONDS`：等待 OpenClaw 返回的超时

## 项目二：myapp-channel（OpenClaw 插件）

**定位**
- OpenClaw 自定义 Channel 插件，通过出站 WebSocket 长连接接入 `xiaozhi-server` 的 Bridge
- 接收 `reply.request`，调用 OpenClaw 的 reply 生成并回传 `reply.response`

**特性**
- 出站连接 + 心跳 `bridge.ping/pong` + 空闲超时重连（见 [index.ts](./myapp-channel-0.1.0/myapp-channel/index.ts)）
- `reply.request` / `reply.response` 协议（请求关联、错误返回）
- 基于 `bridgeToken` 的轻量共享密钥鉴权（token 作为 query 参数 `?token=...`）

**安装与配置**
- 插件目录：`myapp-channel-0.1.0/myapp-channel`
- 安装说明：见 [INSTALL.md](./myapp-channel-0.1.0/INSTALL.md)
- 协议与示例：见 [myapp-channel README](./myapp-channel-0.1.0/myapp-channel/README.md)

OpenClaw 配置示例：

```json
{
  "channels": {
    "myapp-channel": {
      "enabled": true,
      "bridgeUrl": "ws://YOUR_BRIDGE_HOST:8080/openclaw-bridge",
      "bridgeToken": "replace-with-random-token",
      "botName": "OpenClaw",
      "timeoutMs": 120000,
      "reconnectMs": 3000
    }
  }
}
```

## Bridge 协议（简版）

请求（`xiaozhi-server` → OpenClaw）：

```json
{
  "type": "reply.request",
  "requestId": "uuid",
  "text": "你好",
  "from": "device-id",
  "senderName": "client-id",
  "conversationId": "session-id"
}
```

响应（OpenClaw → `xiaozhi-server`）：

```json
{
  "type": "reply.response",
  "requestId": "uuid",
  "ok": true,
  "sessionKey": "myapp-channel:device-id:session-id",
  "from": "device-id",
  "conversationId": "session-id",
  "reply": "..."
}
```

## 本地联调建议

1. 启动 `xiaozhi-server`，确保端口与 `/openclaw-bridge` 可用
2. 在 OpenClaw 中安装并启用 `myapp-channel`，配置 `bridgeUrl` 指向 `xiaozhi-server`
3. 确保两侧 `bridgeToken` 一致
4. 观察日志：`xiaozhi-server` 出现 `OpenClaw bridge connected`，OpenClaw 侧出现 `bridge connected`
