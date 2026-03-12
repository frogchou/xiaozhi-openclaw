import subprocess
from .logger import logger
import struct
import random
import time

# Build Ogg CRC32 table once (non-reflected, poly 0x04C11DB7)
OGG_CRC_TABLE = [0] * 256
for i in range(256):
    r = i << 24
    for _ in range(8):
        if r & 0x80000000:
            r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
        else:
            r = (r << 1) & 0xFFFFFFFF
    OGG_CRC_TABLE[i] = r

class AudioConverter:
    @staticmethod
    def convert_opus_to_wav(opus_data: bytes) -> bytes:
        """
        使用 ffmpeg 将 Raw Opus 数据转换为 Wav 格式
        注意: 需要系统安装 ffmpeg
        """
        try:
            # 启动 ffmpeg 进程
            # -f opus: 输入格式为 opus (raw)
            # -i pipe:0: 从标准输入读取
            # -f wav: 输出格式为 wav
            process = subprocess.Popen(
                ['ffmpeg', '-y', '-f', 'opus', '-i', 'pipe:0', '-f', 'wav', 'pipe:1'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            wav_data, stderr = process.communicate(input=opus_data)
            
            if process.returncode != 0:
                logger.error(f"FFmpeg conversion failed: {stderr.decode()}")
                return None
                
            return wav_data
        except FileNotFoundError:
            logger.error("FFmpeg not found. Please install ffmpeg.")
            return None
        except Exception as e:
            logger.error(f"Audio conversion error: {e}")
            return None

    @staticmethod
    def build_ogg_opus_from_frames(frames: list[bytes], input_sample_rate: int, frame_duration_ms: int, channels: int = 1) -> bytes:
        if not frames:
            return b""

        def _ogg_crc(data: bytes) -> int:
            crc = 0
            for b in data:
                crc = ((crc << 8) & 0xFFFFFFFF) ^ OGG_CRC_TABLE[((crc >> 24) & 0xFF) ^ b]
            return crc & 0xFFFFFFFF

        def _ogg_page(header_type: int, granule_pos: int, serial: int, seq: int, segments: list[bytes]) -> bytes:
            segment_table = bytearray()
            body = bytearray()
            for seg in segments:
                size = len(seg)
                # lacing: 255-chunks followed by remainder
                full = size // 255
                rem = size % 255
                for _ in range(full):
                    segment_table.append(255)
                # Always append remainder as terminator (<255); when rem == 0, use 0 to terminate packet
                segment_table.append(rem)
                body.extend(seg)

            page_header = bytearray()
            page_header.extend(b"OggS")                # capture pattern
            page_header.extend(struct.pack("B", 0))    # stream structure version
            page_header.extend(struct.pack("B", header_type))  # header type
            page_header.extend(struct.pack("<q", granule_pos)) # granule position (little-endian signed 64)
            page_header.extend(struct.pack("<I", serial))      # bitstream serial number
            page_header.extend(struct.pack("<I", seq))         # page sequence no
            page_header.extend(struct.pack("<I", 0))           # checksum placeholder
            page_header.extend(struct.pack("B", len(segment_table)))  # page_segments
            page = page_header + segment_table + body
            # compute ogg crc (checksum field zeroed)
            page[22:26] = b"\x00\x00\x00\x00"
            crc = _ogg_crc(bytes(page))
            page[22:26] = struct.pack("<I", crc)
            return bytes(page)

        # OpusHead (per RFC 7845)
        # sample rate in OpusHead is 48000 always
        opus_head = bytearray()
        opus_head.extend(b"OpusHead")
        opus_head.extend(struct.pack("B", 1))                 # version
        opus_head.extend(struct.pack("B", channels))          # channel count
        opus_head.extend(struct.pack("<H", 0))                # pre-skip
        opus_head.extend(struct.pack("<I", 48000))            # input sample rate (must be 48000)
        opus_head.extend(struct.pack("<H", 0))                # output gain
        opus_head.extend(struct.pack("B", 0))                 # channel mapping (0 = single stream)

        # OpusTags (vendor only)
        vendor = b"xiaozhi-server"
        opus_tags = bytearray()
        opus_tags.extend(b"OpusTags")
        opus_tags.extend(struct.pack("<I", len(vendor)))
        opus_tags.extend(vendor)
        opus_tags.extend(struct.pack("<I", 0))  # user_comment_list_length

        serial = random.randint(1, 2**31 - 1)
        seq = 0
        pages = []
        t0 = time.perf_counter()
        # BOS page
        pages.append(_ogg_page(0x02, 0, serial, seq, [bytes(opus_head)]))
        seq += 1
        # Tags page
        pages.append(_ogg_page(0x00, 0, serial, seq, [bytes(opus_tags)]))
        seq += 1

        # Audio data pages
        # granule pos advanced in 48000-rate units
        samples_per_frame_48k = int(48000 * frame_duration_ms / 1000)
        granule_pos = 0
        # build packets; ensure we do not exceed large page size: we can put multiple packets per page
        current_segments = []
        total_segments_len = 0
        for pkt in frames:
            # segment sizes used in Ogg lacing; but we add entire packet as one segment list entry
            current_segments.append(pkt)
            total_segments_len += len(pkt)
            granule_pos += samples_per_frame_48k
            # flush page if too big
            if len(current_segments) >= 50 or total_segments_len >= 4096:
                pages.append(_ogg_page(0x00, granule_pos, serial, seq, current_segments))
                seq += 1
                current_segments = []
                total_segments_len = 0

        if current_segments:
            pages.append(_ogg_page(0x04, granule_pos, serial, seq, current_segments))  # EOS
        else:
            # add empty EOS page if needed
            pages.append(_ogg_page(0x04, granule_pos, serial, seq, []))

        out = b"".join(pages)
        t1 = time.perf_counter()
        logger.info(f"Ogg build pages={len(pages)} bytes={len(out)} cost={(t1-t0):.3f}s")
        return out

    @staticmethod
    def convert_opus_frames_to_wav(frames: list[bytes], input_sample_rate: int, frame_duration_ms: int) -> bytes:
        ogg_data = AudioConverter.build_ogg_opus_from_frames(frames, input_sample_rate, frame_duration_ms)
        if not ogg_data:
            return None
        try:
            process = subprocess.Popen(
                ['ffmpeg', '-y', '-f', 'ogg', '-i', 'pipe:0', '-f', 'wav', 'pipe:1'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            wav_data, stderr = process.communicate(input=ogg_data)
            if process.returncode != 0:
                logger.error(f"FFmpeg Ogg->Wav failed: {stderr.decode(errors='ignore')}")
                return None
            return wav_data
        except FileNotFoundError:
            logger.error("FFmpeg not found. Please install ffmpeg.")
            return None
        except Exception as e:
            logger.error(f"Audio conversion error: {e}")
            return None

    @staticmethod
    def convert_wav_to_ogg_opus(wav_data: bytes, sample_rate: int, frame_duration_ms: int) -> bytes:
        try:
            process = subprocess.Popen(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "wav",
                    "-i",
                    "pipe:0",
                    "-ac",
                    "1",
                    "-ar",
                    str(int(sample_rate)),
                    "-c:a",
                    "libopus",
                    "-application",
                    "voip",
                    "-frame_duration",
                    str(int(frame_duration_ms)),
                    "-f",
                    "ogg",
                    "pipe:1",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            ogg_data, stderr = process.communicate(input=wav_data)
            if process.returncode != 0:
                logger.error(f"FFmpeg opus encode failed: {stderr.decode(errors='ignore')}")
                return None

            return ogg_data
        except FileNotFoundError:
            logger.error("FFmpeg not found. Please install ffmpeg.")
            return None
        except Exception as e:
            logger.error(f"Audio conversion error: {e}")
            return None

    @staticmethod
    def extract_ogg_packets(ogg_data: bytes) -> list[bytes]:
        packets: list[bytes] = []
        current = bytearray()
        offset = 0
        data_len = len(ogg_data)

        while offset + 27 <= data_len:
            if ogg_data[offset : offset + 4] != b"OggS":
                break

            page_segments = ogg_data[offset + 26]
            segment_table_start = offset + 27
            segment_table_end = segment_table_start + page_segments
            if segment_table_end > data_len:
                break

            segment_table = ogg_data[segment_table_start:segment_table_end]
            body_start = segment_table_end
            body_size = 0
            for seg_len in segment_table:
                body_size += seg_len
            body_end = body_start + body_size
            if body_end > data_len:
                break

            body = ogg_data[body_start:body_end]
            body_offset = 0
            for seg_len in segment_table:
                if seg_len:
                    current.extend(body[body_offset : body_offset + seg_len])
                body_offset += seg_len
                if seg_len < 255:
                    packets.append(bytes(current))
                    current.clear()

            offset = body_end

        return packets
