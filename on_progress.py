import argparse
import hashlib
import logging
import os
import tempfile
import time
from queue import Empty, Queue
from threading import Event, Lock, Thread

import serial

# my package
from infra import *
import lan_api

from UI import *


# ========= Runtime state (initialized in main) =========
port = None      # type: str | None
username = None  # type: str | None
user_hash = None # type: bytes | None
api_key = None   # type: str | None

stop_event = Event()

# ========= Task / Event state =========
work_queue = Queue()
_tasks_lock = Lock()
_tasks = {}

_events_lock = Lock()
_event_seq = 0
_events = []
_MAX_EVENTS = 1000

_uploads_dir = None

user_list = []

def _new_id() -> str:
    return hashlib.md5(f"{time.time_ns()}:{os.getpid()}".encode()).hexdigest()[:16]

def _task_put(kind: str, payload: dict) -> str:
    task_id = _new_id()
    task = {
        "id": task_id,
        "kind": kind,
        "payload": payload,
        "status": "queued",  # queued|running|completed|failed
        "created_at": time.time(),
        "updated_at": time.time(),
        "result": None,
        "error": None,
    }
    with _tasks_lock:
        _tasks[task_id] = task
    work_queue.put(task_id)
    return task_id

def _task_update(task_id: str, *, status=None, result=None, error=None) -> None:
    with _tasks_lock:
        t = _tasks.get(task_id)
        if not t:
            return
        if status is not None:
            t["status"] = status
        if result is not None:
            t["result"] = result
        if error is not None:
            t["error"] = error
        t["updated_at"] = time.time()

def _task_get(task_id: str):
    with _tasks_lock:
        t = _tasks.get(task_id)
        return dict(t) if t else None

def _event_add(event_type: str, data: dict) -> dict:
    global _event_seq
    with _events_lock:
        _event_seq += 1
        evt = {"id": _event_seq, "ts": time.time(), "type": event_type, "data": data}
        _events.append(evt)
        if len(_events) > _MAX_EVENTS:
            del _events[: len(_events) - _MAX_EVENTS]

    # Push to SSE subscribers (if API is running). This keeps "broadcast" behavior without polling.
    try:
        lan_api.publish(evt)
    except Exception:
        pass

    return evt

def _events_after(after_id: int) -> list[dict]:
    with _events_lock:
        if after_id <= 0:
            return list(_events)
        return [e for e in _events if e["id"] > after_id]


logger = logging.getLogger("my_logger")
logger.propagate = False
logger.setLevel(logging.DEBUG)

logging.getLogger().setLevel(logging.ERROR)

parser = argparse.ArgumentParser(description="Sync Unit")
parser.add_argument("--port", "-P", type=str, help="port name", required=True)
parser.add_argument("--name", "-N", type=str, help="User name", required=True)
args = parser.parse_args()

port = args.port
username = args.name
user_hash = hashlib.md5(username.encode()).digest()[:2]
async_time = 5
# ser = 


message_seq_num = 0
def background_worker(ser: Magic_serial):
    while True:
        if stop_event.is_set():
            return
        # 优先处理串口输入（避免阻塞 API 请求线程）
        if ser.in_waiting > 0:
            # print("[后台进程] 正在运行任务...")
            bk_seq, bk_payload = ser.receive_frame(deadline=0.5)
            dispatcher = ser.frame_dispatcher(seq=bk_seq,payload=bk_payload,mode="unlisten")
            if dispatcher is None:
                continue
            print_log(f'recieved {dispatcher}')
            print_commu(dispatcher)
            # if dispatcher.startswith(b"\x02"):
            #     file_sender = dispatcher[1:].decode()
            #     run_file_receive(timeout=8,file_sender=file_sender) # TODO
            time.sleep(0.3)
            continue
        try:
            task_id = work_queue.get(timeout=0.2)
        except Empty:
            continue

        task = _task_get(task_id)
        if not task:
            continue
        _task_update(task_id, status="running")
        # print(task)
        # print(f"[后台进程] 处理任务: {task}")
        try:
            if task["kind"] == "message":
                text = task["payload"]["text"]
                sender = task["payload"].get("from") or username
                via = task["payload"].get("via") or "local"
                res = ser.send_frame_with_ack(payload=text.encode(),seq=message_seq_num,timeout=2)
                if res:
                    print_success(task)
                    _task_update(task_id, status="completed", result={"ok": True})
                else:
                    print_failed(task)
                    _task_update(task_id, status="failed", result={"ok": False})
                message_seq_num += 1
                message_seq_num = message_seq_num & 0xff
                _event_add("chat.message", {"from": sender, "text": text, "via": via, "ok": bool(res)})
            # elif task["kind"] == "sendfile":
            #     file_name = task["payload"]["file_path"]
            #     file_receiver = task["payload"]["receiver"]
            #     cleanup = bool(task["payload"].get("cleanup", False))
            #     if not os.path.isfile(file_name):
            #         print_failed(f"[backend] file not found: {file_name}")
            #         _task_update(task_id, status="failed", error=f"file not found: {file_name}")
            #         continue
            #     run_file_send(file_name=file_name, file_receiver=file_receiver)
            #     _task_update(task_id, status="completed", result={"ok": True})
            #     if cleanup:
            #         try:
            #             os.remove(file_name)
            #         except Exception:
            #             pass
            # elif task["kind"] == "discover":
            #     run_discover() # TODO
            #     _task_update(task_id, status="completed", result={"ok": True})
            else:
                _task_update(task_id, status="failed", error=f"unknown task kind: {task['kind']}")
        except Exception as e:
            _task_update(task_id, status="failed", error=str(e))
            

def foreground_shell(cmd):
    if cmd in ("exit", "quit"):
        return
    # cmd = cmd.split(" ")
    if cmd.startswith("message"):
        if len(cmd) < 6:
            print("Usage: message <message>")
            return
        # send_frame(payload=cmd[1].encode(),seq=0)
        task_id = _task_put("message", {"text": cmd[8:]})
        print(f"message send task added: {cmd[8:]} (task_id={task_id})")
    if cmd.startswith("sendfile"):
        if len(cmd) < 10:
            print("Usage: sendfile filename")
        cmd_content = cmd[9:]
        cmd_content = cmd_content.split(" ")

        task_id = _task_put("sendfile", {"file_path": cmd_content[0], "receiver": cmd_content[1]})
        print(f"sendfile task added: {cmd_content[0],cmd_content[1]} (task_id={task_id})")
set_enter_handler(foreground_shell)

def cmd_dispatcher(cmd):
    if cmd in ("exit", "quit"):
        return
    # cmd = cmd.split(" ")
    if cmd.startswith("message"):
        if len(cmd) < 6:
            print_log("Usage: message <message>")
            return
        # send_frame(payload=cmd[1].encode(),seq=0)
        work_queue.append(("message",cmd[8:]))
        print_log(f"message send task added: {cmd[8:].strip()}")
    if cmd.startswith("sendfile"):
        if len(cmd) < 10:
            print_log("Usage: sendfile filename")
        cmd_content = cmd[9:]
        cmd_content = cmd_content.split(" ")
        work_queue.append(("sendfile",cmd_content[0],cmd_content[1]))
        print_log(f"message send task added: {cmd_content[0],cmd_content[1]}")

set_enter_handler(cmd_dispatcher)
#  define keyboard enter handler

def _parse_args():
    parser = argparse.ArgumentParser(description="Sync Unit")
    parser.add_argument("--port", "-P", type=str, help="port name", required=True)
    parser.add_argument("--name", "-N", type=str, help="User name", required=True)

    parser.add_argument("--api-enable", action="store_true", help="Expose HTTP API for LAN clients")
    parser.add_argument("--api-host", type=str, default="0.0.0.0", help="API bind host (use 0.0.0.0 for LAN)")
    parser.add_argument("--api-port", type=int, default=8000, help="API bind port")
    parser.add_argument("--api-key", type=str, default=None, help="API key (sent via X-API-Key header)")
    parser.add_argument("--no-shell", action="store_true", help="Disable interactive shell (headless mode)")

    return parser.parse_args()

def main():
    global port, username, user_hash, api_key, _uploads_dir

    args = _parse_args()
    port = args.port
    username = args.name
    user_hash = hashlib.md5(username.encode()).digest()[:2]
    api_key = args.api_key

    if args.api_enable and args.api_host not in ("127.0.0.1", "localhost") and not api_key:
        raise SystemExit("Refusing to bind API on non-localhost without --api-key")

    # no global declaration needed here.argv[1]
    ser = Magic_serial(port=port,bitrate=9600,timeout=3)
    # foreground_shell()
    _uploads_dir = tempfile.mkdtemp(prefix="on_progress_uploads_")
    p = Thread(target=background_worker, args=(ser), daemon=True)
    p.start()

    httpd = None
    if args.api_enable:
        def _api_get_status():
            return {
                "username": username,
                "port": port,
                "users": list(user_list),
                "message_seq_num": message_seq_num,
            }

        def _api_save_upload(filename, raw_bytes):
            safe_name = os.path.basename(filename or "upload.bin")
            if not _uploads_dir:
                raise RuntimeError("uploads dir not ready")
            tmp_path = os.path.join(_uploads_dir, f"{_new_id()}_{safe_name}")
            with open(tmp_path, "wb") as f:
                f.write(raw_bytes)
            return tmp_path

        httpd = lan_api.start_api(
            args.api_host,
            args.api_port,
            api_key=api_key,
            task_put=_task_put,
            task_get=_task_get,
            events_after=_events_after,
            get_status=_api_get_status,
            save_upload=_api_save_upload,
            event_add=_event_add,
        )
        print_success(f"API ready: http://{args.api_host}:{args.api_port} (auth={'on' if api_key else 'off'})")
    try:
        if args.no_shell:
            # Headless mode: block without polling (Ctrl+C will interrupt).
            stop_event.wait()
        else:
            app.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if httpd:
            try:
                httpd.shutdown()
            except Exception:
                pass
        try:
            ser.close()
        except Exception:
            pass

# TODO add limit about MAXPAYLOAD
if __name__ == "__main__":
    main()