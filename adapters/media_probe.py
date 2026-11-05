"""Linux bridge to the existing worker's probe; no ASR or model import is executed."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor/subtitle-cloud-worker'))
import worker

if __name__ == '__main__':
    print(worker.probe(Path(sys.argv[1])))
