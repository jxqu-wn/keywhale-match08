"""Project paths shared by all preparation, inference and export commands."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / 'data/raw/subject'
REFERENCE = ROOT / 'data/reference'
CURATED = ROOT / 'data/curated'
OUT = ROOT / 'data/processed'
BASELINE = ROOT / 'artifacts/baseline'
SERVICE = ROOT / 'artifacts/service'
RUNS = ROOT / 'artifacts/runs'
SUBMISSION = ROOT / 'outputs/submission'
