"""Runtime settings. Everything that changes a finding is recorded with it."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # local dev only; on Cloud Run the key comes from Secret Manager
DATA_DIR = ROOT / "data"
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
EVAL_DIR = ROOT / "evals"
LOG_DIR = Path(os.environ.get("ATTEST_LOG_DIR", ROOT / "logs"))

MEASUREMENT_YEAR = 2026

MODEL = os.environ.get("ATTEST_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("ATTEST_EFFORT", "high")
MAX_TOKENS = 16000

# The dataset the app and the eval run against: "v1" (data/*.json) or a v2 bundle path. The held-out
# split of record follows it, so the eval panel and the CI gate always speak about the loaded population.
DATASET = os.environ.get("ATTEST_DATASET", "v1")
HELDOUT_SPLIT = "heldout" if DATASET == "v1" else "v2-heldout"

# One prompt per measure, versioned by filename (attest/prompts/<version>.md); the registry
# (attest/measures/*.yaml) is the source of truth.
from .registry import prompt_versions as _prompt_versions  # noqa: E402

PROMPT_VERSIONS = _prompt_versions()

# USD per million tokens, for the cost line in eval runs.
PRICE_PER_MTOK = {"claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00}}
