import struct
import time
import zlib
import logging
import serial
import random
import hashlib
# PROTO CONFIG
# 4-byte magic number
MAGIC_BYTES = b'\xFF\xC0\xFF\xEE'
MAX_PAYLOAD = 150  # bytes
# Header: checksum(4), seq(1), len(2)
# Header: type(1), seq(1), nonce(2), len(2), checksum(4),

# MAGIC HEADER PAYLOAD
# 4 +     10 + N
# HEADER_FMT = '>IBH'
HEADER_FMT = '>BBHHI'
HEADER_SIZE = struct.calcsize(HEADER_FMT)
CHKSUM_SIZE = 4
WINDOWS_SIZE = 1
# INFRA

###  PROTO
#  cmdid    (2B)   CMD (3B)                 -> CMD
#  userhash (2B)   ":" (1B)  payload (nB)   -> packet  
###

class Magic_serial:
    
    def __init__(
        self,
        port,
        username = "anonymous",
        bitrate=9600,
        timeout=1,
        logger_name='my_logger',
        logger_level=logging.DEBUG,
    ):
        self.ser = serial.Serial(port, bitrate, timeout=timeout)
        
        logger = logging.getLogger(logger_name)
        logger.propagate = False
        logger.setLevel(logger_level)
        self.logger = logger
        self.message_seq_num = 0  # Stop-and-Wait seq
        
        self.has_received_hash = set()
        self.username = username

    # atomatic build op
    def build_frame(self, payload: bytes, seq: int, type: int = 0):
        length = len(payload)
        nonce = random.randint(0, 0xFFFF)
        # first set checksum to 0
        header = struct.pack(HEADER_FMT, type, seq, nonce, length, 0)
        chksum = zlib.crc32(header + payload)
        header = struct.pack(HEADER_FMT, type, seq, nonce, length, chksum)
        payload = MAGIC_BYTES + header + payload
        return payload

    # atomatic send op
    def send_frame(self, payload: bytes, seq: int, type: int = 0):
        length = len(payload)
        frame = self.build_frame(payload, seq, type)
        self.ser.write(frame)
        print(f"write to serial: {frame}")
        self.logger.debug(f"write to serial: {frame}")
        return len(frame)

    # atomatic recv op
    def read_exact(self, size: int, deadline: int) -> bytes:
        buf = bytearray()
        start_time = time.time()
        while len(buf) < size and (time.time() - start_time) < deadline:
            chunk= self.ser.read(size - len(buf))
            self.logger.debug(f"read from serial: {chunk}")
            if not chunk:
                continue
            buf.extend(chunk)
        return bytes(buf)

    def receive_frame(self, deadline=10):
        self.logger.info(f"enter receiving mode")
        data = bytearray()
        magic = bytearray()
        start_time = time.time()
        frame_hash = None
        while (time.time() - start_time) < deadline:
            # ensure magic exists at front first
            magic.extend(self.read_exact(len(MAGIC_BYTES) - len(magic), deadline))
            if len(magic) < len(MAGIC_BYTES):
                continue
            if magic != MAGIC_BYTES:
                magic.pop(0)
                continue
            
            header = self.read_exact(HEADER_SIZE, deadline)
            if len(header) < HEADER_SIZE:
                continue
            type, seq, nonce, length, chksum= struct.unpack(HEADER_FMT, header)

            payload = self.read_exact(length, deadline)
            if len(payload) < length:
                continue
            if chksum != zlib.crc32(header[:-CHKSUM_SIZE] + b'\0'*CHKSUM_SIZE + payload):
                self.logger.warning("incorrect checksum")
                continue
            frame_hash = hashlib.md5(header + payload).hexdigest()
            self.logger.debug(f"receive from serial: {payload}")
            return payload, type, seq, length, frame_hash
        return None, None, None, None, frame_hash
    
    def send_frame_with_ack(self, payload: bytes, retries=2, timeout=2):
        for _ in range(retries):
            self.send_frame(payload, self.message_seq_num)
            start = time.time()
            while (time.time() - start) < timeout:
                _, rtype, rseq, _, _ = self.receive_frame(deadline=timeout)
                if rtype == b"\x01" and rseq == self.message_seq_num:
                    self.logger.info("ACK received")
                    
                    self.message_seq_num = (self.message_seq_num + 1 ) % 256
                    return True
            # 超时重试
        self.logger.warning("send failed after retries")
        return False


    def frame_dispatcher(self, payload, type, rseq, length, frame_hash, ack_retry:int=1):
        
        if frame_hash == None:
            return None
        
        if type == b"\x01": 
            # repated ACK
            return None
        else: 
            # if last_message == payload: # TODO
            #     print("[-] repeat Message")
            # else:
            # send ACK back
            for _ in range(ack_retry):  # repeat N times , but OT is enough in practice
                self.send_frame(payload=b'', type=1, seq=rseq)
                time.sleep(0.1)
            if frame_hash in self.has_received_hash:
                self.logger.info("duplicate message received, ignored")
                return None                
            return payload
    
    # last_message =  ""
    # def frame_dispatcher(seq, payload, ack_retry:int=1,mode="unlisten"):
    #     if payload is None:
    #         return None
    #     elif payload == b"\x01\x01ACK":      # repeated ACK
    #         return None
    #     elif payload[:10] == b"\x03\x01DISCOVER": # discover others 
    #         # TODO
    #         if mode == "listen":
    #             return None
    #         send_frame(f"\x03\x02{username}".encode())
    #     elif payload[:2] == b'\x03\x02':     # recieve username 
    #         user_list.append(payload[2:].decode())
    #         print_log(user_list)
    #     elif payload[:2] == b'\x02\x01': # handshake request
    #         if mode == "listen":
    #             return None
    #         parts = payload.decode().split(":")
    #         if len(parts) != 3:
    #             print_failed("Invalid handshake request")
    #             return None
    #         msg_command, file_sender, file_receiver = parts
    #         if msg_command != "\x02\x01REQ":
    #             print_failed("Invalid handshake command")
    #             return None
    #         if file_receiver != username:
    #             print_failed("Handshake request not for us")
    #             return None
    #         print_log(f"Handshake request from {file_sender}")
    #         # send ACK back
    #         time.sleep(1) # add lag by hand
    #         send_frame(f"\x02\x02ACK:{file_sender}:{username}".encode(), seq)
    #         print_log("send ACK back")
    #         return f"\x02{file_sender}".encode()
    #     else:                                # normal message
    #         # if last_message == payload: # TODO
    #         #     print("[-] repeat Message")
    #         # else:
    #         print_log("[+] data received:", payload)
    #         print_commu(payload.decode())
    #         # send ACK back
    #         for _ in range(ack_retry):  # repeat N times , but OT is enough in practice
    #             send_frame(b"\x01\x01ACK", seq)
    #             time.sleep(0.1)
    #         last_message = payload
    #         return payload
    #     return None

    
    

    # def print_log(message):
    #     print(f"[LOG] {message}")

    # def print_success(message):
    #     print(f"[+] {message}")

    # def print_failed(message):
    #     print(f"[-] {message}")

