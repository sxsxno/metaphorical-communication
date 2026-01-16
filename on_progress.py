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
from infra import *
from UI import *

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

# TODO
# class core:
#     def __init__(self):
#         self.s
#     def send():
        

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
                
                message_seq_num += 1  # 切换 seq
                message_seq_num %= 256
                return True
        # 超时重试
    logger.warning("send failed after retries")
    return False

last_message =  ""
def frame_dispatcher(seq, payload, ack_retry:int=1,mode="unlisten"):
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
work_queue = []

def background_worker():
    while True:
        if len(work_queue) == 0 or ser.in_waiting > 0:
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
        task = work_queue.pop(0)
        # print(task)
        # print(f"[后台进程] 处理任务: {task}")
        if task[0] == "message":
            res = send_frame_with_ack(payload=task[1].encode(),seq=message_seq_num,timeout=2)
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
            run_discover() # TODO
    
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
        print_log(f"message send task added: {cmd[8:]}")
    if cmd.startswith("sendfile"):
        if len(cmd) < 10:
            print_log("Usage: sendfile filename")
        cmd_content = cmd[9:]
        cmd_content = cmd_content.split(" ")
        work_queue.append(("sendfile",cmd_content[0],cmd_content[1]))
        print_log(f"message send task added: {cmd_content[0],cmd_content[1]}")
kb_enter_handler = cmd_dispatcher
#  define keyboard enter handler

def main():
    global ser
    # no global declaration needed here.argv[1]
    ser = serial.Serial(port, 9600, timeout=1)
    init_serial(ser)
    init_logger(logger)
    
    app.run()
    
    p = Thread(target=background_worker, daemon=True)
    p.start()
    # foreground_shell()

# TODO add limit about MAXPAYLOAD
main()