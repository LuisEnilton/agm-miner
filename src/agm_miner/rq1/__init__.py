"""RQ1 content coding: which categories of instructions appear in agent artifacts.

Two phases over the sections of ``rq1_sections`` (analysis gold output):

1. **Label creation** on a stratified sample: an LLM proposes free labels per section, then
   consolidates them into a draft codebook (seeded with the taxonomy of related work) that a
   researcher curates into ``codebooks/rq1.yaml``.
2. **Classification** of every section against the curated codebook, validated against a blind
   human-labelled sample (Cohen's kappa).

Like the analysis, this package never touches the database: it reads gold Parquet files and
writes its own files with a manifest.
"""
