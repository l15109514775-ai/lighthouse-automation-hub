"""Metadata-only private subprocess; media requests remain prohibited."""
from pathlib import Path
import sys
from .ytdlp_whole_v4 import child

if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--download':raise SystemExit('INTERNAL_WHOLE_REPLAY_PLAN_HELPER_ONLY')
    raise SystemExit(child(Path(sys.argv[2]),planning=True))
