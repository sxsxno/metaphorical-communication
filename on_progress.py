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

# ========= Message Func ===========
message_seq_num = 0  # Stop-and-Wait seq
def send_frame_with_ack(payload: bytes, seq: int, retries=2, timeout=2):
    global message_seq_num
    for _ in range(retries):
        send_frame(payload, seq)
        start = time.time()
        while (time.time() - start) < timeout:
            rseq, rpayload = receive_frame(deadline=timeout)
            if rpayload == b"\x01\x01ACK" and rseq == seq:
                logger.info("ACK received")
                
                message_seq_num ^= 1  # 切换 seq
                return True
            # 不中断 ACK 等待的前提下，尽量不丢弃其它帧（至少记录事件）
            if rpayload is not None:
                _event_add("serial.frame", {"seq": rseq, "payload_hex": rpayload.hex()})
        # 超时重试
    logger.warning("send failed after retries")
    return False

last_message =  ""
def frame_dispatcher(seq, payload, ack_retry:int=1,mode="unlisten"):
    global last_message
    if payload is None:
        return None
    elif payload == b"\x01\x01ACK":      # repeated ACK
        return None
    elif payload[:10] == b"\x03\x01DISCOVER": # discover others 
        # TODO
        if mode == "listen":
            return None
        send_frame(f"\x03\x02{username}".encode())
    elif payload[:2] == b'\x03\x02':     # recieve username 
        user_list.append(payload[2:].decode())
        print_log(user_list)
        _event_add("discover.user", {"username": payload[2:].decode(errors="replace")})
    elif payload[:2] == b'\x02\x01': # handshake request
        if mode == "listen":
            return None
        parts = payload.decode().split(":")
        if len(parts) != 3:
            print_failed("Invalid handshake request")
            return None
        msg_command, file_sender, file_receiver = parts
        if msg_command != "\x02\x01REQ":
            print_failed("Invalid handshake command")
            return None
        if file_receiver != username:
            print_failed("Handshake request not for us")
            return None
        print_log(f"Handshake request from {file_sender}")
        _event_add("file.handshake_request", {"from": file_sender, "to": file_receiver})
        # send ACK back
        time.sleep(1) # add lag by hand
        send_frame(f"\x02\x02ACK:{file_sender}:{username}".encode(), seq)
        print_log("send ACK back")
        return f"\x02{file_sender}".encode()
    else:                                # normal message
        # if last_message == payload: # TODO
        #     print("[-] repeat Message")
        # else:
        print_log("[+] data received:", payload)
        print_commu(payload.decode())
        try:
            text = payload.decode("utf-8", errors="replace")
        except Exception:
            text = None
        _event_add("message.recv", {"seq": seq, "payload_hex": payload.hex(), "text": text})
        # send ACK back
        for _ in range(ack_retry):  # repeat N times , but OT is enough in practice
            send_frame(b"\x01\x01ACK", seq)
            time.sleep(0.1)
        last_message = payload
        return payload
    return None

# =========== File Func =============

def send_file_check_if_FIN(packet_, fin_count):
    for i in range(0, fin_count):
        if packet_[i] == b'0':
            print_failed("File receive incomplete, missing packets.")
            return False
    return True

def send_file_write_to_file(remote_file_packet):
    # assemble file
    remote_file = b""
    for parts in remote_file_packet[1:-1]: # skip file info and fin
        remote_file += parts[3:] # skip user hash and ":"
    # verify file hash
    parts = remote_file_packet[0].split(b":")
    file_name       = parts[0].decode()
    total_packets   = int(parts[1].decode())
    file_hash       = parts[2]
    calc_hash = hashlib.md5(remote_file).hexdigest().encode()
    if calc_hash != file_hash:
        print_failed("File hash mismatch, transfer failed.")
        return
    with open(f"recv_{file_name}", "wb") as f:
        f.write(remote_file)
    print_success(f"File {file_name} received successfully.")
    _event_add("file.received", {"filename": file_name, "bytes": len(remote_file)})
    return

def run_file_send(file_name, file_receiver):
    recv_hash = hashlib.md5(file_receiver.encode()).digest()[:2]
    if not handshake(file_receiver=file_receiver):
        print_failed(f"Handshake with {file_receiver} failed.")
        return
    # TODO add authentication
    with open(file_name, "rb") as f:
        file_data = f.read()
    per_chunk_size = MAX_PAYLOAD - 3
    total_packets = len(file_data) // per_chunk_size
    # align total packets
    if total_packets * per_chunk_size < len(file_data):
        total_packets += 1
    
    total_packets += 2 # file info and fin  
    # send file info first
    file_hash = hashlib.md5(file_data).hexdigest().encode()
    # set up packet
    packet = [b'0']*(total_packets)
    # 0 for file info
    packet[0] = recv_hash + f":{file_name}:{total_packets}:".encode() + file_hash
    for i in range(total_packets-1): # -1 for fin
        id = i + 1
        chunk = file_data[i*per_chunk_size:(i+1)*per_chunk_size]
        packet[id] = recv_hash + b":" + chunk
    packet[total_packets-1] = recv_hash + b"\x02\x05FIN"
    stage_count = 0
    ack_count = 0
    timeout_count = 0
    # windows send  
    while True:
        for i in range(ack_count, min(ack_count + WINDOWS_SIZE, total_packets - (stage_count * 0x100), 0xff)):
            packet_id = i + stage_count * 256
            send_frame(packet[packet_id], seq=packet_id % 256)
            print_log(f"Sent packet {packet_id}/{total_packets} to {file_receiver}")
        rseq, rpayload = receive_frame(deadline=3)
        if rpayload is None:
            timeout_count += 1
            if timeout_count >= 5:
                print_failed("File send timeout.")
                return
            print_failed("Timeout waiting for ACK, resending window.")
            continue
        if rpayload[:2] != user_hash:
            frame_dispatcher(rseq, rpayload, mode="listen")
            continue
        elif rpayload[:2] == user_hash:
            content = rpayload[2:]
            if content[:5] == b"\x02\x04ACK":
                if rseq == content[6]:
                    ### TO check if it works
                    if rseq == 0x00 and ack_count >= 0x30: # in case 0xff ack didnt receive
                        stage_count += 1
                        ack_count = 0
                    ### 
                    else:
                        ack_count = rseq
                    if rseq == 0xff:
                        stage_count += 1
                        ack_count = 0
                    continue
                else:
                    frame_dispatcher(rseq, rpayload, mode="listen")
                    continue
            else:
                frame_dispatcher(rseq, rpayload, mode="listen")
                continue
            
        
        
def handshake(file_receiver,timeout=8,retries=3):
    stage_flag = 0
    for _ in range(retries):
        file_sender = username
        send_frame(f"\x02\x01REQ:{file_sender}:{file_receiver}".encode(),0xFF)
        start = time.time()
        while (time.time() - start) < timeout:
            rseq, rpayload = receive_frame(deadline=timeout)
            if rpayload == f"\x02\x02ACK:{username}:{file_receiver}".encode() and rseq == 0xFF:
                # logger.info("ACK received")
                stage_flag = 1
                break
        if stage_flag == 1:
            break
    if stage_flag == 0:
        return False 
    send_frame(b"\x02\x03ACK",0x0)
    return True

def run_file_receive(timeout,file_sender):
    fin_count = -1
    ack_count = 0
    timeout_count = 0
    file_sender_hash = hashlib.md5(file_sender.encode()).digest()[:2]
    remote_file_packet = []
    windows_packet = [b'0'] *  0x100
    print_log("enter file receive mode")
    while True:
        # start = time.time()
        for i in range(0, WINDOWS_SIZE):
            rseq, rpayload = receive_frame(deadline=timeout) # wait till first packet 
            # print(rpayload[:2], user_hash)
            if rseq is None and rpayload is None:
                # remote did not respond
                timeout_count += 1
                if timeout_count >= 5:
                    print_failed("File receive timeout.")
                    return
                continue
            if rpayload == b"\x02\x03ACK":
                print_success("Handshake complete, ready to receive file.")
                continue
            elif rpayload[:5] == b"\x02\x01REQ":
                parts = rpayload.decode().split(":")
                if len(parts) != 3:
                    print_failed("Invalid handshake request")
                    return None
                msg_command, file_sender_req, file_receiver_req = parts
                if file_receiver_req != username:
                    continue
                if file_sender_req != file_sender:
                    continue
                if file_sender_req == file_sender:
                    # re-send ACK
                    send_frame(f"\x02\x02ACK:{file_sender}:{username}".encode(), seq=rseq)
                    start = time.time()
                    continue
            elif rpayload[2:7] == b"\x02\x05FIN" and rpayload[:2] == user_hash:
                fin_count = rseq
                # check if complete
                if send_file_check_if_FIN(windows_packet, fin_count):
                    remote_file_packet += windows_packet
                    send_file_write_to_file(remote_file_packet)
                    return
                else:
                    continue

            elif rpayload[:2] != user_hash:
                frame_dispatcher(rseq, rpayload, mode="listen")
                continue
            elif rpayload[:2] == user_hash:
                if rpayload[3] != b':':
                    print_log("Stage 3")
                    frame_dispatcher(rseq, rpayload, mode="listen")
                else:
                    print_log("Stage 2")
                    if rseq >= ack_count + WINDOWS_SIZE or rseq < ack_count:
                        # out of window
                        continue
                    packet_content = rpayload[3:]
                    windows_packet[rseq] = packet_content
                    print_log(f"Received packet {rseq} from {file_sender}")
                    if fin_count != -1: # fin received should check if complete
                        if send_file_check_if_FIN(windows_packet, fin_count):
                            remote_file_packet += windows_packet
                            send_file_write_to_file(remote_file_packet)
                            return
                        else:
                            continue
        for i in range(0, len(windows_packet)):
            if windows_packet[i] == b'0':
                break
            else:
                ack_count = i
        send_frame(file_sender_hash + b"\x02\x04ACK:" + bytes([ack_count]), seq=ack_count)
        if ack_count == 0xff:
            ack_count = 0
            remote_file_packet += windows_packet
            windows_packet = [b'0'] *  0x100
                # parts = rpayload.split(b":")
                # if len(parts) < 4:
                #     frame_dispatcher(rseq, rpayload, mode="listen")
                #     continue
                # elif len(parts) == 4:
                #     # id_hash = parts[0]
                #     file_name       = parts[1].decode()
                #     total_packets   = int(parts[2].decode())
                #     file_hash       = parts[3]
                #     break
                # else:
                #     frame_dispatcher(rseq, rpayload, mode="listen")
                #     continue

        
# =========== Discover Func ===========
user_list = []
def run_discover():
    send_frame(b"\x03\x01DISCOVER")

# =========== Main loop =============


def background_worker():
    while True:
        if stop_event.is_set():
            return

        # 优先处理串口输入（避免阻塞 API 请求线程）
        if ser.in_waiting > 0:
            # print("[后台进程] 正在运行任务...")
            bk_seq, bk_payload = receive_frame(deadline=0.5)
            dispatcher = frame_dispatcher(seq=bk_seq,payload=bk_payload,mode="unlisten")
            if dispatcher is None:
                continue
            print_log(dispatcher,"Stage 1")
            # match dispatcher:
            #     case b"\x02":
            if dispatcher.startswith(b"\x02"):
                file_sender = dispatcher[1:].decode()
                run_file_receive(timeout=8,file_sender=file_sender) # TODO
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
                res = send_frame_with_ack(payload=text.encode(),seq=message_seq_num,timeout=2)
                if res:
                    print_success(task)
                    _task_update(task_id, status="completed", result={"ok": True})
                else:
                    print_failed(task)
                    _task_update(task_id, status="failed", result={"ok": False})
                _event_add("chat.message", {"from": sender, "text": text, "via": via, "ok": bool(res)})
            elif task["kind"] == "sendfile":
                file_name = task["payload"]["file_path"]
                file_receiver = task["payload"]["receiver"]
                cleanup = bool(task["payload"].get("cleanup", False))
                if not os.path.isfile(file_name):
                    print_failed(f"[backend] file not found: {file_name}")
                    _task_update(task_id, status="failed", error=f"file not found: {file_name}")
                    continue
                run_file_send(file_name=file_name, file_receiver=file_receiver)
                _task_update(task_id, status="completed", result={"ok": True})
                if cleanup:
                    try:
                        os.remove(file_name)
                    except Exception:
                        pass
            elif task["kind"] == "discover":
                run_discover() # TODO
                _task_update(task_id, status="completed", result={"ok": True})
            else:
                _task_update(task_id, status="failed", error=f"unknown task kind: {task['kind']}")
        except Exception as e:
            _task_update(task_id, status="failed", error=str(e))
            

def foreground_shell():
    while True:
        cmd = input("\nshell> ")
        if cmd in ("exit", "quit"):
            break
        # cmd = cmd.split(" ")
        if cmd.startswith("message"):
            if len(cmd) < 6:
                print("Usage: message <message>")
                continue
            # send_frame(payload=cmd[1].encode(),seq=0)
            task_id = _task_put("message", {"text": cmd[7:]})
            print(f"message send task added: {cmd[7:]} (task_id={task_id})")
        if cmd.startswith("sendfile"):
            if len(cmd) < 10:
                print("Usage: sendfile filename")
            cmd_content = cmd[9:]
            cmd_content = cmd_content.split(" ")

            task_id = _task_put("sendfile", {"file_path": cmd_content[0], "receiver": cmd_content[1]})
            print(f"sendfile task added: {cmd_content[0],cmd_content[1]} (task_id={task_id})")
    

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
    global ser
    global port, username, user_hash, api_key, _uploads_dir

    args = _parse_args()
    port = args.port
    username = args.name
    user_hash = hashlib.md5(username.encode()).digest()[:2]
    api_key = args.api_key

    if args.api_enable and args.api_host not in ("127.0.0.1", "localhost") and not api_key:
        raise SystemExit("Refusing to bind API on non-localhost without --api-key")

    # no global declaration needed here.argv[1]
    ser = serial.Serial(port, 9600, timeout=1)
    init_serial(ser)
    init_logger(logger)
    
    app.run()
    

    _uploads_dir = tempfile.mkdtemp(prefix="on_progress_uploads_")
    p = Thread(target=background_worker, daemon=True)
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
            while True:
                time.sleep(1)
        else:
            foreground_shell()
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