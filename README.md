# PROTO CONFIG
4-byte magic number
MAGIC_BYTES = b'\xFF\xC0\xFF\xEE'
MAX_PAYLOAD = 150  # bytes

Header: type(1), seq(1), nonce(2), len(2), checksum(4),

MAGIC HEADER PAYLOAD
  4  +  10  +   N
HEADER_FMT = '>BBHHI'
HEADER_SIZE = struct.calcsize(HEADER_FMT)
CHKSUM_SIZE = 4
WINDOWS_SIZE = 1


## type

0: DATA
1: ACK



# metaphorical-communication

入口脚本是 `on_progress.py`，它通过串口与外部设备通信，并提供：

- 交互式 shell（本机直接输入 `message` / `sendfile` 等命令）
- 可选的内网 HTTP API（让内网其它电脑通过“client”调用本机，复用同一套交互逻辑）

## 设备端（运行 `on_progress.py`）

### 1) 仅本机交互（原有模式）

```bash
python on_progress.py -P COM3 -N Alice
```

进入后可用：

- `message <text>`
- `sendfile <local_path> <receiver_name>`

### 2) 开启内网 API（推荐 headless）

```bash
python on_progress.py -P COM3 -N Alice ^
  --api-enable --api-host 0.0.0.0 --api-port 8000 --api-key YOUR_KEY ^
  --no-shell
```

说明：

- **绑定非本机地址时强制要求 `--api-key`**（请求需带 `X-API-Key`）
- 建议配合系统防火墙只允许内网访问该端口

## 内网客户端（运行 `lan_client.py`）

### 聊天室 Shell（推荐）

在内网机器上运行（会在连接时提交用户名）：

```bash
python lan_client.py --server 192.168.1.10:8000 --api-key YOUR_KEY chat --name Bob
```

可用命令：

- `/users`：查看当前在线用户
- `/discover`：触发一次 discover（走原有任务逻辑）
- `/sendfile <path> <receiver>`：发送文件（走原有任务逻辑）
- `/exit`：退出聊天室

### 健康检查 / 状态

```bash
python lan_client.py --server 192.168.1.10:8000 --api-key YOUR_KEY health
python lan_client.py --server 192.168.1.10:8000 --api-key YOUR_KEY status
```

### 发送消息（异步任务）

```bash
python lan_client.py --server 192.168.1.10:8000 --api-key YOUR_KEY message "hello from LAN" --wait
```

服务端会返回 `task_id`；加上 `--wait` 会自动轮询直到任务完成/失败。

### 发现用户

```bash
python lan_client.py --server 192.168.1.10:8000 --api-key YOUR_KEY discover --wait
python lan_client.py --server 192.168.1.10:8000 --api-key YOUR_KEY users
```

### 查询事件 / 任务状态

```bash
python lan_client.py --server 192.168.1.10:8000 --api-key YOUR_KEY events --after 0
python lan_client.py --server 192.168.1.10:8000 --api-key YOUR_KEY task <task_id>
```

### 发送文件（上传到设备端再走串口发送）

```bash
python lan_client.py --server 192.168.1.10:8000 --api-key YOUR_KEY sendfile --to Bob --file .\demo.bin --wait
```

> 注意：仓库里的文件传输逻辑仍在演进中；API 侧是“复用现有 sendfile 逻辑”，如果你当前的串口端文件协议未完成，可能需要继续完善 `run_file_send/run_file_receive`。

## HTTP API 一览

- `GET /api/v1/health`
- `GET /api/v1/status`
- `GET /api/v1/users`
- `GET /api/v1/events?after=<id>&wait=1&timeout=30`（支持长轮询）
- `GET /api/v1/events/stream?after=<id>`（SSE 推送：服务端广播新消息给各个 client）
- `GET /api/v1/tasks/<task_id>`
- `POST /api/v1/tasks/message` `{ "text": "..." }`
- `POST /api/v1/tasks/discover` `{}`
- `POST /api/v1/tasks/sendfile` `{ "receiver": "...", "filename": "...", "data_b64": "..." }`
- `POST /api/v1/chat/join` `{ "username": "..." }`
- `POST /api/v1/chat/leave` `{ "session_id": "..." }`
- `POST /api/v1/chat/message` `{ "session_id": "...", "text": "..." }`
- `GET /api/v1/chat/clients`

