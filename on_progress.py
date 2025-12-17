import time
import serial
import sys
from threading import Thread
import time
import os
import logging
import hashlib
import argparse
# my package
from .infra import *
from . import UI

logger = logging.getLogger('my_logger')
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
            rseq, rpayload = receive_frame(timeout)
            if rpayload == b"\x01\x01ACK" and rseq == seq:
                logger.info("ACK received")
                
                message_seq_num ^= 1  # 切换 seq
                return True
        # 超时重试
    logger.warning("send failed after retries")
    return False

last_message =  ""
def receive_frame_with_dispatcher(ack_retry:int=1,timeout=1):
    seq, payload = receive_frame(timeout)
    if payload is None:
        return None
    elif payload == b"\x01\x01ACK":      # repeated ACK
        return None
    elif payload == b"\x03\x01DISCOVER": # discover others 
        send_frame(f"\x03\x02{username}".encode())
    elif payload[:2] == b'\x03\x02':     # recieve username 
        user_list.append(payload[2:].decode())
        print_log(user_list)
    else:                                # normal message
        if last_message == payload: # TODO
            print("[-] repeat Message")
        else:
            print("[+] data received:", payload)
        # send ACK back
        for _ in range(ack_retry):  # repeat N times , but OT is enough in practice
            send_frame(b"\x01\x01ACK", seq)
            time.sleep(0.1)
        last_message = payload
        return payload
    return None

# =========== File Func =============
def run_file_send(file_name, file_receiver):
    recv_hash = hashlib.md5(file_receiver.encode()).digest()[:2]
    if not handshake(file_receiver=file_receiver):
        print_failed(f"Handshake with {file_receiver} failed.")
        return
    # TODO add authentication
    

def handshake(file_receiver,timeout=2,retries=3):
    stage_flag = 0
    for _ in range(retries):
        send_frame(f"\x02\x01REQ:{username}:{file_receiver}".encode(),0xFF)
        start = time.time()
        while (time.time() - start) < timeout:
            rseq, rpayload = receive_frame(timeout)
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

# =========== Discover Func ===========
user_list = []
def run_discover():
    send_frame(b"\x03\x01DISCOVER")

# =========== Main loop =============
work_queue = []

def background_worker():
    while True:
        if len(work_queue) == 0 or ser.in_waiting > 0:
            # print("[后台进程] 正在运行任务...")
            receive_frame_with_dispatcher(timeout=0.5)
            continue
        task = work_queue.pop(0)
        # print(f"[后台进程] 处理任务: {task}")
        if task[0] == "message":
            res = send_frame_with_ack(payload=task.encode(),seq=message_seq_num,timeout=2)
            if res == 1:
                print_success(task)
            else:
                print_failed(task)
        elif task[0] == "sendfile":
            file_name = task[1]
            file_receiver = task[2]
            if not os.path.isfile(file_name):
                print_failed(f"[backend] file not found: {file_name}")
                continue
            recei_hash = hashlib.md5(file_receiver.encode()).digest()[:2]
            run_file_send(file_name=file_name, file_receiver=file_receiver)
        elif task[0] == "discover":
            run_discover()
            

def foreground_shell():
    while True:
        cmd = input("\nshell> ")
        if cmd in ("exit", "quit"):
            break
        # cmd = cmd.split(" ")
        if cmd.startswith("message"):
            if len(cmd) < 6:
                print("Usage: send <message>")
                continue
            # send_frame(payload=cmd[1].encode(),seq=0)
            work_queue.append(("message",cmd[7:]))
            print(f"message send task added: {cmd[7:]}")
        if cmd.startswith("sendfile"):
            if len(cmd) < 10:
                print("Usage: sendfile filename")
            cmd_content = cmd[9:]
            cmd_content = cmd_content.split(" ")

            work_queue.append(("sendfile",cmd_content[0],cmd_content[1]))
            print(f"message send task added: {cmd_content[0],cmd_content[1]}")
    

def main():
    global ser
    # no global declaration needed here.argv[1]
    ser = serial.Serial(port, 9600, timeout=1)
    p = Thread(target=background_worker, daemon=True)
    p.start()

    foreground_shell()
# TODO add limit about MAXPAYLOAD
main()