import asyncio
import io
import time
from typing import Optional

from openai import OpenAI

from config import Config
from services.openclaw_bridge import get_openclaw_bridge_service
from utils.audio_converter import AudioConverter
from utils.logger import logger


def _create_openai_client() -> Optional[OpenAI]:
    if not Config.OPENAI_API_KEY:
        return None
    return OpenAI(
        api_key=Config.OPENAI_API_KEY,
        base_url=Config.OPENAI_BASE_URL,
        timeout=Config.OPENAI_TIMEOUT_SECONDS,
        max_retries=0,
    )


class AsrService:
    def __init__(self):
        self.client = _create_openai_client()

    async def transcribe(self, audio_data: bytes) -> str:
        if not self.client:
            logger.warning("OpenAI API Key not set, skipping transcription")
            return "Server: OpenAI API Key missing"

        return ""

    async def transcribe_frames(
        self, frames: list[bytes], input_sample_rate: int, frame_duration_ms: int
    ) -> str:
        if not self.client:
            logger.warning("OpenAI API Key not set, skipping transcription")
            return "Server: OpenAI API Key missing"

        t0 = time.perf_counter()
        logger.info(
            f"ASR BuildOgg start frames={len(frames)} sr={input_sample_rate} fd={frame_duration_ms}"
        )
        ogg_data = await asyncio.to_thread(
            AudioConverter.build_ogg_opus_from_frames,
            frames,
            input_sample_rate,
            frame_duration_ms,
        )
        t1 = time.perf_counter()
        if ogg_data:
            logger.info(f"ASR BuildOgg done bytes={len(ogg_data)} cost={(t1 - t0):.3f}s")
        else:
            logger.error(f"ASR BuildOgg failed cost={(t1 - t0):.3f}s")
        if not ogg_data:
            return ""

        try:
            audio_file = io.BytesIO(ogg_data)
            audio_file.name = "audio.ogg"

            def _do_transcribe() -> str:
                transcript = self.client.audio.transcriptions.create(
                    model=Config.WHISPER_MODEL,
                    file=audio_file,
                )
                return transcript.text or ""

            t2 = time.perf_counter()
            logger.info(
                f"ASR Whisper start base_url={Config.OPENAI_BASE_URL} model={Config.WHISPER_MODEL}"
            )
            text = await asyncio.to_thread(_do_transcribe)
            t3 = time.perf_counter()
            if text:
                logger.info(f"ASR Whisper done len={len(text)} cost={(t3 - t2):.3f}s")
                logger.info(f"ASR Result: {text}")
            else:
                logger.info(f"ASR Whisper empty cost={(t3 - t2):.3f}s")
            return text
        except Exception:
            t3 = time.perf_counter()
            logger.error(f"ASR Whisper error cost={(t3 - t2):.3f}s", exc_info=True)
            return ""


class LlmService:
    def __init__(self):
        self.bridge_service = get_openclaw_bridge_service()

    async def chat(
        self,
        user_text: str,
        from_id: str,
        conversation_id: str,
        sender_name: Optional[str] = None,
    ) -> str:
        try:
            t0 = time.perf_counter()
            logger.info(
                f"LLM start via OpenClaw bridge input_len={len(user_text)}"
            )
            content = await self.bridge_service.request_reply(
                user_text=user_text,
                from_id=from_id,
                conversation_id=conversation_id,
                sender_name=sender_name,
            )
            t1 = time.perf_counter()
            logger.info(f"LLM done output_len={len(content)} cost={(t1 - t0):.3f}s")
            return content
        except Exception:
            t1 = time.perf_counter()
            logger.error(f"LLM error cost={(t1 - t0):.3f}s", exc_info=True)
            return ""


class TtsService:
    def __init__(self):
        self.client = _create_openai_client()

    async def synthesize_opus_packets(
        self, text: str, sample_rate: int, frame_duration_ms: int
    ) -> list[bytes]:
        if not self.client:
            logger.warning("OpenAI API Key not set, skipping TTS")
            return []

        if not text:
            return []

        try:
            def _request_tts() -> bytes:
                response = self.client.audio.speech.create(
                    model=Config.TTS_MODEL,
                    voice=Config.TTS_VOICE,
                    input=text,
                    response_format="wav",
                )
                if hasattr(response, "content"):
                    return response.content
                if hasattr(response, "read"):
                    return response.read()
                return bytes(response)

            t0 = time.perf_counter()
            logger.info(
                f"TTS start base_url={Config.OPENAI_BASE_URL} model={Config.TTS_MODEL} voice={Config.TTS_VOICE} text_len={len(text)}"
            )
            wav_data = await asyncio.to_thread(_request_tts)
            t1 = time.perf_counter()
            logger.info(f"TTS response wav bytes={len(wav_data)} cost={(t1 - t0):.3f}s")

            t2 = time.perf_counter()
            ogg_data = await asyncio.to_thread(
                AudioConverter.convert_wav_to_ogg_opus,
                wav_data,
                sample_rate,
                frame_duration_ms,
            )
            t3 = time.perf_counter()
            if not ogg_data:
                logger.error(f"FFmpeg Wav->OggOpus failed cost={(t3 - t2):.3f}s")
                return []
            logger.info(f"FFmpeg Wav->OggOpus done bytes={len(ogg_data)} cost={(t3 - t2):.3f}s")

            t4 = time.perf_counter()
            packets = await asyncio.to_thread(AudioConverter.extract_ogg_packets, ogg_data)
            t5 = time.perf_counter()
            if not packets:
                logger.error(f"Ogg extract packets failed cost={(t5 - t4):.3f}s")
                return []
            if len(packets) <= 2:
                logger.error(f"Ogg extract packets too few={len(packets)} cost={(t5 - t4):.3f}s")
                return []
            logger.info(f"Ogg extract packets done total={len(packets)} cost={(t5 - t4):.3f}s")
            return packets[2:]
        except Exception:
            t1 = time.perf_counter()
            logger.error(f"TTS error cost={(t1 - t0):.3f}s", exc_info=True)
            return []
