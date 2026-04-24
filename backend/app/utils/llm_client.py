"""
LLM客户端封装
统一使用OpenAI格式调用
"""

import json
import re
from typing import Optional, Dict, Any, List
from openai import OpenAI

from ..config import Config


class LLMClient:
    """LLM客户端"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME
        
        if not self.api_key:
            raise ValueError("LLM_API_KEY 未配置")
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )

    def _prefers_max_completion_tokens(self) -> bool:
        """OpenAI GPT-5 / reasoning 系列使用 max_completion_tokens。"""
        model_name = (self.model or "").lower()
        return model_name.startswith(("gpt-5", "o1", "o3", "o4"))

    def _supports_custom_temperature(self) -> bool:
        """部分 OpenAI 推理模型仅支持默认 temperature，不应显式传入。"""
        return not self._prefers_max_completion_tokens()

    @staticmethod
    def _swap_token_limit_param(kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """在 max_tokens 与 max_completion_tokens 之间切换，兼容不同模型接口。"""
        swapped = dict(kwargs)
        if "max_tokens" in swapped:
            swapped["max_completion_tokens"] = swapped.pop("max_tokens")
        elif "max_completion_tokens" in swapped:
            swapped["max_tokens"] = swapped.pop("max_completion_tokens")
        return swapped

    @staticmethod
    def _is_token_param_error(exc: Exception) -> bool:
        message = str(exc)
        return (
            "Unsupported parameter" in message and
            ("max_tokens" in message or "max_completion_tokens" in message)
        )

    @staticmethod
    def _without_temperature(kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """移除 temperature 以兼容只支持默认值的模型。"""
        normalized = dict(kwargs)
        normalized.pop("temperature", None)
        return normalized

    @staticmethod
    def _is_temperature_param_error(exc: Exception) -> bool:
        message = str(exc)
        return (
            "temperature" in message and
            ("Unsupported value" in message or "unsupported_value" in message)
        )
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = 4096,
        response_format: Optional[Dict] = None
    ) -> str:
        return self.chat_text(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format
        )

    def create_chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = 4096,
        response_format: Optional[Dict] = None
    ):
        """
        发送原始 chat completion 请求并返回完整 response 对象。
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数；传 None 时不显式指定
            response_format: 响应格式（如JSON模式）
            
        Returns:
            OpenAI-compatible response
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
        }

        if self._supports_custom_temperature():
            kwargs["temperature"] = temperature

        if max_tokens is not None:
            token_param = "max_completion_tokens" if self._prefers_max_completion_tokens() else "max_tokens"
            kwargs[token_param] = max_tokens
        
        if response_format:
            kwargs["response_format"] = response_format

        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            if self._is_token_param_error(exc):
                response = self.client.chat.completions.create(
                    **self._swap_token_limit_param(kwargs)
                )
            elif self._is_temperature_param_error(exc):
                response = self.client.chat.completions.create(
                    **self._without_temperature(kwargs)
                )
            else:
                raise

        return response

    def chat_text(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = 4096,
        response_format: Optional[Dict] = None
    ) -> str:
        """发送聊天请求并直接返回文本内容。"""
        response = self.create_chat_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format
        )

        content = response.choices[0].message.content
        # 部分模型（如MiniMax M2.5）会在content中包含<think>思考内容，需要移除
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return content
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """
        发送聊天请求并返回JSON
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            
        Returns:
            解析后的JSON对象
        """
        response = self.chat_text(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        # 清理markdown代码块标记
        cleaned_response = response.strip()
        cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
        cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
        cleaned_response = cleaned_response.strip()

        try:
            return json.loads(cleaned_response)
        except json.JSONDecodeError:
            raise ValueError(f"LLM返回的JSON格式无效: {cleaned_response}")
