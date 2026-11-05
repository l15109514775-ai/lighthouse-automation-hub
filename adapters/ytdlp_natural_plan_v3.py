"""Metadata-only subprocess boundary; never an external shell interface."""
from pathlib import Path
import sys
from .ytdlp_natural_v3 import planning_child

if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--download':raise SystemExit('INTERNAL_NATURAL_PLAN_HELPER_ONLY')
    raise SystemExit(planning_child(Path(sys.argv[2])))
