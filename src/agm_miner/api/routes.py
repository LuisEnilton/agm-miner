from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from fastapi import APIRouter, BackgroundTasks, Depends

from agm_miner.config import Settings, get_settings
from agm_miner.core.exporter import DatasetExporter
from agm_miner.database.session import get_engine
from agm_miner.services.discovery import discover_repositories
from agm_miner.services.mining import mine_pending_repositories


class DiscoverRequest(BaseModel):
    start_date: date = Field(..., description="Data inicial da janela de busca")
    end_date: date = Field(..., description="Data final da janela de busca")
    query: str = Field(..., min_length=1, description="Query base da Search API")

# filename:CLAUDE.md stars:>=50 size:>=50
router = APIRouter(prefix="/api/v1", tags=["jobs"])


@router.post("/jobs/discover")
def discover_jobs(
    payload: DiscoverRequest,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    background_tasks.add_task(
        discover_repositories,
        settings,
        payload.start_date,
        payload.end_date,
        payload.query,
    )
    return {"status": "scheduled"}


@router.post("/jobs/mine-pending")
def mine_pending_job(
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    background_tasks.add_task(mine_pending_repositories, settings)
    return {"status": "scheduled"}


@router.get("/dataset/export")
def export_dataset(settings: Settings = Depends(get_settings)) -> dict[str, list[str]]:
    engine = get_engine(settings)
    exporter = DatasetExporter(settings=settings, engine=engine)
    exported_files = exporter.export()
    return {"exported_files": [str(path) for path in exported_files]}