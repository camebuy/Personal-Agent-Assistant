import os
from pathlib import Path
from dotenv import load_dotenv

# Explicit paths make startup independent of the shell's working directory.
# A root .env is preferred; the existing config/.env remains a local fallback.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)
load_dotenv(BASE_DIR / "config" / ".env", override=False)


def _integer_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    if not minimum <= value <= maximum:
        return default
    return value


class SettingsConfig:
    """全局参数配置类"""

    # 1. API 与服务配置
    SILICONFLOW_API_KEY: str = os.getenv("SILICONFLOW_API_KEY", "")
    MODEL_API_KEY: str = os.getenv("MODEL_API_KEY", "").strip() or SILICONFLOW_API_KEY
    SILICONFLOW_BASE_URL: str = os.getenv("MODEL_BASE_URL", "").strip() or os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")

    # 2. 模型配置
    MODEL_NAME: str = os.getenv("MODEL_NAME", "deepseek-ai/DeepSeek-V3")
    EMBED_MODEL: str = os.getenv("EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
    LLM_TEMPERATURE: float = 0.1
    CONTEXT_WINDOW: int = 8192
    LLM_TIMEOUT: float = 60.0
    KIMI_WEBBRIDGE_HOST: str = os.getenv("KIMI_WEBBRIDGE_HOST", "127.0.0.1").strip()
    KIMI_WEBBRIDGE_PORT: int = _integer_env("KIMI_WEBBRIDGE_PORT", 10086, 1, 65535)
    KIMI_WEBBRIDGE_SESSION: str = os.getenv("KIMI_WEBBRIDGE_SESSION", "aacwork-agent").strip() or "aacwork-agent"
    KIMI_WEBBRIDGE_TIMEOUT: float = _float_env("KIMI_WEBBRIDGE_TIMEOUT", 30.0, 1.0, 300.0)

    # 3. Agent 运行限制
    MAX_ITERATIONS: int = _integer_env("AACWORK_MAX_ITERATIONS", 20, 1, 100)
    FUNCTION_CALLING: bool = os.getenv("AACWORK_FUNCTION_CALLING", "false").lower() in {"true", "1", "yes"}

    # 4. 路径配置（统一使用 absolute path，避免相对路径混乱）
    DATA_DIR: Path = Path(os.getenv("PERSONAL_AGENT_DATA_DIR", "").strip() or os.getenv("AACWORK_DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()


# 实例化全局配置对象，供全项目直接 import
settings = SettingsConfig()
