import struct
import time
import serial
import zlib
import sys
from threading import Thread

import subprocess
import time

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
def receive_frame(seq: int, deadline: int):
    logging.info(f"enter receiving mode")
    data = bytearray()
    magic = bytearray()
    start_time = int(time.time())
    while (time.time() - start_time) < deadline:
        # ensure magic exists at front first
        magic.extend(read_exact(len(MAGIC_BYTES) - len(magic), deadline))
        if len(magic) < len(MAGIC_BYTES):
            continue
        if magic != MAGIC_BYTES:
            magic.pop(0)
            continue
        
        header = read_exact(HEADER_SIZE, deadline)
        if len(header) < HEADER_SIZE:
            continue
        chksum, remote_seq, length = struct.unpack(HEADER_FMT, header)

        payload = read_exact(length, deadline)
        if len(payload) < length:
            continue
        if chksum != zlib.crc32(b'\0'*CHKSUM_SIZE + header[CHKSUM_SIZE:] + payload):
            logging.warning("incorrect checksum")
            continue
        logger.debug(f"receive from serial: {payload}")
        return payload
    return None

def send_frame_with_ack(payload: bytes, seq: int):
    # while True:
    for i in range(3):
        send_frame(payload, seq)
        time.sleep(0.5)  # wait for a moment before checking for ACK
    # send_frame(payload, seq)
    for i in range(2):
        ack = receive_frame(seq, 6)
        if ack is not None and ack == b"ACK":
            logger.info("ACK received")
            return 1
    return 0
    
    # time.sleep(0.5)
    
def receive_frame_with_ack(seq: int, deadline: int):
    ack = receive_frame(seq, deadline)
    if ack is not None and ack != b"ACK":
        print("data received:", ack)
        time.sleep(2)
        send_frame(payload=b"ACK", seq=seq)
        time.sleep(2)
        send_frame(payload=b"ACK", seq=seq)
        time.sleep(2)
        send_frame(payload=b"ACK", seq=seq)
        return 1
    return 0

work_queue = []

def background_worker():
    while True:
        if len(work_queue) == 0:
            # print("[后台进程] 正在运行任务...")
            receive_frame_with_ack(seq=0, deadline=1)
            continue
        task = work_queue.pop(0)
        # print(f"[后台进程] 处理任务: {task}")
        res = send_frame_with_ack(payload=task.encode(),seq=0)
        if res == 1:
            print(f"[backend] success: {task}")
        else:
            print(f"[backend] failed: {task}")

def foreground_shell():
    while True:
        cmd = input("shell> ")
        if cmd in ("exit", "quit"):
            break
        # cmd = cmd.split(" ")
        if cmd.startswith("send"):
            if len(cmd) < 6:
                print("Usage: send <message>")
                continue
            # send_frame(payload=cmd[1].encode(),seq=0)
            work_queue.append(cmd[5:])
            print(f"task added: {cmd[5:]}")

def main():
    global ser
    # no global declaration needed here.argv[1]
    ser = serial.Serial(port, 9600, timeout=1)

    p = Thread(target=background_worker, daemon=True)
    p.start()

    foreground_shell()
    
main()