"""
LLM Handler Module

This module provides classes and functions to evaluate job fit using AI models.
It supports multiple providers (Together and OpenRouter) and includes helper
functions for input sanitization, prompt creation, and score extraction.
"""

import json
import re
import os
import asyncio
import time
import math
import random
from typing import Dict, Any, List

import tiktoken
from tenacity import retry, stop_after_attempt, wait_exponential

from jobfuq.logger import logger
from jobfuq.models.together import TogetherModel
from jobfuq.models.openrouter import OpenRouterModel

class ProviderManager:
    """
    Manages the selection and failure reporting of AI providers.

    The provider mode is determined by the configuration. It supports "together",
    "openrouter", and "multi" modes. Additionally, it tracks failures and cooldowns.
    """
    def __init__(self, config: Dict[str, Any]) -> None:
        """
        Initialize the ProviderManager with configuration settings.

        :param config: Configuration dictionary containing 'ai_providers' settings.
        """
        self.config: Dict[str, Any] = config
        # Read provider_mode from the nested ai_providers config.
        mode: str = config.get("ai_providers", {}).get("provider_mode", "openrouter").strip().lower()
        if mode == "together":
            self.providers = ["together"]
        elif mode == "openrouter":
            self.providers = ["openrouter"]
        elif mode == "multi":
            self.providers = ["together", "openrouter"]
        else:
            logger.warning(f"Unknown provider_mode '{mode}', defaulting to 'together'")
            self.providers = ["together"]

        self.current_step: int = 1
        self.together_jobs_remaining: int = 1
        self.failures: Dict[str, int] = {provider: 0 for provider in self.providers}
        self.cooldown_until: Dict[str, float] = {provider: 0.0 for provider in self.providers}

    def get_provider(self) -> str:
        """
        Select an AI provider based on current cooldowns and availability.

        :return: The selected provider name.
        """
        current_time: float = time.time()
        if "openrouter" in self.providers:
            if self.cooldown_until["openrouter"] > current_time:
                if "together" in self.providers:
                    return "together"
                return "openrouter"
            if "together" in self.providers and self.together_jobs_remaining > 0:
                self.together_jobs_remaining -= 1
                return "together"
            elif "openrouter" in self.providers:
                return "openrouter"
        return self.providers[0]

    def report_success(self, provider: str) -> None:
        """
        Reset the failure counter for the provider and update job counters.

        :param provider: The provider that succeeded.
        """
        if provider in self.providers:
            self.failures[provider] = 0
            if provider == "together":
                self.together_jobs_remaining = self.current_step + 1
            self.current_step += 1

    def report_failure(self, provider: str) -> None:
        """
        Increment the failure counter for a provider and set a cooldown if necessary.

        :param provider: The provider that failed.
        """
        if provider in self.providers:
            self.failures[provider] += 1
            if self.failures[provider] >= 2:
                self.cooldown_until[provider] = time.time() + 60
                self.failures[provider] = 0
                if "together" in self.providers:
                    self.together_jobs_remaining = self.current_step + 1
        else:
            logger.error(f"Failure reported for unknown provider '{provider}'.")

class AIModel:
    """
    Represents an AI model for evaluating job fit.

    This class prepares the prompt and delegates the evaluation to a specific provider model.
    """
    def __init__(self, config: Dict[str, Any], provider_manager: ProviderManager) -> None:
        """
        Initialize the AIModel with configuration and provider manager.

        :param config: Configuration dictionary.
        :param provider_manager: Instance of ProviderManager for selecting providers.
        """
        self.config: Dict[str, Any] = config
        self.provider_manager: ProviderManager = provider_manager
        self.encoder = tiktoken.encoding_for_model("gpt-3.5-turbo")

        # Load the prompt template from the configuration file.
        prompt_path: str = self.config.get("prompt", "prompts/examples/deepseek_r1_career_advisor_template.txt")
        prompt_path = os.path.join(os.path.dirname(__file__), prompt_path)
        with open(prompt_path, 'r') as file:
            self.prompt_template: str = file.read()

        # Instantiate provider models.
        self.together_model = TogetherModel(config, self.get_system_message())
        self.openrouter_model = OpenRouterModel(config, self.get_system_message())

    def sanitize_input(self, text: str) -> str:
        """
        Remove non-printable characters from text.

        :param text: The input text.
        :return: Sanitized text.
        """
        return ''.join(char for char in text if char.isprintable())

    def truncate_text(self, text: str, max_tokens: int) -> str:
        """
        Truncate text to a maximum number of tokens.

        :param text: The input text.
        :param max_tokens: Maximum number of tokens.
        :return: Truncated text.
        """
        tokens = self.encoder.encode(text)
        return self.encoder.decode(tokens[:max_tokens]) if len(tokens) > max_tokens else text

    def create_prompt(self, job_description: str) -> str:
        """
        Create the final prompt by replacing a placeholder in the template.

        :param job_description: Job description text.
        :return: Final prompt string.
        """
        safe_text = self.truncate_text(self.sanitize_input(job_description), 3000)
        return self.prompt_template.replace('{job_description}', safe_text)

    def get_system_message(self) -> str:
        """
        Return the system message for the AI model.

        :return: System message string.
        """
        return "You are an AI career advisor. Provide concise JSON answers."

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=10, min=10, max=60))
    async def evaluate_job_fit(self, job_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate the job fit using the selected AI provider.

        Constructs a prompt from the job information, calls the provider's evaluation function,
        and extracts the response metrics.

        :param job_info: Dictionary containing job details.
        :return: Dictionary of extracted evaluation metrics.
        :raises: Exception if evaluation fails after retries.
        """
        provider: str = self.provider_manager.get_provider()
        job_description: str = (
            f"Company: {job_info.get('company', 'Unknown')}\n"
            f"Title: {job_info.get('title', 'Unknown')}\n"
            f"Location: {job_info.get('location', 'Unknown')}\n"
            f"Description: {job_info.get('description', 'No description available')}"
        )
        prompt: str = self.create_prompt(job_description)
        if not provider:
            logger.error("No available providers.")
            return self.create_error_response("No available providers.")
        try:
            if provider == "together":
                raw_result: str = await self.together_model.evaluate(prompt)
            elif provider == "openrouter":
                raw_result = await self.openrouter_model.evaluate(prompt)
            else:
                logger.error(f"Provider '{provider}' not available.")
                return self.create_error_response(f"Provider '{provider}' not available.")
            self.provider_manager.report_success(provider)
            qt: int = len(self.encoder.encode(prompt))
            rt: int = len(self.encoder.encode(raw_result))
            logger.info(f"Raw response ({provider}): {raw_result}")
            extracted: Dict[str, Any] = self.extract_scores(raw_result)
            extracted['query_token_count'] = qt
            extracted['response_token_count'] = rt
            return extracted
        except Exception as e:
            self.provider_manager.report_failure(provider)
            raise e

    def create_error_response(self, error_message: str) -> Dict[str, Any]:
        """
        Create a default error response.

        :param error_message: The error message to include.
        :return: Dictionary with error response metrics.
        """
        return {
            'skills_match': 0.0,
            'resume_similarity': 0.0,
            'final_fit_score': 0.0,
            'reasoning': error_message,
            'areas_for_development': "Error during evaluation",
            'success_probability': 0.6,
            'confidence': 0.7,
            'effort_days_to_fit': 4,
            'critical_skill_mismatch_penalty': 0.0,
            'query_token_count': 0,
            'response_token_count': 0
        }

    def remove_think_tags(self, text: str) -> str:
        """
        Remove content within <think> ... </think> tags from the text.

        :param text: The input text.
        :return: Cleaned text.
        """
        return re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE)

    def extract_scores(self, text: str) -> Dict[str, Any]:
        """
        Extract evaluation metrics from the AI response.

        The function attempts to extract JSON-formatted data from text using several regex patterns.
        If no valid JSON is found, returns an error response.

        :param text: The raw text response from the AI.
        :return: Dictionary of evaluation metrics.
        """
        cleaned_text: str = self.remove_think_tags(text)

        def safe_float(value: Any) -> float:
            try:
                s = str(value).strip()
                return float(s.rstrip('%')) / 100 if '%' in s else float(s)
            except Exception:
                return 0.0

        json_patterns: List[str] = [
            r'```json\s*(\{[\s\S]*?\})\s*```',
            r'```\s*(\{[\s\S]*?\})\s*```',
            r'(\{[\s\S]*?\})'
        ]
        data: Dict[str, Any] = {}
        for pattern in json_patterns:
            match = re.search(pattern, cleaned_text)
            if match:
                try:
                    data = json.loads(match.group(1))
                    break
                except json.JSONDecodeError:
                    continue
        if not data:
            logger.warning("No JSON found in response after removing <think> tags.")
        result: Dict[str, Any] = {
            'skills_match': safe_float(data.get('skills_match', 0.0)),
            'resume_similarity': safe_float(data.get('resume_similarity', 0.0)),
            'final_fit_score': safe_float(data.get('final_fit_score', 0.0)),
            'reasoning': str(data.get('reasoning', '')).strip(),
            'areas_for_development': str(data.get('areas_for_development', '')).strip(),
            'success_probability': safe_float(data.get('success_probability', 0.6)),
            'confidence': safe_float(data.get('confidence', 0.7)),
            'effort_days_to_fit': safe_float(data.get('effort_days_to_fit', 4)),
            'critical_skill_mismatch_penalty': safe_float(data.get('critical_skill_mismatch_penalty', 0.0))
        }
        for key in ['skills_match', 'resume_similarity', 'final_fit_score']:
            result[key] = min(max(result[key], 0.0), 1.0)
        if all(result[k] == 0.0 for k in ['skills_match', 'resume_similarity', 'final_fit_score']) and not (result.get('reasoning') or result.get('areas_for_development')):
            logger.warning("Extraction failure: all numeric scores 0.0 with no explanation.")
            return self.create_error_response("Extraction failed.")
        return result

async def evaluate_job(ai_model: AIModel, job: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate a job by merging the original job data with the AI evaluation results.

    :param ai_model: An instance of AIModel.
    :param job: Dictionary containing the original job data.
    :return: Combined job data and evaluation metrics.
    """
    evaluation: Dict[str, Any] = await ai_model.evaluate_job_fit(job)
    return {**job, **evaluation}
