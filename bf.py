import struct
import time
import serial
import zlib
import sys

import logging
import os

# 4-byte magic number
MAGIC_BYTES = b'\xDE\xAD\xBE\xEF'
MAX_PAYLOAD = 150  # bytes
# Header: checksum(4), seq(1), len(2)
HEADER_FMT = '>IBH'
HEADER_SIZE = struct.calcsize(HEADER_FMT)
CHKSUM_SIZE = 4

mode = sys.argv[1]
port = sys.argv[2]
file = sys.argv[3]
output_dir = sys.argv[4]

try:
    os.mkdir(output_dir)
except:
    pass


logger = logging.getLogger('my_logger')
logger.propagate = False
logger.setLevel(logging.DEBUG)

# 创建文件处理器
file_handler = logging.FileHandler(f'{output_dir}/my_log.log')
file_handler.setLevel(logging.DEBUG)  # 设置此处理器的日志级别

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)



logging.getLogger().setLevel(logging.INFO)

ser = serial.Serial(port, 9600, timeout=1)

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

def read_exact(size: int, deadline: int) -> bytes:
    buf = bytearray()
    while len(buf) < size and time.time() < deadline:
        chunk = ser.read(size - len(buf))
        logger.debug(f"read from serial: {chunk}")

        if not chunk:
            continue
        buf.extend(chunk)
    return bytes(buf)

def send_file(file: str, seq: int, deadline: int):
    logging.info(f"enter sending mode")
    with open(file, "rb") as f:
        payload = f.read()
    file_off = 0
    sent = 0
    while time.time() < deadline:
        single_payload = struct.pack(">II", len(payload), file_off) + payload[file_off: file_off+MAX_PAYLOAD]
        send_frame(single_payload, seq)
        sent += len(single_payload) - 8
        file_off += MAX_PAYLOAD
        time.sleep(1)
        if file_off > len(payload):
            file_off = 0
        logging.info(f"sent: {sent}, {sent/len(payload)*100:.2f}%")


def receive_file(file: str, seq: int, deadline: int):
    logging.info(f"enter receiving mode")
    data = bytearray()
    magic = bytearray()
    while time.time() < deadline:
        # ensure magic exists at front first
        magic.extend(read_exact(len(MAGIC_BYTES) - len(magic), deadline))
        if len(magic) < len(MAGIC_BYTES):
            continue
        if magic != MAGIC_BYTES:
            magic.pop(0)
            continue

        magic = bytearray()

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
        if remote_seq != seq:
            logging.warning("incorrect seq")
            continue
    
        total_len, off = struct.unpack(">II", payload[:8])
        payload = payload[8:]
        length = len(payload)
        data = data.ljust(off, b'\0')[:off] + payload + data[off+length:]
        with open(file, "wb") as f:
            f.write(data)
    
        logging.info(f"received {len(payload)} valid data, total: {total_len}, offset: {off}, {(off+length)/total_len*100:.2f}%")
    

# sync every 10 minutes
# [0, 5): node A send, node B receive
# [5, 10): node B send, node A receive

sync_time = 60 * 10
off_sync = 5

logging.info(f"act as mode {mode}")

while True:
    # align to 10 min first
    start_time = int(time.time())
    time_offset = sync_time - (start_time % sync_time)
    logging.info(f"wait for {time_offset}s to sync")
    time.sleep(time_offset)
    start_time += time_offset

    seq = (start_time // sync_time) & 0xff

    if mode == 'A':
        send_file(file, seq, start_time + sync_time // 2)
        receive_file(f"{output_dir}/seq_{seq}.bin", seq, start_time + sync_time - off_sync)
    else:
        receive_file(f"{output_dir}/seq_{seq}.bin", seq, start_time + sync_time // 2)
        send_file(file, seq, start_time + sync_time - off_sync)