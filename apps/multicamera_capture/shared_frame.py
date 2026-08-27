from __future__ import annotations

from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import Any

import numpy as np


@dataclass(slots=True)
class SharedFrameState:
    shared_memory_name: str
    shape: tuple[int, ...]
    sequence: Any
    timestamp_ns: Any
    active_slot: Any
    frame_ready: Any
    metadata_lock: Any


@dataclass(frozen=True, slots=True)
class FrameSnapshot:
    sequence: int
    timestamp_ns: int
    frame: np.ndarray


class SharedFrameOwner:
    def __init__(self, context: Any, shape: tuple[int, ...]) -> None:
        frame_bytes = int(np.prod(shape, dtype=np.int64))
        self._shared_memory = shared_memory.SharedMemory(
            create=True,
            size=frame_bytes * 2,
        )
        self.state = SharedFrameState(
            shared_memory_name=self._shared_memory.name,
            shape=shape,
            sequence=context.Value("Q", 0, lock=False),
            timestamp_ns=context.Value("Q", 0, lock=False),
            active_slot=context.Value("i", 0, lock=False),
            frame_ready=context.Event(),
            metadata_lock=context.Lock(),
        )
        self._frames = np.ndarray(
            (2, *shape),
            dtype=np.uint8,
            buffer=self._shared_memory.buf,
        )
        self._frames.fill(0)

    def read_latest(self, retries: int = 3) -> FrameSnapshot | None:
        if not self.state.frame_ready.is_set():
            return None
        for _attempt in range(retries):
            with self.state.metadata_lock:
                sequence = int(self.state.sequence.value)
                timestamp_ns = int(self.state.timestamp_ns.value)
                slot = int(self.state.active_slot.value)
            frame = self._frames[slot].copy()
            with self.state.metadata_lock:
                unchanged = (
                    sequence == int(self.state.sequence.value)
                    and slot == int(self.state.active_slot.value)
                )
            if unchanged:
                return FrameSnapshot(sequence, timestamp_ns, frame)
        return None

    def close(self) -> None:
        self._shared_memory.close()
        self._shared_memory.unlink()


class SharedFrameWriter:
    def __init__(self, state: SharedFrameState) -> None:
        self.state = state
        self._frame_ready_published = False
        self._shared_memory = shared_memory.SharedMemory(
            name=state.shared_memory_name
        )
        self._frames = np.ndarray(
            (2, *state.shape),
            dtype=np.uint8,
            buffer=self._shared_memory.buf,
        )

    def publish(self, frame: np.ndarray, timestamp_ns: int) -> int:
        if frame.shape != self.state.shape or frame.dtype != np.uint8:
            raise ValueError(
                f"Frame must have shape={self.state.shape} and dtype=uint8"
            )
        with self.state.metadata_lock:
            next_slot = 1 - int(self.state.active_slot.value)
        np.copyto(self._frames[next_slot], frame)
        with self.state.metadata_lock:
            self.state.active_slot.value = next_slot
            self.state.timestamp_ns.value = timestamp_ns
            self.state.sequence.value += 1
            sequence = int(self.state.sequence.value)
            if not self._frame_ready_published:
                self.state.frame_ready.set()
                self._frame_ready_published = True
        return sequence

    def close(self) -> None:
        self._shared_memory.close()
