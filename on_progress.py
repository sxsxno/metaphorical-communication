import struct
import time
import serial
import zlib
import sys
from threading import Thread

import subprocess
import time
import random

import logging
import os
from .infra import *
logger = logging.getLogger('my_logger')
logger.propagate = False
logger.setLevel(logging.DEBUG)

logging.getLogger().setLevel(logging.ERROR)
port = sys.argv[1]
async_time = 5
# ser = 

# def send_frame_with_ack(payload: bytes, seq: int):
#     # while True:
#     for i in range(3):
#         send_frame(payload, seq)
#         time.sleep(0.5)  # wait for a moment before checking for ACK
#     # send_frame(payload, seq)
#     for i in range(2):
#         ack = receive_frame(seq, 6)
#         if ack is not None and ack == b"ACK":
#             logger.info("ACK received")
#             return 1
#     return 0
    # time.sleep(0.5)
# def wait_for_clear_channel():
#     if not channel_is_busy():
#         time.sleep(random.uniform(0.01, 0.05))  # 随机退避10~50ms


seq_num = 0  # Stop-and-Wait seq
def send_frame_with_ack(payload: bytes, seq: int, retries=5, timeout=1):
    global seq_num
    for _ in range(retries):
        send_frame(payload, seq)
        start = time.time()
        while (time.time() - start) < timeout:
            rseq, rpayload = receive_frame(timeout)
            if rpayload == b"\x01\x01ACK" and rseq == seq:
                logger.info("ACK received")
                seq_num ^= 1  # 切换 seq
                return True
        # 超时重试
    logger.warning("send failed after retries")
    return False    

    
# def receive_frame_with_ack(seq: int, deadline: int):
#     ack = receive_frame(seq, deadline)
#     if ack is not None and ack != b"ACK":
#         print("data received:", ack)
#         time.sleep(2)
#         send_frame(payload=b"ACK", seq=seq)
#         time.sleep(2)
#         send_frame(payload=b"ACK", seq=seq)
#         time.sleep(2)
#         send_frame(payload=b"ACK", seq=seq)
#         return 1
#     return 0

def receive_frame_with_ack(ack_retry:int=1,timeout=1):
    seq, payload = receive_frame(timeout)
    if payload and payload != b"\x01\x01ACK":
        print("data received:", payload)
        # 自动发送 ACK
        for _ in range(ack_retry):  # 连续发n次确保对方收到,实际发现丢包率没那么高,所以应该在发送方retry
            send_frame(b"\x01\x01ACK", seq)
            time.sleep(0.1)
        return payload
    return None

work_queue = []

def background_worker():
    while True:
        if len(work_queue) == 0 or ser.in_waiting > 0:
            # print("[后台进程] 正在运行任务...")
            receive_frame_with_ack(timeout=0.5)
            continue
        task = work_queue.pop(0)
        # print(f"[后台进程] 处理任务: {task}")
        if task[0] == "message":
            res = send_frame_with_ack(payload=task.encode(),seq=0)
            if res == 1:
                print(f"[backend] success: {task}")
            else:
                print(f"[backend] failed: {task}")
        elif task[0] == "sendfile":
            filename = task[1]
            if not os.path.isfile(filename):
                print(f"[backend] file not found: {filename}")
                continue
            pass

def foreground_shell():
    while True:
        cmd = input("shell> ")
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
            work_queue.append(("sendfile",cmd[9:]))
            print(f"message send task added: {cmd[9:]}")
def main():
    global ser
    # no global declaration needed here.argv[1]
    ser = serial.Serial(port, 9600, timeout=1)

    p = Thread(target=background_worker, daemon=True)
    p.start()

    foreground_shell()
    
main()