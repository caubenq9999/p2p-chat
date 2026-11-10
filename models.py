"""Shared Sprint 1 data contracts."""
from dataclasses import dataclass


@dataclass
class Peer:
    peer_id: str
    name: str
    ip: str
    tcp_port: int
    last_seen: float


@dataclass
class Event:
    type: str
    peer_id: str
    data: dict
