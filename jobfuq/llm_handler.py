import json, re, os, asyncio, time, math, random
from typing import Dict, Any, List
import tiktoken
from tenacity import retry, stop_after_attempt, wait_exponential
from jobfuq.logger import logger
from jobfuq.models.together import TogetherModel
from jobfuq.models.openrouter import OpenRouterModel

class ProviderManager:
    def __init__(self, config):
        self.config = config
        mode = config.get('ai_providers', {}).get('provider_mode', 'openrouter').strip().lower()
        if mode == 'together':
            self.providers = ['together']
        elif mode == 'openrouter':
            self.providers = ['openrouter']
        elif mode == 'multi':
            self.providers = ['together', 'openrouter']
        else:
            logger.warning(f"Unknown provider_mode '{mode}', defaulting to 'together'")
            self.providers = ['together']
        self.current_step = 1
        self.together_jobs_remaining = 1
        self.failures = {provider: 0 for provider in self.providers}
        self.cooldown_until = {provider: 0.0 for provider in self.providers}

    def get_provider(self):
        current_time = time.time()
        if 'openrouter' in self.providers:
            if self.cooldown_until['openrouter'] > current_time:
                if 'together' in self.providers:
                    return 'together'
                return 'openrouter'
            if 'together' in self.providers and self.together_jobs_remaining > 0:
                self.together_jobs_remaining -= 1
                return 'together'
            elif 'openrouter' in self.providers:
                return 'openrouter'
        return self.providers[0]

    def report_success(self, provider):
        if provider in self.providers:
            self.failures[provider] = 0
            if provider == 'together':
                self.together_jobs_remaining = self.current_step + 1
            self.current_step += 1

    def report_failure(self, provider):
        if provider in self.providers:
            self.failures[provider] += 1
            if self.failures[provider] >= 2:
                self.cooldown_until[provider] = time.time() + 60
                self.failures[provider] = 0
                if 'together' in self.providers:
                    self.together_jobs_remaining = self.current_step + 1
        else:
            logger.error(f"Failure reported for unknown provider '{provider}'.")

class AIModel:
    def __init__(self, config, provider_manager):
        self.config = config
        self.provider_manager = provider_manager
        self.encoder = tiktoken.encoding_for_model('gpt-3.5-turbo')
        prompt_path = self.config.get('prompt', 'prompts/examples/deepseek_r1_career_advisor_template.txt')
        prompt_path = os.path.join(os.path.dirname(__file__), prompt_path)
        with open(prompt_path, 'r') as file:
            self.prompt_template = file.read()
        self.together_model = TogetherModel(config, self.get_system_message())
        self.openrouter_model = OpenRouterModel(config, self.get_system_message())

    def sanitize_input(self, text: str) -> str:
        return ''.join(char for char in text if char.isprintable())

    def truncate_text(self, text: str, max_tokens: int) -> str:
        tokens = self.encoder.encode(text)
        if len(tokens) > max_tokens:
            return self.encoder.decode(tokens[:max_tokens])
        return text

    def create_prompt(self, job_description: str) -> str:
        safe_text = self.truncate_text(self.sanitize_input(job_description), 3000)
        return self.prompt_template.replace('{job_description}', safe_text)

    def get_system_message(self) -> str:
        return 'You are an AI career advisor. Provide concise JSON answers.'

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=10, min=10, max=60))
    async def evaluate_job_fit(self, job_info: Dict[str, Any]) -> Dict[str, Any]:
        provider = self.provider_manager.get_provider()
        job_description = (
            f"Company: {job_info.get('company','Unknown')}\n"
            f"Title: {job_info.get('title','Unknown')}\n"
            f"Location: {job_info.get('location','Unknown')}\n"
            f"Description: {job_info.get('description','No description available')}"
        )
        prompt = self.create_prompt(job_description)
        if not provider:
            logger.error('No available providers.')
            return self.create_error_response('No available providers.')
        try:
            if provider == 'together':
                raw_result = await self.together_model.evaluate(prompt)
            elif provider == 'openrouter':
                raw_result = await self.openrouter_model.evaluate(prompt)
            else:
                logger.error(f"Provider '{provider}' not available.")
                return self.create_error_response(f"Provider '{provider}' not available.")
            self.provider_manager.report_success(provider)
            qt = len(self.encoder.encode(prompt))
            rt = len(self.encoder.encode(raw_result))
            logger.info(f"Raw response ({provider}): {raw_result}")
            extracted = self.extract_scores(raw_result)
            extracted['query_token_count'] = qt
            extracted['response_token_count'] = rt
            return extracted
        except Exception as e:
            self.provider_manager.report_failure(provider)
            raise e

    def create_error_response(self, error_message: str) -> Dict[str, Any]:
        return {
            'skills_match': 0.0,
            'experience_gap': 0.0,
            'model_fit_score': 0.0,
            'reasoning': error_message,
            'areas_for_development': 'Error during evaluation',
            'success_probability': 50.0,
            'role_complexity': 50.0,
            'effort_days_to_fit': 10.0,
            'critical_skill_mismatch_penalty': 0.0,
            'query_token_count': 0,
            'response_token_count': 0
        }

    def remove_think_tags(self, text: str) -> str:
        return re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE)

    def extract_scores(self, text: str) -> Dict[str, Any]:
        cleaned_text = self.remove_think_tags(text)
        def safe_float(value):
            try:
                s = str(value).strip()
                if s.endswith('%'):
                    s = s[:-1]
                return float(s)
            except:
                return 0.0
        json_patterns = [
            r'```json\s*(\{[\s\S]*?\})\s*```',
            r'```\s*(\{[\s\S]*?\})\s*```',
            r'(\{[\s\S]*?\})'
        ]
        data = {}
        for pattern in json_patterns:
            match = re.search(pattern, cleaned_text)
            if match:
                try:
                    data = json.loads(match.group(1))
                    break
                except json.JSONDecodeError:
                    continue
        if not data:
            logger.warning('No JSON found in response after removing <think> tags.')
        # If the model outputs values in the 0–1 range, scale them to 0–100.
        def scale_if_needed(x):
            val = safe_float(x)
            if 0 < val < 2:
                return val * 100
            return val
        result = {
            'skills_match': scale_if_needed(data.get('skills_match', data.get('resume_similarity', 0.0))),
            'experience_gap': scale_if_needed(data.get('experience_gap', 0.0)),
            'model_fit_score': scale_if_needed(data.get('model_fit_score', data.get('final_fit_score', 0.0))),
            'success_probability': scale_if_needed(data.get('success_probability', 50.0)),
            'role_complexity': scale_if_needed(data.get('role_complexity', data.get('confidence', 50.0))),
            'effort_days_to_fit': safe_float(data.get('effort_days_to_fit', 7.0)),
            'critical_skill_mismatch_penalty': scale_if_needed(data.get('critical_skill_mismatch_penalty', 0.0)),
            'areas_for_development': str(data.get('areas_for_development', '')).strip(),
            'reasoning': str(data.get('reasoning', '')).strip()
        }
        def clamp100(x):
            val = int(round(x))
            if val < 0:
                return 0
            if val > 100:
                return 100
            return val
        for key in ['skills_match', 'experience_gap', 'model_fit_score', 'success_probability', 'role_complexity', 'critical_skill_mismatch_penalty']:
            result[key] = clamp100(result[key])
        result['effort_days_to_fit'] = int(round(result['effort_days_to_fit']))
        numeric_keys = ['skills_match', 'experience_gap', 'model_fit_score', 'success_probability', 'role_complexity', 'effort_days_to_fit', 'critical_skill_mismatch_penalty']
        if all(result[k] == 0 for k in numeric_keys) and not (result['reasoning'] or result['areas_for_development']):
            logger.warning('Extraction failure: all numeric scores 0 with no explanation.')
            return self.create_error_response('Extraction failed.')
        return result

async def evaluate_job(ai_model: AIModel, job: Dict[str, Any]) -> Dict[str, Any]:
    evaluation = await ai_model.evaluate_job_fit(job)
    return {**job, **evaluation}
