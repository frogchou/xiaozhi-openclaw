import os

from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


class Config:
    @classmethod
    def reload(cls) -> None:
        load_dotenv(override=True)
        cls.HOST = os.getenv("HOST", "0.0.0.0")
        cls.PORT = int(os.getenv("PORT", 8080))

        cls.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        cls.OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
        cls.WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")

        cls.LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
        cls.LLM_SYSTEM_PROMPT = os.getenv(
            "LLM_SYSTEM_PROMPT",
            "你是一个语音助手。请用简洁、口语化的中文回答用户的问题。",
        )

        cls.OPENCLAW_BRIDGE_PATH = os.getenv("OPENCLAW_BRIDGE_PATH", "/openclaw-bridge")
        cls.OPENCLAW_BRIDGE_TOKEN = os.getenv("OPENCLAW_BRIDGE_TOKEN", "")
        cls.OPENCLAW_BRIDGE_REQUEST_TIMEOUT_SECONDS = float(
            os.getenv("OPENCLAW_BRIDGE_REQUEST_TIMEOUT_SECONDS", 120)
        )

        cls.TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
        cls.TTS_VOICE = os.getenv("TTS_VOICE", "alloy")

        cls.OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", 10))

        cls.AUDIO_SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", 16000))
        cls.AUDIO_FRAME_DURATION = int(os.getenv("AUDIO_FRAME_DURATION", 60))
        cls.AUDIO_CHANNELS = int(os.getenv("AUDIO_CHANNELS", 1))

        cls.ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")

        cls.WS_PING_INTERVAL = float(os.getenv("WS_PING_INTERVAL", 20))
        cls.WS_PING_TIMEOUT = float(os.getenv("WS_PING_TIMEOUT", 20))
        cls.WS_MAX_SIZE = int(os.getenv("WS_MAX_SIZE", 2 * 1024 * 1024))
        cls.WS_MAX_QUEUE = int(os.getenv("WS_MAX_QUEUE", 32))
        cls.ENABLE_RELOAD_CONFIG = os.getenv("ENABLE_RELOAD_CONFIG", "0") == "1"

    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", 8080))

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
    WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")

    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
    LLM_SYSTEM_PROMPT = os.getenv(
        "LLM_SYSTEM_PROMPT",
        "你是一个语音助手。请用简洁、口语化的中文回答用户的问题。",
    )

    OPENCLAW_BRIDGE_PATH = os.getenv("OPENCLAW_BRIDGE_PATH", "/openclaw-bridge")
    OPENCLAW_BRIDGE_TOKEN = os.getenv("OPENCLAW_BRIDGE_TOKEN", "")
    OPENCLAW_BRIDGE_REQUEST_TIMEOUT_SECONDS = float(
        os.getenv("OPENCLAW_BRIDGE_REQUEST_TIMEOUT_SECONDS", 120)
    )

    TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
    TTS_VOICE = os.getenv("TTS_VOICE", "alloy")

    OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", 10))

    # 音频参数 (服务端下发)
    AUDIO_SAMPLE_RATE = 16000
    AUDIO_FRAME_DURATION = 60
    AUDIO_CHANNELS = 1

    # 认证参数
    ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")

    WS_PING_INTERVAL = float(os.getenv("WS_PING_INTERVAL", 20))
    WS_PING_TIMEOUT = float(os.getenv("WS_PING_TIMEOUT", 20))
    WS_MAX_SIZE = int(os.getenv("WS_MAX_SIZE", 2 * 1024 * 1024))
    WS_MAX_QUEUE = int(os.getenv("WS_MAX_QUEUE", 32))
    ENABLE_RELOAD_CONFIG = os.getenv("ENABLE_RELOAD_CONFIG", "0") == "1"
