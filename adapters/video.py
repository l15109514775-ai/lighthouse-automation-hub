from pathlib import Path
import os
import subprocess
import sys
from services.hub.errors import HubError
from services.hub.evidence import sha256_file


class VideoProcessor:
    def __init__(self, root):
        self.root = Path(root)

    def probe(self, path):
        try:
            # Module launch prevents adapters/http.py from shadowing Python's stdlib http package.
            process = subprocess.run([sys.executable, '-m', 'adapters.media_probe', str(path)],
                                     cwd=self.root, capture_output=True, timeout=30)
            if process.returncode:
                raise HubError('UPSTREAM_MEDIA_PROBE_FAILED', review=True)
            return float(process.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            raise HubError('MEDIA_PROBE_RUNTIME_UNAVAILABLE', review=True) from None

    def remux(self, source, destination, check_disk):
        source, destination = Path(source), Path(destination)
        # Stream copy has a bounded output similar to the source, reserve before starting.
        check_disk(source.stat().st_size + 64_000_000)
        duration = self.probe(source)
        temporary = destination.with_suffix('.partial.mp4')
        try:
            proc = subprocess.run(['ffmpeg','-nostdin','-y','-v','error','-threads','1','-i',str(source),
                                   '-map','0:v:0','-map','0:a:0','-c','copy','-movflags','+faststart',
                                   '-fs',str(source.stat().st_size + 64_000_000),str(temporary)],
                                  capture_output=True, timeout=7200)
        except (OSError, subprocess.SubprocessError):
            raise HubError('FFMPEG_RUNTIME_ERROR', retryable=True) from None
        if proc.returncode or not temporary.is_file():
            raise HubError('FFMPEG_PROCESSING_FAILED', review=True)
        actual_duration = self.probe(temporary)
        if abs(actual_duration - duration) > 1:
            raise HubError('PROCESSED_MEDIA_DURATION_MISMATCH', review=True)
        os.replace(temporary, destination)
        return {'file':str(destination), 'sha256':sha256_file(destination), 'duration':actual_duration,
                'processing':'stream_copy_remux'}
