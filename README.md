# PROTO CONFIG
4-byte magic number
MAGIC_BYTES = b'\xFF\xC0\xFF\xEE'
MAX_PAYLOAD = 150  # bytes

Header: type(1), seq(1), nonce(2), len(2), checksum(4),

MAGIC HEADER PAYLOAD
  4  +  10  +   N
HEADER_FMT = '>BBHHI'
HEADER_SIZE = struct.calcsize(HEADER_FMT)
CHKSUM_SIZE = 4
WINDOWS_SIZE = 1


## type

0: DATA
1: ACK


