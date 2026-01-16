import base64
import json
import os
from queue import Empty, Full, Queue
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from urllib.parse import parse_qs, urlparse


_sessions_lock = Lock()
_sessions = {}  # session_id -> {username, created_at, last_seen}
_username_to_session = {}  # username -> session_id

_subs_lock = Lock()
_subs = {}  # sub_id -> Queue
_SUB_QUEUE_SIZE = 256


def _clean_sessions(now_ts, ttl_seconds=60 * 60 * 6):
    """Best-effort cleanup to avoid unbounded growth."""
    expired = []
    with _sessions_lock:
        for sid, s in list(_sessions.items()):
            last_seen = float(s.get("last_seen") or 0)
            if now_ts - last_seen > ttl_seconds:
                expired.append((sid, s.get("username")))
        for sid, uname in expired:
            _sessions.pop(sid, None)
            if uname and _username_to_session.get(uname) == sid:
                _username_to_session.pop(uname, None)
    return expired


def _touch_session(session_id, now_ts):
    if not session_id:
        return None
    with _sessions_lock:
        s = _sessions.get(session_id)
        if not s:
            return None
        s["last_seen"] = now_ts
        return s.get("username")


def publish(evt):
    """
    Broadcast an event dict to all connected SSE subscribers.
    Safe to call even if the API server isn't started (no subscribers).
    """
    # Don't push very noisy debug frames to every client.
    if (evt or {}).get("type") == "serial.frame":
        return

    with _subs_lock:
        items = list(_subs.items())

    for _, q in items:
        try:
            q.put_nowait(evt)
        except Full:
            # Drop oldest so slow clients still get the newest updates.
            try:
                q.get_nowait()
            except Empty:
                pass
            try:
                q.put_nowait(evt)
            except Exception:
                pass


def _subscribe():
    sub_id = secrets.token_hex(8)
    q = Queue(maxsize=_SUB_QUEUE_SIZE)
    with _subs_lock:
        _subs[sub_id] = q
    return sub_id, q


def _unsubscribe(sub_id):
    with _subs_lock:
        _subs.pop(sub_id, None)


def _build_handler(api_key, task_put, task_get, events_after, get_status, save_upload, event_add=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "on_progress-api/0.1"

        def log_message(self, format, *args):
            # quiet default http logs
            return

        def _send_json(self, status_code, obj):
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(int(status_code))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Client disconnected (common with long-polling / Ctrl+C). Ignore to avoid noisy tracebacks.
                return
            except OSError as e:
                # Windows may raise OSError with winerror 10053/10054
                if getattr(e, "winerror", None) in (10053, 10054):
                    return
                raise

        def _auth_ok(self):
            if api_key is None:
                return True
            return self.headers.get("X-API-Key") == api_key

        def _read_json(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def _sse_write(self, raw_bytes):
            try:
                self.wfile.write(raw_bytes)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return False
            except OSError as e:
                if getattr(e, "winerror", None) in (10053, 10054):
                    return False
                raise

        def do_GET(self):
            if not self._auth_ok():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return

            now_ts = time.time()
            _clean_sessions(now_ts)
            _touch_session(self.headers.get("X-Session-Id"), now_ts)

            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/api/v1/health":
                self._send_json(200, {"ok": True})
                return

            if path == "/api/v1/events/stream":
                after = int(qs.get("after", ["0"])[0] or "0")
                last_sent = after

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                # Subscribe BEFORE backlog to avoid missing events.
                sub_id, q = _subscribe()
                try:
                    backlog = events_after(after)
                    for evt in backlog:
                        try:
                            eid = int(evt.get("id") or 0)
                        except Exception:
                            eid = 0
                        if eid <= last_sent:
                            continue
                        last_sent = eid
                        data = json.dumps(evt, ensure_ascii=False)
                        pkt = f"id: {eid}\nevent: {evt.get('type','')}\ndata: {data}\n\n".encode("utf-8")
                        if not self._sse_write(pkt):
                            return

                    # Keepalive to avoid middlebox timeouts.
                    while True:
                        try:
                            evt = q.get(timeout=15.0)
                        except Empty:
                            _touch_session(self.headers.get("X-Session-Id"), time.time())
                            if not self._sse_write(b": keep-alive\n\n"):
                                return
                            continue

                        try:
                            eid = int((evt or {}).get("id") or 0)
                        except Exception:
                            eid = 0
                        if eid <= last_sent:
                            continue
                        last_sent = eid

                        _touch_session(self.headers.get("X-Session-Id"), time.time())
                        data = json.dumps(evt, ensure_ascii=False)
                        pkt = f"id: {eid}\nevent: {(evt or {}).get('type','')}\ndata: {data}\n\n".encode("utf-8")
                        if not self._sse_write(pkt):
                            return
                finally:
                    _unsubscribe(sub_id)

            if path == "/api/v1/status":
                status = get_status() or {}
                status["ok"] = True
                self._send_json(200, status)
                return

            if path == "/api/v1/users":
                status = get_status() or {}
                self._send_json(200, {"ok": True, "users": list(status.get("users") or [])})
                return

            if path == "/api/v1/events":
                after = int(qs.get("after", ["0"])[0] or "0")
                wait = (qs.get("wait", ["0"])[0] or "0").lower() in ("1", "true", "yes", "y")
                timeout_s = float(qs.get("timeout", ["30"])[0] or "30")
                timeout_s = max(0.0, min(timeout_s, 60.0))
                events = events_after(after)
                if wait and not events and timeout_s > 0:
                    deadline = time.time() + timeout_s
                    while time.time() < deadline:
                        time.sleep(0.25)
                        events = events_after(after)
                        if events:
                            break
                self._send_json(200, {"ok": True, "events": events})
                return

            if path.startswith("/api/v1/tasks/"):
                task_id = path.rsplit("/", 1)[-1]
                t = task_get(task_id)
                if not t:
                    self._send_json(404, {"ok": False, "error": "task not found"})
                    return
                self._send_json(200, {"ok": True, "task": t})
                return

            if path == "/api/v1/chat/clients":
                with _sessions_lock:
                    clients = [
                        {"username": s.get("username"), "last_seen": s.get("last_seen")}
                        for s in _sessions.values()
                    ]
                self._send_json(200, {"ok": True, "count": len(clients), "clients": clients})
                return

            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._auth_ok():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return

            now_ts = time.time()
            _clean_sessions(now_ts)

            parsed = urlparse(self.path)
            path = parsed.path
            body = self._read_json()

            # -------- Chat session --------
            if path == "/api/v1/chat/join":
                username = (body.get("username") or "").strip()
                if not username:
                    self._send_json(400, {"ok": False, "error": "username is required"})
                    return
                if len(username) > 64:
                    self._send_json(400, {"ok": False, "error": "username too long (max 64)"})
                    return

                with _sessions_lock:
                    if username in _username_to_session:
                        self._send_json(409, {"ok": False, "error": "username already in use"})
                        return
                    session_id = secrets.token_hex(8)
                    _sessions[session_id] = {"username": username, "created_at": now_ts, "last_seen": now_ts}
                    _username_to_session[username] = session_id

                if event_add:
                    try:
                        event_add("chat.join", {"username": username})
                    except Exception:
                        pass

                self._send_json(200, {"ok": True, "session_id": session_id, "username": username})
                return

            if path == "/api/v1/chat/leave":
                session_id = (body.get("session_id") or "").strip()
                if not session_id:
                    self._send_json(400, {"ok": False, "error": "session_id is required"})
                    return
                with _sessions_lock:
                    s = _sessions.pop(session_id, None)
                    if s and _username_to_session.get(s.get("username")) == session_id:
                        _username_to_session.pop(s.get("username"), None)
                if not s:
                    self._send_json(404, {"ok": False, "error": "session not found"})
                    return
                if event_add:
                    try:
                        event_add("chat.leave", {"username": s.get("username")})
                    except Exception:
                        pass
                self._send_json(200, {"ok": True})
                return

            if path == "/api/v1/chat/message":
                session_id = (body.get("session_id") or "").strip()
                text = (body.get("text") or "").strip()
                if not session_id:
                    self._send_json(400, {"ok": False, "error": "session_id is required"})
                    return
                if not text:
                    self._send_json(400, {"ok": False, "error": "text is required"})
                    return
                sender = _touch_session(session_id, now_ts)
                if not sender:
                    self._send_json(401, {"ok": False, "error": "invalid session"})
                    return
                task_id = task_put("message", {"text": text, "from": sender, "via": "lan"})
                self._send_json(200, {"ok": True, "task_id": task_id})
                return

            if path == "/api/v1/tasks/message":
                text = (body.get("text") or "").strip()
                if not text:
                    self._send_json(400, {"ok": False, "error": "text is required"})
                    return
                sender = (body.get("from") or "").strip() or None
                payload = {"text": text}
                if sender:
                    payload["from"] = sender
                    payload["via"] = "api"
                task_id = task_put("message", payload)
                self._send_json(200, {"ok": True, "task_id": task_id})
                return

            if path == "/api/v1/tasks/discover":
                task_id = task_put("discover", {})
                self._send_json(200, {"ok": True, "task_id": task_id})
                return

            if path == "/api/v1/tasks/sendfile":
                receiver = (body.get("receiver") or "").strip()
                filename = (body.get("filename") or "upload.bin").strip() or "upload.bin"
                data_b64 = body.get("data_b64") or ""
                if not receiver:
                    self._send_json(400, {"ok": False, "error": "receiver is required"})
                    return
                if not data_b64:
                    self._send_json(400, {"ok": False, "error": "data_b64 is required"})
                    return
                try:
                    raw = base64.b64decode(data_b64.encode("ascii"), validate=True)
                except Exception:
                    self._send_json(400, {"ok": False, "error": "invalid base64"})
                    return

                safe_name = os.path.basename(filename)
                try:
                    tmp_path = save_upload(safe_name, raw)
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": f"upload failed: {e}"})
                    return

                task_id = task_put("sendfile", {"file_path": tmp_path, "receiver": receiver, "cleanup": True})
                self._send_json(200, {"ok": True, "task_id": task_id})
                return

            self._send_json(404, {"ok": False, "error": "not found"})

    return Handler


def start_api(
    host,
    port,
    *,
    api_key=None,
    task_put=None,
    task_get=None,
    events_after=None,
    get_status=None,
    save_upload=None,
    event_add=None,
):
    """
    Start HTTP API server in a background thread.

    Required callbacks:
    - task_put(kind:str, payload:dict) -> task_id:str
    - task_get(task_id:str) -> task:dict|None
    - events_after(after_id:int) -> list[dict]
    - get_status() -> dict (JSON serializable)
    - save_upload(filename:str, raw_bytes:bytes) -> file_path:str
    """
    if task_put is None or task_get is None or events_after is None or get_status is None or save_upload is None:
        raise ValueError("start_api missing required callbacks")

    handler_cls = _build_handler(api_key, task_put, task_get, events_after, get_status, save_upload, event_add=event_add)
    httpd = ThreadingHTTPServer((host, int(port)), handler_cls)
    t = Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd

