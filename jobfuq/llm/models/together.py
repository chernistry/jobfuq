"""
Together Model Module

This module defines the TogetherModel class, which interfaces with Together's API
for chat completions. It enforces rate limiting (with special handling for the
model-specific limit for "deepseek-ai/DeepSeek-R1") and allows merging of extra API parameters.
"""

import aiohttp
import asyncio
import time
from typing import Dict, Any

from jobfuq.logger.logger import logger

class TogetherModel:
    """
    A model that uses Together's API for chat completions.

    It enforces rate limiting based on API responses and, for the model "deepseek-ai/DeepSeek-R1",
    applies a model-specific limit of 6 queries per minute.
    """
    def __init__(self, config: Dict[str, Any], system_message: str) -> None:
        """
        Initialize the TogetherModel.

        :param config: A configuration dictionary containing API keys, model name, and extra parameters.
        :param system_message: The system message to include in every prompt.
        """
        # Read AI provider settings from the nested config.
        ai_config = config.get("ai_providers", {})
        self.api_key: str = ai_config.get("together_api_key")
        self.model: str = ai_config.get("together_model", "deepseek-ai/DeepSeek-R1")
        self.system_message: str = system_message
        # If the model is "deepseek-ai/DeepSeek-R1", enforce the model-specific limit.
        if self.model == "deepseek-ai/DeepSeek-R1":
            self.rpm_limit: int = 6
        else:
            self.rpm_limit: int = ai_config.get("together_rpm", 55)
        self._requests: list = []
        self.window: int = 60  # seconds
        # Allow passing extra API parameters from config.
        self.extra_params: Dict[str, Any] = ai_config.get("together_extra_params", {})

    async def _update_rate_limit(self) -> None:
        """
        Query Together's auth endpoint for current rate-limit information and update rpm_limit.

        Expects a JSON response with a "rate_limit" field formatted like "10s".
        For the "deepseek-ai/DeepSeek-R1" model, the limit is forced to 6 RPM.
        """
        try:
            async with aiohttp.ClientSession() as session:
                headers: Dict[str, str] = {"Authorization": f"Bearer {self.api_key}"}
                async with session.get("https://api.together.ai/v1/auth/key", headers=headers) as resp:
                    if resp.status == 200:
                        data: Dict[str, Any] = await resp.json()
                        rate_limit_str: str = data.get("rate_limit", "10s")
                        seconds: int = int(rate_limit_str.rstrip("s"))
                        new_limit: int = int(60 / seconds)
                        # If using the model-specific limit, enforce 6 RPM maximum.
                        if self.model == "deepseek-ai/DeepSeek-R1":
                            new_limit = min(new_limit, 6)
                        logger.info(f"Together rate limit updated: {new_limit} RPM (based on {rate_limit_str}).")
                        self.rpm_limit = new_limit
        except Exception as e:
            logger.warning(f"Failed to update Together rate limit: {e}")

    async def _rate_limit(self) -> None:
        """
        Enforce a sliding-window rate limiter for Together API requests.

        Updates the rate limit and sleeps if necessary.
        """
        await self._update_rate_limit()
        current_time: float = time.time()
        while self._requests and current_time - self._requests[0] > self.window:
            self._requests.pop(0)
        if len(self._requests) >= self.rpm_limit:
            sleep_time: float = self.window - (current_time - self._requests[0])
            logger.debug(f"Together rate limiter sleeping for {sleep_time:.2f} seconds.")
            await asyncio.sleep(sleep_time)
        self._requests.append(time.time())

    async def evaluate(self, prompt: str, max_tokens: int = 8000) -> str:
        """
        Evaluate a prompt using Together's API.

        :param prompt: The prompt string.
        :param max_tokens: Maximum number of tokens to generate.
        :return: The API response text.
        :raises Exception: If the API response is invalid.
        """
        await self._rate_limit()
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_message},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stop": None,
            "restart": True
        }
        # Merge any extra API parameters provided in the config.
        payload.update(self.extra_params)
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.together.xyz/v1/chat/completions", headers=headers, json=payload) as response:
                if response.status == 200:
                    data: Dict[str, Any] = await response.json()
                    result: str = data.get('choices', [{}])[0].get('message', {}).get('content')
                    if result:
                        # Optionally update rate limits based on response headers.
                        ratelimit: str = response.headers.get("x-ratelimit-limit")
                        if ratelimit:
                            try:
                                rps: float = float(ratelimit)
                                new_rpm: int = int(rps * 60)
                                if self.model == "deepseek-ai/DeepSeek-R1":
                                    new_rpm = min(new_rpm, 6)
                                if new_rpm != self.rpm_limit:
                                    logger.info(f"Together rate limit adjusted via headers: {new_rpm} RPM.")
                                    self.rpm_limit = new_rpm
                            except Exception as e:
                                logger.debug(f"Failed to parse Together rate limit header: {e}")
                        return result
                error_text: str = await response.text()
                logger.error(f"Together API failed {response.status}: {error_text}")
                raise Exception(f"Together API request failed: {response.status}")
