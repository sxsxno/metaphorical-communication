import argparse
import base64
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _normalize_server(server):
    server = (server or "").strip()
    if not server:
        raise ValueError("server is required")
    if server.startswith("http://") or server.startswith("https://"):
        return server.rstrip("/")
    return ("http://" + server).rstrip("/")


def _request_json(base_url, method, path, api_key, body=None, extra_headers=None, timeout=15):
    url = base_url + path
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    if extra_headers:
        headers.update(extra_headers)

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=float(timeout)) as resp:
            raw = resp.read()
            if not raw:
                return {"ok": True}
            return json.loads(raw.decode("utf-8"))
    except HTTPError as e:
        raw = e.read()
        try:
            body_obj = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body_obj = raw.decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTP {e.code}", "body": body_obj}
    except URLError as e:
        return {"ok": False, "error": str(e)}


def _wait_task(base_url, task_id, api_key, interval, timeout):
    deadline = time.time() + timeout
    while True:
        res = _request_json(base_url, "GET", f"/api/v1/tasks/{task_id}", api_key)
        if not res.get("ok"):
            return res
        task = res.get("task") or {}
        if task.get("status") in ("completed", "failed"):
            return res
        if time.time() >= deadline:
            return {"ok": False, "error": "timeout waiting task", "last": res}
        time.sleep(interval)


def _format_event(evt, self_name):
    et = evt.get("type")
    data = evt.get("data") or {}
    if et == "chat.message":
        sender = data.get("from") or "?"
        text = data.get("text") or ""
        if sender == self_name:
            return f"{text}"
        return f"{text}"
    if et == "chat.join":
        u = data.get("username") or "?"
        return f"* {u} joined"
    if et == "chat.leave":
        u = data.get("username") or "?"
        return f"* {u} left"
    if et == "message.recv":
        text = data.get("text")
        if text:
            return f"{text}"
        hx = data.get("payload_hex") or ""
        return f"hex={hx}"
    return None


def main(argv):
    parser = argparse.ArgumentParser(description="LAN client for on_progress.py HTTP API")
    parser.add_argument("--server", required=True, help="e.g. 192.168.1.10:8000 or http://192.168.1.10:8000")
    parser.add_argument("--api-key", default=None, help="API key (X-API-Key)")

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="GET /api/v1/health")
    sub.add_parser("status", help="GET /api/v1/status")
    sub.add_parser("users", help="GET /api/v1/users")

    p_events = sub.add_parser("events", help="GET /api/v1/events?after=<id>")
    p_events.add_argument("--after", type=int, default=0)

    p_task = sub.add_parser("task", help="GET /api/v1/tasks/<task_id>")
    p_task.add_argument("task_id")

    p_msg = sub.add_parser("message", help="POST /api/v1/tasks/message")
    p_msg.add_argument("text")
    p_msg.add_argument("--wait", action="store_true")
    p_msg.add_argument("--timeout", type=float, default=30.0)
    p_msg.add_argument("--interval", type=float, default=0.5)

    p_disc = sub.add_parser("discover", help="POST /api/v1/tasks/discover")
    p_disc.add_argument("--wait", action="store_true")
    p_disc.add_argument("--timeout", type=float, default=10.0)
    p_disc.add_argument("--interval", type=float, default=0.5)

    p_sf = sub.add_parser("sendfile", help="POST /api/v1/tasks/sendfile")
    p_sf.add_argument("--to", required=True, dest="receiver")
    p_sf.add_argument("--file", required=True, dest="file_path")
    p_sf.add_argument("--name", default=None, dest="filename", help="remote filename (default: basename of --file)")
    p_sf.add_argument("--wait", action="store_true")
    p_sf.add_argument("--timeout", type=float, default=300.0)
    p_sf.add_argument("--interval", type=float, default=1.0)

    p_chat = sub.add_parser("chat", help="Join chat shell via API (provide username)")
    p_chat.add_argument("--name", required=True, help="your username in the LAN chatroom")
    p_chat.add_argument("--history", action="store_true", help="print existing events before joining")
    p_chat.add_argument("--poll-timeout", type=float, default=30.0, help="long-poll timeout seconds (0-60)")

    args = parser.parse_args(argv)
    base_url = _normalize_server(args.server)
    api_key = args.api_key

    if args.cmd == "health":
        res = _request_json(base_url, "GET", "/api/v1/health", api_key)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "status":
        res = _request_json(base_url, "GET", "/api/v1/status", api_key)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "users":
        res = _request_json(base_url, "GET", "/api/v1/users", api_key)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "events":
        res = _request_json(base_url, "GET", f"/api/v1/events?after={int(args.after)}", api_key)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "task":
        res = _request_json(base_url, "GET", f"/api/v1/tasks/{args.task_id}", api_key)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "message":
        res = _request_json(base_url, "POST", "/api/v1/tasks/message", api_key, {"text": args.text})
        if args.wait and res.get("ok") and res.get("task_id"):
            res = _wait_task(base_url, res["task_id"], api_key, args.interval, args.timeout)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "discover":
        res = _request_json(base_url, "POST", "/api/v1/tasks/discover", api_key, {})
        if args.wait and res.get("ok") and res.get("task_id"):
            res = _wait_task(base_url, res["task_id"], api_key, args.interval, args.timeout)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "sendfile":
        file_path = args.file_path
        if not os.path.isfile(file_path):
            print(json.dumps({"ok": False, "error": f"file not found: {file_path}"}, ensure_ascii=False, indent=2))
            return 2
        filename = args.filename or os.path.basename(file_path) or "upload.bin"
        with open(file_path, "rb") as f:
            raw = f.read()
        data_b64 = base64.b64encode(raw).decode("ascii")
        res = _request_json(
            base_url,
            "POST",
            "/api/v1/tasks/sendfile",
            api_key,
            {"receiver": args.receiver, "filename": filename, "data_b64": data_b64},
        )
        if args.wait and res.get("ok") and res.get("task_id"):
            res = _wait_task(base_url, res["task_id"], api_key, args.interval, args.timeout)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "chat":
        # join
        join = _request_json(base_url, "POST", "/api/v1/chat/join", api_key, {"username": args.name})
        if not join.get("ok"):
            print(json.dumps(join, ensure_ascii=False, indent=2))
            return 1
        session_id = join.get("session_id")
        self_name = join.get("username") or args.name
        print(f"joined chat as {self_name}")

        # init last event id
        last_id = 0
        init = _request_json(
            base_url,
            "GET",
            "/api/v1/events?after=0",
            api_key,
            extra_headers={"X-Session-Id": session_id},
            timeout=30,
        )
        if init.get("ok"):
            events = init.get("events") or []
            if args.history:
                for e in events:
                    msg = _format_event(e, self_name)
                    if msg:
                        print(msg)
            if events:
                last_id = int(events[-1].get("id") or 0)

        stop = False

        # SSE stream loop in background thread (server will broadcast events immediately).
        def _stream():
            nonlocal last_id, stop
            while not stop:
                stream_url = f"{base_url}/api/v1/events/stream?after={last_id}"
                headers = {
                    "Accept": "text/event-stream",
                    "X-Session-Id": session_id,
                }
                if api_key:
                    headers["X-API-Key"] = api_key

                req = Request(stream_url, method="GET", headers=headers)
                # Socket timeout just needs to be > server keep-alive interval (15s)
                sock_timeout = max(20.0, min(300.0, float(args.poll_timeout) + 10.0))

                try:
                    with urlopen(req, timeout=sock_timeout) as resp:
                        buf = []
                        while not stop:
                            line = resp.readline()
                            if not line:
                                break
                            s = line.decode("utf-8", errors="replace").rstrip("\r\n")
                            if not s:
                                # dispatch one SSE event
                                if buf:
                                    data_lines = [x[5:].lstrip() for x in buf if x.startswith("data:")]
                                    if data_lines:
                                        try:
                                            evt = json.loads("\n".join(data_lines))
                                            try:
                                                last_id = max(last_id, int(evt.get("id") or 0))
                                            except Exception:
                                                pass
                                            msg = _format_event(evt, self_name)
                                            if msg:
                                                print(msg)
                                        except Exception:
                                            pass
                                buf = []
                                continue
                            if s.startswith(":"):
                                # keep-alive comment
                                continue
                            buf.append(s)
                except Exception:
                    # reconnect with after=last_id
                    time.sleep(1.0)
                    continue

        import threading

        t = threading.Thread(target=_stream, daemon=True)
        t.start()

        # input loop
        try:
            while True:
                line = input()
                if not line:
                    continue
                cmd = line.strip()
                if cmd in ("/quit", "/exit"):
                    break
                if cmd in ("/who", "/users"):
                    who = _request_json(base_url, "GET", "/api/v1/chat/clients", api_key, extra_headers={"X-Session-Id": session_id})
                    print(json.dumps(who, ensure_ascii=False, indent=2))
                    continue
                if cmd == "/discover":
                    res = _request_json(base_url, "POST", "/api/v1/tasks/discover", api_key, {})
                    print(json.dumps(res, ensure_ascii=False, indent=2))
                    continue
                if cmd.startswith("/sendfile "):
                    parts = cmd.split(" ", 2)
                    if len(parts) < 3:
                        print("usage: /sendfile <path> <receiver>")
                        continue
                    file_path = parts[1].strip()
                    receiver = parts[2].strip()
                    if not os.path.isfile(file_path):
                        print(f"file not found: {file_path}")
                        continue
                    with open(file_path, "rb") as f:
                        raw = f.read()
                    data_b64 = base64.b64encode(raw).decode("ascii")
                    filename = os.path.basename(file_path) or "upload.bin"
                    res = _request_json(
                        base_url,
                        "POST",
                        "/api/v1/tasks/sendfile",
                        api_key,
                        {"receiver": receiver, "filename": filename, "data_b64": data_b64},
                    )
                    print(json.dumps(res, ensure_ascii=False, indent=2))
                    continue

                # normal chat message
                _request_json(
                    base_url,
                    "POST",
                    "/api/v1/chat/message",
                    api_key,
                    {"session_id": session_id, "text": line},
                )
        except KeyboardInterrupt:
            pass
        finally:
            stop = True
            _request_json(base_url, "POST", "/api/v1/chat/leave", api_key, {"session_id": session_id})
        return 0

    print(json.dumps({"ok": False, "error": "unknown command"}, ensure_ascii=False, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

