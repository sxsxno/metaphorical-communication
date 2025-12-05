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

logger = logging.getLogger('my_logger')
logger.propagate = False
logger.setLevel(logging.DEBUG)

logging.getLogger().setLevel(logging.ERROR)
# init
port = sys.argv[1]
# has_pass = (sys.argv[2]) # 1 means has pass
# ser = serial.Serial(port, 9600, timeout=1)

# send
    # devide package
    # for package
        # add header
        # timeout = avertimeout // timeoutcount
        # while (not recived)
            # write
            # waitfor ACK
            # if recived
                # break
            # if timeout:
                # timeout = 1.5 timeout
                # avertimeout += timeout
                # timeoutcount += 1
                # continue

# recv
    # read package
    # parse package
    # send ACK
    

# handshake 


# mainloop
while True:
    # if has pass
        # if has file in queue
            # send
            # if sendfile end
            # getACKed
            # if pass-req flag
                # start pass-ex
        # if not
            # heartbeat
            # if heartbeat with pass-req
            # set pass-req flag

    # else no pass
        # if recv-file in queue
            # recv file
        # recv heartbeat
        # if need pass-ex
            # send ACK with pass-req
        # else
            # send ACK
    break

# protocol
# 4-byte magic number
MAGIC_BYTES = b'\xFF\xC0\xFF\xEE'
MAX_PAYLOAD = 150  # bytes
# Header: checksum(4), seq(1), len(2)
HEADER_FMT = '>IBH'
HEADER_SIZE = struct.calcsize(HEADER_FMT)
CHKSUM_SIZE = 4


async_time = 5
# ser = 
# atomatic send op
def send_frame(payload: bytes, seq: int):
    global ser
    length = len(payload)
    # first set checksum to 0
    header = struct.pack(HEADER_FMT, 0, seq, length)
    chksum = zlib.crc32(header + payload)
    header = struct.pack(HEADER_FMT, chksum, seq, length)
    payload = MAGIC_BYTES + header + payload
    ser.write(payload)
    logger.debug(f"write to serial: {payload}")
    return len(payload)

# atomatic recv op
def read_exact(size: int, deadline: int) -> bytes:
    global ser
    buf = bytearray()
    start_time = int(time.time())
    while len(buf) < size and (time.time() - start_time) < deadline:
        chunk = ser.read(size - len(buf))
        logger.debug(f"read from serial: {chunk}")

        if not chunk:
            continue
        buf.extend(chunk)
    return bytes(buf)

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

# def handshake():
#     if has_pass == 1:
#         receive_sender_flag = False
#         while True:
#             if receive_sender_flag == True:
#                 send_frame(payload="ACK",seq=0)
#                 if receive_frame(0,async_time) != -1:
#                     send_frame(payload="HAN_FIN",seq=0)
#                     break
#             send_frame(payload=b"Hello From MN",seq=0)
#             if receive_frame(0,async_time) == -1:
#                 # no package recived
#                 continue
#             else:
#                 receive_sender_flag = True
#                 send_frame(payload="ACK",seq=0)
#                 if receive_frame(0,async_time) != -1:
#                     send_frame(payload="HAN_FIN",seq=0)
#                     break
#     else:
#         receive_receiver_flag = False
#         while True:
#             if receive_frame(0,async_time) == -1:
#                 continue
#             else:
#                 receive_receiver_flag = True
#                 send_frame(payload=b"ACK",seq=0)
#                 if receive_frame(0,async_time*2) == -1: # TODO check if get HAN_FIN
#                     break

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


if __name__ == "__main__":
    global ser
    port = sys.argv[1]
    ser = serial.Serial(port, 9600, timeout=1)

    p = Thread(target=background_worker, daemon=True)
    p.start()

    foreground_shell()