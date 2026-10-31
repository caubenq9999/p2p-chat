"""Length-prefixed JSON packets over raw TCP."""
import json
import struct
import uuid

MAX_PAYLOAD = 1024 * 1024
PACKET_TYPES = frozenset({
    "HELLO", "CHAT", "FILE_OFFER", "FILE_ACCEPT", "FILE_REJECT",
    "FILE_CHUNK", "FILE_END", "FILE_RESULT", "ERROR", "PING", "PONG",
})


class ProtocolError(ValueError):
    pass


def make_packet(packet_type, sender_id, payload):
    return {"v": 1, "type": packet_type, "id": uuid.uuid4().hex,
            "sender_id": sender_id, "payload": payload}


def validate_packet(packet):
    if not isinstance(packet, dict):
        raise ProtocolError("Packet must be an object")
    if type(packet.get("v")) is not int or packet["v"] != 1:
        raise ProtocolError("Unsupported protocol version")
    if not isinstance(packet.get("type"), str) or packet["type"] not in PACKET_TYPES:
        raise ProtocolError("Unknown packet type")
    for field in ("id", "sender_id"):
        if not isinstance(packet.get(field), str) or not packet[field]:
            raise ProtocolError(f"Invalid {field}")
    payload = packet.get("payload")
    if not isinstance(payload, dict):
        raise ProtocolError("Payload must be an object")
    if packet["type"] == "CHAT" and not isinstance(payload.get("text"), str):
        raise ProtocolError("CHAT requires text")
    if packet["type"] == "HELLO":
        if payload.get("peer_id") != packet["sender_id"]:
            raise ProtocolError("HELLO identity mismatch")
        if not isinstance(payload.get("name"), str):
            raise ProtocolError("HELLO requires name")
    return packet


def encode_packet(packet):
    validate_packet(packet)
    try:
        body = json.dumps(packet, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Packet is not valid JSON") from exc
    if not 0 < len(body) <= MAX_PAYLOAD:
        raise ProtocolError("Payload exceeds frame limit")
    return struct.pack("!I", len(body)) + body


def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("Connection closed before next complete frame")
        data.extend(chunk)
    return bytes(data)


def recv_packet(sock):
    size = struct.unpack("!I", recv_exact(sock, 4))[0]
    if not 0 < size <= MAX_PAYLOAD:
        raise ProtocolError("Invalid frame length")
    try:
        packet = json.loads(recv_exact(sock, size).decode("utf-8"),
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid JSON payload") from exc
    return validate_packet(packet)
