import struct
import json
from dataclasses import dataclass
from typing import Optional, Union, Dict, Any

@dataclass
class AudioPacket:
    payload: bytes
    timestamp: int = 0
    seq: int = 0

class ProtocolHandler:
    """处理 WebSocket 协议的打包与解包"""
    
    def __init__(self, version: int = 1):
        self.version = version

    def decode_binary(self, data: bytes) -> Optional[AudioPacket]:
        """根据协议版本解析二进制音频包"""
        if self.version == 1:
            # Version 1: 纯 Opus 数据
            return AudioPacket(payload=data)
            
        elif self.version == 2:
            # Version 2: Header(16B) + Payload
            # struct BinaryProtocol2 {
            #     uint16_t version; uint16_t type; uint32_t reserved;
            #     uint32_t timestamp; uint32_t payload_size;
            # }
            if len(data) < 16:
                return None
            header = struct.unpack('!HHIII', data[:16])
            payload_size = header[4]
            timestamp = header[3]
            return AudioPacket(payload=data[16:16+payload_size], timestamp=timestamp)
            
        elif self.version == 3:
            # Version 3: Header(4B) + Payload
            # struct BinaryProtocol3 {
            #     uint8_t type; uint8_t reserved; uint16_t payload_size;
            # }
            if len(data) < 4:
                return None
            header = struct.unpack('!BBH', data[:4])
            payload_size = header[2]
            return AudioPacket(payload=data[4:4+payload_size])
            
        return None

    def encode_binary(self, payload: bytes, timestamp: int = 0) -> bytes:
        if self.version == 1:
            return payload

        if self.version == 2:
            header = struct.pack(
                "!HHIII",
                int(self.version) & 0xFFFF,
                0,
                0,
                int(timestamp) & 0xFFFFFFFF,
                len(payload) & 0xFFFFFFFF,
            )
            return header + payload

        if self.version == 3:
            header = struct.pack("!BBH", 0, 0, len(payload) & 0xFFFF)
            return header + payload

        return payload

    def encode_text(self, data: Dict[str, Any]) -> str:
        return json.dumps(data)

    def decode_text(self, text: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
