"""
OpenRouter Model Module

This module defines the OpenRouterModel class, which interfaces with the OpenRouter API
to perform chat completion requests for job evaluation. It implements rate limiting based
on API feedback.
"""

import aiohttp
import asyncio
import time
from typing import Any, Dict, List

from openai import OpenAI
from jobfuq.logger.logger import logger


class OpenRouterModel:
    """
    A model that uses the OpenRouter API for chat completions.

    It dynamically updates its rate limits by querying the API and enforces a sliding-window
    rate limiter.
    """

    def __init__(self, config: Dict[str, Any], system_message: str) -> None:
        """
        Initialize the OpenRouterModel.

        Args:
            config (Dict[str, Any]): A configuration dictionary containing API keys and model
                information.
            system_message (str): The system message to include in every prompt.

        Raises:
            ValueError: If no OpenRouter API key is provided in the config.
        """
        # ==== CONFIGURATION & INITIALIZATION ==== #
        ai_config: Dict[str, Any] = config.get("ai_providers", {})
        api_keys: List[str] = ai_config.get("openrouter_api_keys", [])
        if not api_keys:
            raise ValueError(
                "No OpenRouter API key provided in ai_providers.openrouter_api_keys."
            )
        self.api_key: str = api_keys[0]
        self.model: str = ai_config.get("openrouter_model", "deepseek/deepseek-r1:free")
        self.system_message: str = system_message

        # Use free limits if the model ID ends with ':free'; otherwise, use provided config.
        self.rpm_limit: int = (
            20 if self.model.endswith(":free") else ai_config.get("openrouter_rpm", 55)
        )
        self._requests: List[float] = []
        self.window: int = 60  # seconds

    async def _update_rate_limit(self) -> None:
        """
        Update the rate limit (rpm_limit) dynamically based on API rate limit data.

        This function makes a GET request to the OpenRouter auth endpoint to fetch rate limit
        information.

        Raises:
            Exception: If there is an issue with the API request.
        """
        # ==== RATE LIMIT UPDATE SECTION ==== #
        try:
            async with aiohttp.ClientSession() as session:
                headers: Dict[str, str] = {"Authorization": f"Bearer {self.api_key}"}
                async with session.get(
                        "https://openrouter.ai/api/v1/auth/key", headers=headers
                ) as resp:
                    if resp.status == 200:
                        data: Dict[str, Any] = await resp.json()
                        logger.debug(f"Rate limit update response: {data}")
                        # Assume data contains a "rate_limit" field in the format "10s"
                        rate_limit_str: str = data.get("rate_limit", "10s")
                        seconds: int = int(rate_limit_str.rstrip("s"))
                        if not self.model.endswith(":free"):
                            self.rpm_limit = int(60 / seconds)
                    else:
                        logger.warning(
                            f"Rate limit update failed with status {resp.status}"
                        )
        except Exception as e:
            logger.warning(f"Failed to update OpenRouter rate limit: {e}")

    async def _rate_limit(self) -> None:
        """
        Enforce rate limiting using a sliding-window mechanism.

        This method updates the rate limit and sleeps if the number of requests in the current
        window exceeds the limit.
        """
        # ==== RATE LIMIT ENFORCEMENT ==== #
        await self._update_rate_limit()

        current_time: float = time.time()

        # Purge requests that are outside of the current time window
        while self._requests and current_time - self._requests[0] > self.window:
            self._requests.pop(0)

        if len(self._requests) >= self.rpm_limit:
            sleep_time: float = self.window - (current_time - self._requests[0])
            logger.debug(
                f"OpenRouter rate limiter sleeping for {sleep_time:.2f} seconds."
            )
            await asyncio.sleep(sleep_time)

        self._requests.append(time.time())

    async def evaluate(self, prompt: str, max_tokens: int = 10000) -> str:
        """
        Evaluate a prompt using the OpenRouter API.

        Args:
            prompt (str): The prompt string to send.
            max_tokens (int, optional): Maximum number of tokens to generate. Defaults to 10000.

        Returns:
            str: The response text from the API.

        Raises:
            Exception: If the API response is invalid.
        """
        # ==== PROMPT EVALUATION SECTION ==== #
        await self._rate_limit()

        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self.api_key,
            default_headers={
                "HTTP-Referer": "localhost",
                "X-Title": "Job Analyzer",
            },
        )

        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_message},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
        )

        # Add verbose logging to inspect response structure
        logger.debug(f"OpenRouter API raw response: {response}")

        if response and hasattr(response, "choices") and response.choices:
            result: str = response.choices[0].message.content
            if result:
                return result

        # Log the entire response as error if structure is invalid
        logger.error("Invalid response structure from OpenRouter.")
        logger.error(f"Received response: {response}")
        raise Exception("Invalid response structure from OpenRouter.")