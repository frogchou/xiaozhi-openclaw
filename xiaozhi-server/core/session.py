import asyncio
import uuid
from contextlib import suppress
from typing import Awaitable, Callable, Optional

from websockets.exceptions import ConnectionClosed

from config import Config
from services.asr import AsrService, LlmService, TtsService
from utils.logger import logger

from .protocol import ProtocolHandler


class Session:
    PREBUFFER_PACKETS = 5
    HARD_STOP_SECONDS = 8.0
    PLAYBACK_TAIL_PACKETS = PREBUFFER_PACKETS + 2
    BARGE_IN_GRACE_SECONDS = 0.8

    def __init__(
        self,
        websocket,
        device_id: str,
        version: int = 1,
        client_id: str = "unknown_client",
    ):
        self.id = str(uuid.uuid4())
        self.websocket = websocket
        self.device_id = device_id
        self.client_id = client_id
        self.protocol = ProtocolHandler(version)
        self.asr_service = AsrService()
        self.llm_service = LlmService()
        self.tts_service = TtsService()

        self.audio_frames: list[bytes] = []
        self.is_listening = False
        self.is_processing = False
        self.is_speaking = False
        self.listening_mode = "auto"

        self._auto_stop_task: Optional[asyncio.Task] = None
        self._hard_stop_task: Optional[asyncio.Task] = None
        self._processing_task: Optional[asyncio.Task] = None
        self._last_audio_time = 0.0
        self._frames_counter = 0
        self._state_lock = asyncio.Lock()
        self._turn_seq = 0
        self._active_turn_id: Optional[int] = None
        self._speaking_turn_id: Optional[int] = None
        self._last_stopped_turn_id: Optional[int] = None
        self._speak_grace_until = 0.0

        self._text_handlers: dict[str, Callable[[dict], Awaitable[None]]] = {
            "hello": self._handle_hello,
            "listen": self._handle_listen,
            "abort": self._handle_abort,
            "reload_config": self._handle_reload_config,
        }
        self._server_reload_config: Optional[Callable[[], Awaitable[None]]] = None

    def bind_server_hooks(
        self, reload_config: Optional[Callable[[], Awaitable[None]]] = None
    ) -> None:
        self._server_reload_config = reload_config

    async def send_json(self, data: dict) -> None:
        await self.websocket.send(self.protocol.encode_text(data))

    async def send_audio(self, payload: bytes, timestamp: int = 0) -> None:
        await self.websocket.send(
            self.protocol.encode_binary(payload, timestamp=timestamp)
        )

    async def run(self) -> None:
        try:
            async for message in self.websocket:
                if isinstance(message, str):
                    await self.handle_text(message)
                elif isinstance(message, (bytes, bytearray, memoryview)):
                    await self.handle_audio(bytes(message))
        except ConnectionClosed:
            logger.info(f"Connection closed: {self.device_id}/{self.client_id}")
        except Exception:
            logger.error(f"[{self.device_id}] Connection loop error", exc_info=True)
        finally:
            with suppress(Exception):
                await self.abort()

    async def handle_text(self, message: str) -> None:
        data = self.protocol.decode_text(message)
        if not data or not isinstance(data, dict):
            return

        msg_type = data.get("type")
        if not isinstance(msg_type, str):
            return

        handler = self._text_handlers.get(msg_type)
        if handler is None:
            logger.debug(f"[{self.device_id}] Ignore unknown message type: {msg_type}")
            return

        await handler(data)

    async def handle_audio(self, data: bytes) -> None:
        packet = self.protocol.decode_binary(data)
        if packet is None:
            return

        if not packet.payload:
            await self._handle_audio_end_signal()
            return

        if await self._should_barge_in():
            logger.info(f"[{self.device_id}] Interrupt assistant playback by user voice")
            await self._abort_current_turn("barge-in")

        if await self._should_ignore_audio_while_speaking():
            return

        await self._ensure_capture_started("incoming_audio")

        async with self._state_lock:
            if not self.is_listening:
                return

            self.audio_frames.append(packet.payload)
            self._last_audio_time = asyncio.get_running_loop().time()
            self._frames_counter += 1

            if (self._frames_counter % 50) == 0:
                logger.info(
                    f"[{self.device_id}] Received audio frames: {self._frames_counter}"
                )

    async def _handle_hello(self, data: dict) -> None:
        response = {
            "type": "hello",
            "transport": "websocket",
            "session_id": self.id,
            "audio_params": {
                "format": "opus",
                "sample_rate": Config.AUDIO_SAMPLE_RATE,
                "frame_duration": Config.AUDIO_FRAME_DURATION,
                "channels": Config.AUDIO_CHANNELS,
            },
        }
        await self.send_json(response)
        logger.info(f"[{self.device_id}] Handshake successful")

    async def _handle_listen(self, data: dict) -> None:
        state = data.get("state")
        mode = data.get("mode")
        self.set_listening_mode(str(mode) if mode else self.listening_mode)

        if state == "start":
            await self.start_listening("client_start")
            return

        if state == "stop":
            await self.stop_listening("client_stop")
            return

        if state == "detect":
            logger.info(f"[{self.device_id}] Wake word detected: {data.get('text')}")

    async def _handle_abort(self, data: dict) -> None:
        logger.info(f"[{self.device_id}] Abort current session")
        await self.abort()

    async def _handle_reload_config(self, data: dict) -> None:
        if not Config.ENABLE_RELOAD_CONFIG or not self._server_reload_config:
            return

        await self._server_reload_config()
        await self.reload_services()
        await self.send_json(
            {"type": "reload_config", "session_id": self.id, "state": "ok"}
        )

    async def start_listening(self, source: str = "client") -> None:
        await self._abort_current_turn(f"restart:{source}", send_stop=True)

        async with self._state_lock:
            self._begin_listening_locked(reset_buffer=True)

        logger.info(
            f"[{self.device_id}] Start listening mode={self.listening_mode} source={source}"
        )

    async def stop_listening(self, source: str = "client") -> None:
        async with self._state_lock:
            if not self.is_listening and not self.audio_frames:
                return

            self.is_listening = False
            self._cancel_watchdogs_locked()

            frames = self.audio_frames[:]
            self.audio_frames.clear()

            if not frames:
                logger.info(
                    f"[{self.device_id}] Stop listening with empty buffer source={source}"
                )
                return

            self._turn_seq += 1
            turn_id = self._turn_seq
            self._active_turn_id = turn_id
            self._speaking_turn_id = None
            self._last_stopped_turn_id = None
            self.is_processing = True
            self._processing_task = asyncio.create_task(
                self._process_turn(turn_id, frames),
                name=f"session-turn-{self.device_id}-{turn_id}",
            )

        total_bytes = sum(len(frame) for frame in frames)
        logger.info(
            f"[{self.device_id}] Stop listening source={source} frames={len(frames)} bytes={total_bytes}"
        )

    def set_listening_mode(self, mode: str) -> None:
        if mode in ("auto", "manual", "realtime"):
            self.listening_mode = mode
        else:
            self.listening_mode = "auto"

    async def _ensure_capture_started(self, source: str) -> None:
        async with self._state_lock:
            if self.is_listening or self.is_processing or self.is_speaking:
                return

            self._begin_listening_locked(reset_buffer=False)
            logger.info(
                f"[{self.device_id}] Start implicit listening mode={self.listening_mode} source={source}"
            )

    def _begin_listening_locked(self, reset_buffer: bool) -> None:
        self.is_listening = True
        if reset_buffer:
            self.audio_frames.clear()
        self._frames_counter = 0
        self._last_audio_time = asyncio.get_running_loop().time()
        self._cancel_watchdogs_locked()
        self._auto_stop_task = asyncio.create_task(self._auto_stop_watchdog())
        self._hard_stop_task = asyncio.create_task(
            self._hard_stop_after(self.HARD_STOP_SECONDS)
        )

    def _cancel_watchdogs_locked(self) -> None:
        for task in (self._auto_stop_task, self._hard_stop_task):
            if task and not task.done():
                task.cancel()
        self._auto_stop_task = None
        self._hard_stop_task = None

    async def _handle_audio_end_signal(self) -> None:
        async with self._state_lock:
            should_stop = self.is_listening and bool(self.audio_frames)

        if should_stop:
            logger.info(f"[{self.device_id}] Received empty audio frame, stopping capture")
            await self.stop_listening("empty_frame")

    async def _auto_stop_watchdog(self) -> None:
        idle_timeout = max(0.8, Config.AUDIO_FRAME_DURATION / 1000 * 8)
        try:
            while True:
                await asyncio.sleep(0.2)
                async with self._state_lock:
                    if not self.is_listening:
                        return
                    idle_seconds = (
                        asyncio.get_running_loop().time() - self._last_audio_time
                    )
                    should_stop = idle_seconds >= idle_timeout and bool(self.audio_frames)

                if should_stop:
                    await self.stop_listening("auto_silence")
                    return
        except asyncio.CancelledError:
            return

    async def _hard_stop_after(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            async with self._state_lock:
                should_stop = self.is_listening and bool(self.audio_frames)

            if should_stop:
                await self.stop_listening("hard_timeout")
        except asyncio.CancelledError:
            return

    async def _process_turn(self, turn_id: int, frames: list[bytes]) -> None:
        speech_started = False

        try:
            logger.info(f"[{self.device_id}] ASR start turn={turn_id} frames={len(frames)}")
            user_text = await self.asr_service.transcribe_frames(
                frames,
                input_sample_rate=16000,
                frame_duration_ms=Config.AUDIO_FRAME_DURATION,
            )
            if not await self._is_turn_active(turn_id):
                return

            if user_text:
                await self.send_json(
                    {"type": "stt", "session_id": self.id, "text": user_text}
                )
                logger.info(
                    f"[{self.device_id}] LLM start turn={turn_id} input_len={len(user_text)}"
                )
                assistant_text = await self.llm_service.chat(
                    user_text,
                    from_id=self.device_id,
                    conversation_id=self.id,
                    sender_name=self.client_id,
                )
            else:
                assistant_text = "我没听清，请再说一遍。"

            if not await self._is_turn_active(turn_id):
                return

            if not assistant_text:
                assistant_text = "抱歉，我现在有点忙，稍后再试试。"

            await self.send_json(
                {"type": "llm", "session_id": self.id, "emotion": "neutral", "text": assistant_text}
            )

            packets = await self.tts_service.synthesize_opus_packets(
                assistant_text,
                sample_rate=Config.AUDIO_SAMPLE_RATE,
                frame_duration_ms=Config.AUDIO_FRAME_DURATION,
            )
            if not await self._is_turn_active(turn_id):
                return

            logger.info(
                f"[{self.device_id}] TTS ready turn={turn_id} packets={len(packets)}"
            )
            if packets:
                await self._mark_speaking(turn_id)
                speech_started = True
                await self.send_json(
                    {"type": "tts", "session_id": self.id, "state": "start"}
                )
                await self.send_json(
                    {
                        "type": "tts",
                        "session_id": self.id,
                        "state": "sentence_start",
                        "text": assistant_text,
                    }
                )
                await self._stream_tts_audio(turn_id, packets)
            await self._wait_for_client_playback(turn_id, len(packets))
        except asyncio.CancelledError:
            logger.info(f"[{self.device_id}] Turn cancelled turn={turn_id}")
            raise
        except Exception:
            logger.error(f"[{self.device_id}] Turn processing error", exc_info=True)
        finally:
            await self._finish_turn(turn_id, speech_started)

    async def _stream_tts_audio(self, turn_id: int, packets: list[bytes]) -> None:
        if not packets:
            return

        frame_seconds = max(Config.AUDIO_FRAME_DURATION, 20) / 1000.0
        for index, packet in enumerate(packets):
            if not await self._is_turn_active(turn_id):
                return

            await self.send_audio(
                packet, timestamp=index * Config.AUDIO_FRAME_DURATION
            )

            if index + 1 < len(packets) and index + 1 >= self.PREBUFFER_PACKETS:
                await asyncio.sleep(frame_seconds)

    async def _wait_for_client_playback(self, turn_id: int, packet_count: int) -> None:
        if packet_count <= 0:
            return

        frame_seconds = max(Config.AUDIO_FRAME_DURATION, 20) / 1000.0
        tail_packets = min(packet_count, self.PLAYBACK_TAIL_PACKETS)
        tail_seconds = tail_packets * frame_seconds + 0.15
        await asyncio.sleep(tail_seconds)
        if not await self._is_turn_active(turn_id):
            return

    async def _mark_speaking(self, turn_id: int) -> None:
        async with self._state_lock:
            if self._active_turn_id != turn_id:
                raise asyncio.CancelledError

            self.is_speaking = True
            self._speaking_turn_id = turn_id
            self._speak_grace_until = (
                asyncio.get_running_loop().time() + self.BARGE_IN_GRACE_SECONDS
            )

    async def _finish_turn(self, turn_id: int, speech_started: bool) -> None:
        should_send_stop = False
        current_task = asyncio.current_task()

        async with self._state_lock:
            if speech_started and self._last_stopped_turn_id != turn_id:
                should_send_stop = self._speaking_turn_id == turn_id

            if self._speaking_turn_id == turn_id:
                self._speaking_turn_id = None

            if self._active_turn_id == turn_id:
                self._active_turn_id = None

            if self._processing_task is current_task:
                self._processing_task = None

            self.is_processing = self._active_turn_id is not None
            self.is_speaking = self._speaking_turn_id is not None
            if not self.is_speaking:
                self._speak_grace_until = 0.0

        if should_send_stop:
            with suppress(Exception):
                await self.send_json(
                    {"type": "tts", "session_id": self.id, "state": "stop"}
                )

    async def _abort_current_turn(
        self,
        reason: str,
        send_stop: bool = True,
    ) -> None:
        task_to_cancel: Optional[asyncio.Task] = None
        speaking_turn_id: Optional[int] = None
        current_task = asyncio.current_task()

        async with self._state_lock:
            speaking_turn_id = self._speaking_turn_id
            processing_task = self._processing_task
            active_turn_id = self._active_turn_id

            if (
                processing_task
                and processing_task is not current_task
                and not processing_task.done()
            ):
                task_to_cancel = processing_task

            self.is_listening = False
            self.audio_frames.clear()
            self._cancel_watchdogs_locked()

            self.is_processing = False
            self.is_speaking = False
            self._active_turn_id = None
            self._speaking_turn_id = None
            self._speak_grace_until = 0.0

            if speaking_turn_id is not None:
                self._last_stopped_turn_id = speaking_turn_id
            elif active_turn_id is None:
                self._last_stopped_turn_id = None

            if self._processing_task is processing_task:
                self._processing_task = None

        if task_to_cancel:
            task_to_cancel.cancel()
            with suppress(asyncio.CancelledError):
                await task_to_cancel

        if send_stop and speaking_turn_id is not None:
            with suppress(Exception):
                await self.send_json(
                    {"type": "tts", "session_id": self.id, "state": "stop"}
                )

        if task_to_cancel or speaking_turn_id is not None:
            logger.info(f"[{self.device_id}] Abort turn reason={reason}")

    async def _should_barge_in(self) -> bool:
        async with self._state_lock:
            if not self.is_speaking or self.listening_mode == "manual":
                return False
            return asyncio.get_running_loop().time() >= self._speak_grace_until

    async def _should_ignore_audio_while_speaking(self) -> bool:
        async with self._state_lock:
            if not self.is_speaking:
                return False
            if self.listening_mode == "manual":
                return True
            return asyncio.get_running_loop().time() < self._speak_grace_until

    async def _is_turn_active(self, turn_id: int) -> bool:
        async with self._state_lock:
            return self._active_turn_id == turn_id

    async def abort(self) -> None:
        await self._abort_current_turn("session_abort", send_stop=True)

    async def reload_services(self) -> None:
        self.asr_service = AsrService()
        self.llm_service = LlmService()
        self.tts_service = TtsService()
