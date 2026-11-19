"""Command-line interface: one command per pipeline stage, grouped in help panels.

Collection (GitHub + Postgres)
  agm discover            search GitHub for artifact files (stage 1)
  agm select              fetch metadata, apply selection criteria (stage 2)
  agm add-repo NAME...    register repositories by name (validation runs)
  agm mine                mine selected repositories from git clones (stage 3)
  agm run                 run stages 1-3 in parallel until done, then 4 and 5
Export and analysis
  agm export              dump the database as a bronze Parquet dataset (stage 4)
  agm analyze             build silver/gold analysis datasets from a bronze export (stage 5)
Study results (no database)
  agm audit               sampling audit of a bronze export (coverage, filters, failures)
  agm rq1 classify        RQ1 categories of every section with an LLM (cached, resumable)
  agm rq1 agentreadmes    external validation on the Agent READMEs dataset
  agm rq2 summary         RQ2 structure tables, optionally next to another dataset
  agm figures             the paper's figures as LaTeX (TikZ/pgfplots) from the analysis outputs
Operations
  agm status              what is running now and how far it got (also from another terminal)
  agm runs list|funnel    inspect pipeline runs and the selection funnel
  agm help [COMMAND...]   list every command and what it does
Experimental / pilot (not used for the final results)
  agm rq1 sample|propose|consolidate|saturation|validate|compare|smoke|models

Each module registers its commands on ``common.app`` when imported; the import order below is
the order of the commands inside each panel.
"""

# ruff: noqa: I001
from __future__ import annotations

from agm_miner.cli import pipeline  # noqa: F401
from agm_miner.cli import results  # noqa: F401
from agm_miner.cli import rq1  # noqa: F401
from agm_miner.cli import ops  # noqa: F401
from agm_miner.cli.common import PANELS, app

__all__ = ["PANELS", "app"]
