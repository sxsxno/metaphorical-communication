import struct
import time
import zlib
import logging
# PROTO CONFIG
# 4-byte magic number
MAGIC_BYTES = b'\xFF\xC0\xFF\xEE'
MAX_PAYLOAD = 150  # bytes
# Header: checksum(4), seq(1), len(2)
HEADER_FMT = '>IBH'
HEADER_SIZE = struct.calcsize(HEADER_FMT)
CHKSUM_SIZE = 4
WINDOWS_SIZE = 1
# INFRA

###  PROTO
#  cmdid    (2B)   CMD (3B)                 -> CMD
#  userhash (2B)   ":" (1B)  payload (nB)   -> packet  
###

### init
ser = None
logger = None
def init_serial(serial_obj):
    global ser
    ser = serial_obj

def init_logger(logger_obj):
    global logger
    logger = logger_obj

# atomatic build op
def build_frame(payload: bytes, seq: int):
    global ser
    global logger
    length = len(payload)
    # first set checksum to 0
    header = struct.pack(HEADER_FMT, 0, seq, length)
    chksum = zlib.crc32(header + payload)
    header = struct.pack(HEADER_FMT, chksum, seq, length)
    payload = MAGIC_BYTES + header + payload
    return payload

# atomatic send op
def send_frame(payload: bytes, seq: int):
    global ser
    global logger
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
    global logger
    buf = bytearray()
    start_time = time.time()
    while len(buf) < size and (time.time() - start_time) < deadline:
        chunk = ser.read(size - len(buf))
        logger.debug(f"read from serial: {chunk}")
        if not chunk:
            continue
        buf.extend(chunk)
    return bytes(buf)

def receive_frame(deadline):
    global logger
    logging.info(f"enter receiving mode")
    data = bytearray()
    magic = bytearray()
    start_time = time.time()
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
        return remote_seq, payload
    return None, None

# def print_log(message):
#     print(f"[LOG] {message}")

# def print_success(message):
#     print(f"[+] {message}")

# def print_failed(message):
#     print(f"[-] {message}")

