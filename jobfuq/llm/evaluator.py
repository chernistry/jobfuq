from typing import Dict, Any
from jobfuq.logger.logger import logger

async def evaluate_job(ai_model: "AIModel", job: Dict[str, Any]) -> Dict[str, Any]:
    evaluation: Dict[str, Any] = await ai_model.evaluate_job_fit(job)
    return {**job, **evaluation}