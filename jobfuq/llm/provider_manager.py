#!/usr/bin/env python3
import time
from typing import Dict, Any, List
from jobfuq.logger.logger import logger

class ProviderManager:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config: Dict[str, Any] = config
        mode: str = config.get("ai_providers", {}).get("provider_mode", "openrouter").strip().lower()
        if mode == "together":
            self.providers: List[str] = ["together"]
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
        if provider in self.providers:
            self.failures[provider] = 0
            if provider == "together":
                self.together_jobs_remaining = self.current_step + 1
            self.current_step += 1

    def report_failure(self, provider: str) -> None:
        if provider in self.providers:
            self.failures[provider] += 1
            if self.failures[provider] >= 2:
                self.cooldown_until[provider] = time.time() + 60
                self.failures[provider] = 0
                if "together" in self.providers:
                    self.together_jobs_remaining = self.current_step + 1
        else:
            logger.error(f"Failure reported for unknown provider '{provider}'.")