"""
配置管理
统一从项目根目录的 .env 文件加载配置
"""

import os
from dotenv import load_dotenv

# 加载项目根目录的 .env 文件
# 路径: MiroFish/.env (相对于 backend/app/config.py)
project_root_env = os.path.join(os.path.dirname(__file__), '../../.env')

if os.path.exists(project_root_env):
    # 不覆盖已经存在的环境变量，避免示例 .env 中的占位值污染真实配置
    load_dotenv(project_root_env, override=False)
else:
    # 如果根目录没有 .env，尝试加载环境变量（用于生产环境）
    load_dotenv(override=False)


PLACEHOLDER_ENV_VALUES = {
    'your_api_key_here',
    'your_zep_api_key_here',
    'your_base_url_here',
    'your_model_name_here',
}


def _normalize_env_value(value: str | None) -> str | None:
    """将空字符串和示例占位值视为未配置。"""
    if value is None:
        return None

    normalized = value.strip()
    if not normalized:
        return None

    lowered = normalized.lower()
    if lowered in PLACEHOLDER_ENV_VALUES:
        return None

    if lowered.startswith('your_') and lowered.endswith('_here'):
        return None

    return normalized


def _get_env(name: str, default: str | None = None) -> str | None:
    value = _normalize_env_value(os.environ.get(name))
    if value is not None:
        return value

    if default is None:
        return None

    return _normalize_env_value(default)


class Config:
    """Flask配置类"""
    
    # Flask配置
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mirofish-secret-key')
    DEBUG = os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'
    
    # JSON配置 - 禁用ASCII转义，让中文直接显示（而不是 \uXXXX 格式）
    JSON_AS_ASCII = False
    
    # LLM配置（统一使用OpenAI格式）
    LLM_API_KEY = _get_env('LLM_API_KEY')
    LLM_BASE_URL = _get_env('LLM_BASE_URL', 'https://api.openai.com/v1')
    LLM_MODEL_NAME = _get_env('LLM_MODEL_NAME', 'gpt-4o-mini')
    
    # Zep配置
    ZEP_API_KEY = _get_env('ZEP_API_KEY')
    
    # 文件上传配置
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}
    
    # 文本处理配置
    DEFAULT_CHUNK_SIZE = 500  # 默认切块大小
    DEFAULT_CHUNK_OVERLAP = 50  # 默认重叠大小
    
    # OASIS模拟配置
    OASIS_DEFAULT_MAX_ROUNDS = int(os.environ.get('OASIS_DEFAULT_MAX_ROUNDS', '10'))
    OASIS_SIMULATION_DATA_DIR = os.path.join(os.path.dirname(__file__), '../uploads/simulations')
    
    # OASIS平台可用动作配置
    OASIS_TWITTER_ACTIONS = [
        'CREATE_POST', 'LIKE_POST', 'REPOST', 'FOLLOW', 'DO_NOTHING', 'QUOTE_POST'
    ]
    OASIS_REDDIT_ACTIONS = [
        'LIKE_POST', 'DISLIKE_POST', 'CREATE_POST', 'CREATE_COMMENT',
        'LIKE_COMMENT', 'DISLIKE_COMMENT', 'SEARCH_POSTS', 'SEARCH_USER',
        'TREND', 'REFRESH', 'DO_NOTHING', 'FOLLOW', 'MUTE'
    ]
    
    # Report Agent配置
    REPORT_AGENT_MAX_TOOL_CALLS = int(os.environ.get('REPORT_AGENT_MAX_TOOL_CALLS', '5'))
    REPORT_AGENT_MAX_REFLECTION_ROUNDS = int(os.environ.get('REPORT_AGENT_MAX_REFLECTION_ROUNDS', '2'))
    REPORT_AGENT_TEMPERATURE = float(os.environ.get('REPORT_AGENT_TEMPERATURE', '0.5'))
    
    @classmethod
    def validate_llm(cls):
        """验证 LLM 配置"""
        errors = []
        if not cls.LLM_API_KEY:
            errors.append("LLM_API_KEY 未配置")
        return errors

    @classmethod
    def validate_zep(cls):
        """验证 Zep 配置"""
        errors = []
        if not cls.ZEP_API_KEY:
            errors.append("ZEP_API_KEY 未配置")
        return errors

    @classmethod
    def validate(cls):
        """验证必要配置"""
        return cls.validate_llm() + cls.validate_zep()
