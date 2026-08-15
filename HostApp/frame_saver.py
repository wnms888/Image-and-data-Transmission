"""Asynchronous PNG persistence for received image frames."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import queue
import threading

from protocol import Packet, rgb565_to_rgb888, rgb888_to_png


@dataclass(frozen=True)
class SaveJob:
    path: Path
    packet: Packet
    pixel_layout: str


class FrameSaveWorker:
    """Write PNGs off the Tk thread so recording never blocks live display."""

    def __init__(self, base_directory: Path, queue_capacity: int = 0) -> None:
        self.base_directory = base_directory
        self._jobs: "queue.Queue[SaveJob | None]" = queue.Queue(maxsize=queue_capacity)
        self._lock = threading.Lock()
        self._recording_directory: Path | None = None
        self._recording_index = 0
        self.saved_count = 0
        self.dropped_count = 0
        self.error_count = 0
        self.last_error = ""
        self._thread = threading.Thread(
            target=self._write_loop, name="image-frame-writer", daemon=True
        )
        self._thread.start()

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording_directory is not None

    def start_recording(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        directory = self.base_directory / f"session_{stamp}"
        directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._recording_directory = directory
            self._recording_index = 0
        return directory

    def stop_recording(self) -> Path | None:
        with self._lock:
            directory = self._recording_directory
            self._recording_directory = None
        return directory

    def enqueue_current(self, packet: Packet, pixel_layout: str) -> Path | None:
        self.base_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = self.base_directory / f"current_{stamp}_seq_{packet.sequence:05d}.png"
        return path if self._enqueue(SaveJob(path, packet, pixel_layout)) else None

    def enqueue_recorded(self, packet: Packet, pixel_layout: str) -> bool:
        with self._lock:
            directory = self._recording_directory
            if directory is None:
                return False
            self._recording_index += 1
            index = self._recording_index
        path = directory / f"frame_{index:06d}_seq_{packet.sequence:05d}.png"
        return self._enqueue(SaveJob(path, packet, pixel_layout))

    def _enqueue(self, job: SaveJob) -> bool:
        try:
            self._jobs.put_nowait(job)
            return True
        except queue.Full:
            with self._lock:
                self.dropped_count += 1
            return False

    def _write_loop(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                rgb = rgb565_to_rgb888(
                    job.packet.payload,
                    job.packet.width,
                    job.packet.height,
                    layout=job.pixel_layout,
                    packet_flags=job.packet.flags,
                )
                job.path.write_bytes(
                    rgb888_to_png(rgb, job.packet.width, job.packet.height)
                )
                with self._lock:
                    self.saved_count += 1
            except Exception as exc:
                with self._lock:
                    self.error_count += 1
                    self.last_error = str(exc)
            finally:
                self._jobs.task_done()

    def stats(self) -> tuple[int, int, int, str]:
        with self._lock:
            return self.saved_count, self.dropped_count, self.error_count, self.last_error

    def close(self) -> None:
        self.stop_recording()
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            # Let the worker drain the outstanding image jobs before shutdown.
            self._jobs.put(None)
        self._thread.join(timeout=3.0)
