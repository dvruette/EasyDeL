# Copyright 2025 The EasyDeL Author @erfanzar (Erfan Zare Chavoshi).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
import dataclasses
import os
import signal
import threading
import traceback
import typing as tp
import uuid
from asyncio import futures
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field

import jax
import msgspec
import numpy as np
from eformer.pytree import auto_pytree
from jax import numpy as jnp

from easydel.layers.caching import PagesCache, TransformerCache
from easydel.utils.helpers import get_logger

from ..sampling_params import JitableSamplingParams, SamplingParams

if tp.TYPE_CHECKING:
    from easydel.infra.utils import ProcessingClassType


V = tp.TypeVar("V")


class SlotData(tp.NamedTuple):
    """Represents the output data for a single inference slot.

    This structure holds the generated tokens, their validity flags, and the
    current sequence length for one specific slot within a batch processed
    by the engine.

    Attributes:
        tokens: The generated token IDs for the slot (JAX or NumPy array).
                Shape typically (samples_per_slot, num_speculative_tokens).
        valid: A boolean array indicating the validity of each generated token
               (JAX or NumPy array). Shape matches `tokens`.
        lengths: An array containing the current length(s) of the generated
                 sequence(s) for the slot (JAX or NumPy array). Shape
                 typically (samples_per_slot,).
    """

    tokens: jax.Array | np.ndarray
    valid: jax.Array | np.ndarray
    lengths: jax.Array | np.ndarray


class ResultTokens(tp.NamedTuple):
    """Stores the results of a generation step (prefill or decode).

    This structure holds token data, validity flags, and sequence lengths
    concatenated into a single array (`data`) for efficient host transfer.
    Index tuples (`tokens_idx`, `valid_idx`, `length_idx`) specify the slices
    within `data` corresponding to each type of information. This is designed
    to minimize the number of device-to-host transfers.

    Attributes:
        data: A single JAX or NumPy array containing concatenated token IDs,
            validity flags, and lengths for the entire batch. Shape typically
            (batch_size * samples_per_slot, concatenated_data_width).
        tokens_idx: A tuple (start, end) indicating the column slice for token IDs
                    within the `data` array.
        valid_idx: A tuple (start, end) indicating the column slice for validity flags
                   within the `data` array.
        length_idx: A tuple (start, end) indicating the column slice for sequence lengths
                    within the `data` array.
        samples_per_slot: The number of samples generated per inference slot (e.g., 1).
                          Used by `get_result_at_slot` to extract data correctly.
    """

    data: jax.Array | np.ndarray
    tokens_idx: tuple[int, int]
    valid_idx: tuple[int, int]
    length_idx: tuple[int, int]
    samples_per_slot: int

    def copy_to_host_async(self: ResultTokens) -> None:
        """Initiates an asynchronous copy of the `data` array to the host CPU.

        If the data is already a NumPy array, this is a no-op.
        """
        if isinstance(self.data, np.ndarray):
            return
        self.data.copy_to_host_async()

    def convert_to_numpy(self: ResultTokens) -> ResultTokens:
        """Converts the internal `data` array to a NumPy array synchronously.

        Returns:
            A new ResultTokens instance with the data as a NumPy array.
        """
        return ResultTokens(
            np.array(self.data),
            self.tokens_idx,
            self.valid_idx,
            self.length_idx,
            self.samples_per_slot,
        )

    def get_result_at_slot(self, slot: int) -> SlotData:
        """Extracts the generation results for a specific inference slot.

        Args:
            slot: The index of the inference slot (0-based) for which to retrieve data.

        Returns:
            A SlotData object containing the tokens, validity, and lengths for the
            requested slot.

        Note:
            This method correctly handles potential microbatching by using
            `samples_per_slot` to calculate the correct indices within the `data` array.
        """
        start_idx = slot * self.samples_per_slot
        end_idx = (slot + 1) * self.samples_per_slot
        return SlotData(
            tokens=self.data[start_idx:end_idx, self.tokens_idx[0] : self.tokens_idx[1]],
            valid=self.data[start_idx:end_idx, self.valid_idx[0] : self.valid_idx[1]],
            lengths=self.data[start_idx:end_idx, self.length_idx[0] : self.length_idx[1]][:, 0],
        )

    def __str__(self):
        return f"ResultTokens(data={self.data})"


@auto_pytree
class GenerationState:
    """Holds the mutable state required for iterative token generation.

    This state is passed between consecutive `prefill` and `decode` steps,
    carrying information like the KV cache, the last generated tokens, and
    current sequence positions. It's decorated with `@auto_pytree` to allow
    it to be seamlessly used within JAX transformations like `jax.jit` / `ejit`.

    Attributes:
        logits: The logits output from the model for the last generated token(s)
                in the batch. Shape: (batch_size, vocab_size).
        cache: The key-value cache (e.g., TransformerCache) holding past attention
               states. This is typically updated in-place during generation.
        index: The current generation index (position) within the sequence for
               each item in the batch. Shape: (batch_size, 1).
        tokens: The last generated token IDs for each sequence in the batch.
                Shape: (batch_size, 1).
        valids: A boolean array indicating valid positions in the input sequences,
                used for attention masking. Shape: (batch_size, max_length).
        next_position_ids: The position IDs to be used for the *next* generation
                           step for each sequence. Shape: (batch_size, 1).
        generated_tokens: A counter for the number of tokens generated so far for
                          each sequence in the batch. Shape: (batch_size, 1).
    """

    cache: TransformerCache | PagesCache
    index: jax.Array
    logits: jax.Array
    tokens: jax.Array
    valids: jax.Array
    next_position_ids: jax.Array
    generated_tokens: jax.Array
    sampling_params: JitableSamplingParams


@dataclasses.dataclass
class ReturnSample:
    """Represents a single generated sample with text, token IDs, and metrics.

    This dataclass encapsulates the output for one sample (sequence) from a
    generation step, including the detokenized text, the raw token IDs, and
    performance metrics like tokens per second and the cumulative number of
    generated tokens.

    Attributes:
      text: A list of string pieces detokenized from the token IDs. This can be
            a single string or a list of strings if dealing with byte tokens
            or streaming output.
      token_ids: A list of integer token IDs generated in this step.
      tokens_per_second: The cumulative tokens per second achieved for this sample
                         up to the current generation step. Optional.
      num_generated_tokens: The cumulative number of tokens generated for this
                            sample since the start of the decode phase. Optional.
    """

    text: list[str] | str
    token_ids: list[int]
    time_spent_computing: float = 0.0
    accumulated_text: list[str] | str = None
    tokens_per_second: float | None = dataclasses.field(default=None)
    num_generated_tokens: int | None = dataclasses.field(default=None)
    generation_idx: int | None = dataclasses.field(default=None)


class _Exception:
    """A class for propagating exceptions through a queue.

    By wrapping them with a custom private class we ensure that any type
    (including Exception) can be used as a V.
    """

    def __init__(self, exception: Exception) -> None:
        self.exception = exception


class AsyncMultifuture(tp.Generic[V]):
    """AsyncMultifuture is like concurrent.futures.Future but supports returning

    multiple results. It provides an unidirectional stream with buffering and
    exception propagation.

    Supports delivering results to an async Python event loop. Must be
    constructed inside of the event loop.
    """

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._done = threading.Event()
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue[V | _Exception]()

    def cancel(self, unused: tp.Any = None) -> None:
        """Cancels the asyncmultifuture."""
        del unused
        self._cancelled.set()
        self.set_exception(futures.CancelledError())

    def cancelled(self) -> bool:
        """Returns whether the asyncmultifuture has been cancelled."""
        return self._cancelled.is_set()

    def done(self) -> bool:
        """AsyncMultifuture is done when it is finalized with close() or

        set_exception().
        """
        return self._done.is_set()

    def set_exception(self, exception: Exception) -> None:
        """Stores the given exception in the asyncmultifuture.

        The exception would be delivered after all previously added results are
        yielded. set_exception can be called multiple times, however subsequent
        calls will be ignored.

        Args:
          exception: The exception to set.
        """
        self._loop.call_soon_threadsafe(self._queue.put_nowait, _Exception(exception))
        self._loop.call_soon_threadsafe(self._done.set)

    def add_result(self, result: V) -> None:
        """Adds the result to the asyncmultifuture.

        Caller must call .close() once all results are added.

        Args:
          result: The result to add.
        """
        self._loop.call_soon_threadsafe(self._queue.put_nowait, result)

    def close(self) -> None:
        """Notifies the receiver that no more results would be added."""
        self.set_exception(StopAsyncIteration())

    def __aiter__(self) -> AsyncMultifuture:
        return self

    async def __anext__(self) -> V:
        """Returns the next value."""
        value = await self._queue.get()
        if isinstance(value, _Exception):
            raise value.exception
        return value


@dataclass
class ActiveRequestMetadata:
    """Inference request metadata."""

    start_time: float | None = None

    prefill_enqueue_time: float | None = None
    prefill_dequeue_time: float | None = None

    transfer_enqueue_time: float | None = None
    transfer_dequeue_time: float | None = None

    generate_enqueue_time: float | None = None
    generate_dequeue_time: float | None = None

    complete_time: float | None = None


@dataclass
class ActiveRequest:
    """Current state of the driver."""

    return_channel: AsyncMultifuture[list[ReturnSample]]

    sampling_params: SamplingParams

    complete: np.ndarray | None = None

    prefill_result: tp.Any = None
    prefill_content: str | list[int] | None = None
    prefill_tokens_processed: int | None = None
    prefill_tokens_remaining: list[int] | None = None  # Remaining token IDs to process
    is_prefill_complete: bool = False
    current_seq_id: int = -1  # For paged attention

    generate_timestep_added: int | None = None
    is_client_side_tokenization: bool | None = False

    # Metrics Tracking
    decode_start_time: float | None = None
    total_generated_tokens: int = 0
    metadata: ActiveRequestMetadata = field(default_factory=ActiveRequestMetadata)

    id: str = field(default_factory=uuid.uuid4)

    accumulated_text: str | list[str] | None = None

    _token_ids: list[int] | None = None
    _attention_mask: np.ndarray | None = None

    def enqueue_samples(self, generated_samples: list[ReturnSample]):
        """Adds the generated sample(s) to return channel for current step.

        Args:
          generated_samples: The generated sample(s) for current step.

        This should be called only from within the Drivers background thread.
        """
        self.return_channel.add_result(generated_samples)


class SafeThread(threading.Thread):
    """Thread that kills the program if it fails.

    If a driver thread goes down, we can't operate.
    """

    def run(self):
        """Executes the thread's target function.

        If the target function raises any exception, this method catches it,
        prints the traceback, and forcefully kills the entire process using
        `os.kill` with `signal.SIGKILL`. This ensures that if a critical
        driver thread fails, the whole system stops, preventing potential
        inconsistent states or hangs.
        """
        try:
            super().run()
        except Exception as e:
            print(f"Thread {self.name} encountered an error: {e}")
            traceback.print_exc()
            os.kill(os.getpid(), signal.SIGKILL)


def process_result_tokens(
    processor: ProcessingClassType,
    slot: int,
    slot_max_length: int,
    result_tokens: ResultTokens,
    complete: np.ndarray,
    eos_token_id: list[int],
    is_client_side_tokenization: bool = False,
    ignore_eos: bool = False,
) -> tuple[list[ReturnSample], np.ndarray, list[int]]:
    """
    Processes the result tokens for a given slot, extracts text and token IDs,
    updates completion status, and counts valid tokens generated in this step.

    Args:
        processor: The tokenizer/processor instance.
        slot: The index of the inference slot being processed.
        slot_max_length: The maximum allowed length for the sequence in this slot.
        result_tokens: The ResultTokens object containing the generated tokens and metadata.
        complete: A boolean NumPy array indicating the completion status of each sample in the batch.
        is_client_side_tokenization: A boolean indicating if tokenization is handled client-side.

    Returns:
        A tuple containing:
            - A list of ReturnSample objects (without TPS/count populated yet).
            - The updated completion status array.
            - A list containing the number of valid tokens generated in this step
              for each corresponding ReturnSample.
    """
    slot_data = result_tokens.get_result_at_slot(slot)
    slot_tokens = slot_data.tokens
    slot_valid = slot_data.valid
    slot_lengths = slot_data.lengths
    samples, speculations = slot_tokens.shape

    if isinstance(eos_token_id, int):
        eos_token_id = [eos_token_id]
    complete = complete | (slot_lengths > slot_max_length)
    return_samples = []
    num_valid_tokens_step = []  # Track valid tokens generated in this step per sample
    for idx in range(samples):
        text_so_far = []
        tok_id_so_far = []
        valid_tokens_count = 0
        if not complete[idx].item():
            for spec_idx in range(speculations):
                tok_id = slot_tokens[idx, spec_idx].item()
                valid = slot_valid[idx, spec_idx].item()
                if (tok_id in eos_token_id and not ignore_eos) or not valid:
                    complete[idx] = True
                    if valid and tok_id in eos_token_id:
                        tok_id_so_far.append(tok_id)
                        valid_tokens_count += 1
                    break
                else:
                    if not is_client_side_tokenization:
                        text_so_far.append(processor.decode([tok_id], skip_special_tokens=True))
                    tok_id_so_far.append(tok_id)
                    valid_tokens_count += 1
        return_samples.append(ReturnSample(text=text_so_far, token_ids=tok_id_so_far))
        num_valid_tokens_step.append(valid_tokens_count)
    return return_samples, complete, num_valid_tokens_step


def tokenize_and_pad(
    string: str,
    processor: ProcessingClassType,
    is_bos: bool = True,
    prefill_lengths: list[int] | None = None,
    max_prefill_length: int | None = None,
    jax_padding: bool = True,
) -> tuple[jax.Array | np.ndarray, jax.Array | np.ndarray, int]:
    """Tokenizes an input string and pads it to a suitable length.

    Uses the provided processor to tokenize the input string, then pads the
    resulting token IDs and attention mask (valids) to the nearest length
    specified in `prefill_lengths` or up to `max_prefill_length`. Optionally
    prepends the BOS token.

    Args:
        string: The input string to tokenize.
        processor: The tokenizer/processor object.
        is_bos: Whether to prepend the beginning-of-sequence (BOS) token.
            Defaults to True. (Note: BOS handling seems missing in the
            current `pad_tokens` implementation called internally).
        prefill_lengths: A list of bucket lengths to pad to. If None, uses
            `DEFAULT_PREFILL_BUCKETS`.
        max_prefill_length: The maximum allowed prefill length. Overrides
            buckets larger than this value.
        jax_padding: If True, returns JAX arrays; otherwise, returns NumPy arrays.
            Defaults to True.

    Returns:
        A tuple containing:
            - padded_tokens: The padded token ID array (JAX or NumPy).
            - padded_valids: The padded attention mask array (JAX or NumPy).
            - padded_length: The length to which the arrays were padded/truncated.
    """
    content = processor(
        string,
        return_tensors="np",
        return_attention_mask=True,
    )
    tokens = np.array(content["input_ids"])
    valids = np.array(content["attention_mask"])
    bos_token_id = processor.bos_token_id
    pad_token_id = processor.pad_token_id

    padded_tokens, padded_valids, padded_length = pad_tokens(
        tokens=tokens,
        valids=valids,
        bos_token_id=bos_token_id,
        pad_token_id=pad_token_id,
        is_bos=is_bos,
        prefill_lengths=prefill_lengths,
        max_prefill_length=max_prefill_length,
        jax_padding=jax_padding,
    )
    return padded_tokens, padded_valids, padded_length


DEFAULT_PREFILL_BUCKETS = [2**s for s in range(5, 24)]


def take_nearest_length(lengths: list[int], length: int) -> int:
    """Gets the nearest length to the right in a set of lengths.

    Uses binary search to find the smallest length in the `lengths` list that is
    greater than or equal to the input `length`.

    Args:
        lengths: A sorted list of integer lengths (e.g., prefill buckets).
        length: The target length to find the nearest value for.

    Returns:
        The nearest length in `lengths` that is greater than or equal to `length`.
        If `length` is greater than all lengths in the list, returns the largest length.
    """
    pos = bisect_left(lengths, length)
    if pos == len(lengths):
        return lengths[-1]
    return lengths[pos]


def pad_tokens(
    tokens: np.ndarray,
    valids: np.ndarray,
    pad_token_id: int,
    prefill_lengths: list[int] | None = None,
    max_prefill_length: int | None = None,
    jax_padding: bool = True,
    right_padding: bool = False,
    bos_token_id: int | None = None,  # Added for clarity, though not used
    is_bos: bool = True,  # Added for clarity, though not used
) -> tuple[jax.Array | np.ndarray, jax.Array | np.ndarray, int]:
    """Pads token and validity arrays to a specified bucket length.

    Takes 1D NumPy arrays of token IDs and validity masks, determines the
    nearest appropriate padding length from `prefill_lengths` (or capped by
    `max_prefill_length`), and pads or truncates the arrays to that length.
    Padding uses the `pad_token_id` for tokens and 0 for validity.

    Note: The `bos_token_id` and `is_bos` arguments are included for potential
    future use or consistency with `tokenize_and_pad`, but they are not
    currently used within this function's logic. BOS token handling should
    be done before calling this function if required.

    Args:
        tokens: A 1D NumPy array of token IDs.
        valids: A 1D NumPy array representing the attention mask (1 for valid,
            0 for padding). Must be the same size as `tokens`.
        pad_token_id: The token ID used for padding.
        prefill_lengths: A list of integer bucket lengths to choose from.
            Defaults to `DEFAULT_PREFILL_BUCKETS`.
        max_prefill_length: An optional maximum length. If provided, buckets
            larger than this are ignored, and this value is used as the maximum
            padding length.
        jax_padding: If True, converts the padded NumPy arrays to JAX arrays
            before returning. Defaults to True.
        bos_token_id: The beginning-of-sequence token ID (currently unused).
        is_bos: Flag indicating if BOS token handling is expected (currently unused).

    Returns:
        A tuple containing:
            - padded_tokens: The padded/truncated token ID array (JAX or NumPy).
            - padded_valids: The padded/truncated validity mask array (JAX or NumPy).
            - padded_length: The length to which the arrays were padded/truncated.
    """
    if prefill_lengths is None:
        prefill_lengths = DEFAULT_PREFILL_BUCKETS
    if max_prefill_length is not None:
        prefill_lengths = [*prefill_lengths[: prefill_lengths.index(max_prefill_length)], max_prefill_length]
    tokens = tokens.ravel()  # 1d Only
    valids = valids.ravel()
    true_length = tokens.shape[-1]
    assert valids.size == tokens.size
    padded_length = take_nearest_length(prefill_lengths, true_length)
    padding = padded_length - true_length

    if padding < 0:
        padded_tokens = tokens[-padded_length:]
        padded_valids = valids[-padded_length:]
    else:
        paddin = (0, padding) if right_padding else (padding, 0)
        padded_tokens = np.pad(tokens, paddin, constant_values=(pad_token_id,))
        padded_valids = np.pad(valids, paddin, constant_values=(0,))

    if jax_padding:
        padded_tokens = jnp.array([padded_tokens])
        padded_valids = jnp.array([padded_valids])

    if true_length > padded_tokens.shape[-1]:
        true_length = padded_tokens.shape[-1]

    return padded_tokens, padded_valids, true_length


def is_byte_token(s: str) -> bool:
    """Returns True if s is a byte string like "<0xAB>".

    These tokens represent raw bytes and are used in some tokenization schemes
    to handle multi-byte characters or special symbols.

    Args:
        s: The input string to check.

    Returns:
        True if the string matches the byte token format "<0xXX>", False otherwise.
    """
    if len(s) != 6 or s[0:3] != "<0x" or s[-1] != ">":
        return False
    return True


def text_tokens_to_string(text_tokens: tp.Iterable[str]) -> str:
    """Converts an iterable of text tokens, including byte tokens, to a string.

    This function handles tokens that represent raw bytes (e.g., "<0xAB>")
    correctly by converting them to their byte values before decoding the
    entire sequence of bytes into a UTF-8 string. This is necessary for
    tokenizers that output byte tokens for special characters or multi-byte
    sequences.

    Iterates through text tokens. If a token represents a byte (e.g., "<0xAB>"),
    it's converted to its byte value. Otherwise, the token is treated as a
    UTF-8 string and converted to bytes. All resulting bytes are joined and
    decoded back into a single UTF-8 string, replacing errors.

    Args:
        text_tokens: An iterable (e.g., list) of string tokens, which may include
                     byte tokens in the format "<0xXX>".

    Returns:
        The decoded string representation of the token sequence.
    """
    bytes_so_far = []
    for text_token in text_tokens:
        if is_byte_token(text_token):
            bytes_so_far.append(bytes([int(text_token[1:-1], 16)]))
        else:
            bytes_so_far.append(bytes(text_token, "utf-8"))
    return b"".join(bytes_so_far).decode("utf-8", "replace")


def calculate_pefill_lengths(max_prefill_length: int, num_pages: int = 128):
    allowed_calls = {1 << i for i in range(4, 32)}
    return sorted(
        {i * num_pages for i in range(1, (max_prefill_length // num_pages) + 1) if (i * num_pages) in allowed_calls}
        | {max_prefill_length}
    )


class LogprobsLists(tp.NamedTuple):
    logprob_token_ids: list[list[int]]
    logprobs: list[list[float]]
    sampled_token_ranks: list[int]

    def slice(self, start: int, end: int):
        return LogprobsLists(
            self.logprob_token_ids[start:end],
            self.logprobs[start:end],
            self.sampled_token_ranks[start:end],
        )


class LogprobsTensors(tp.NamedTuple):
    logprob_token_ids: jax.Array
    logprobs: jax.Array
    selected_token_ranks: jax.Array

    def tolists(self):
        return LogprobsLists(self.logprob_token_ids.tolist(), self.logprobs.tolist(), self.selected_token_ranks.tolist())

    @staticmethod
    def empty(num_positions: int, num_tokens_per_position: int) -> LogprobsTensors:
        logprob_token_ids = jnp.empty((num_positions, num_tokens_per_position), dtype="i4")
        logprobs = jnp.empty_like(logprob_token_ids, dtype=jnp.float32)
        selected_token_ranks = jnp.empty(num_positions, dtype="i4")
        return LogprobsTensors(
            logprob_token_ids=logprob_token_ids,
            logprobs=logprobs,
            selected_token_ranks=selected_token_ranks,
        )


@dataclass
class PrefixCacheStats:
    """Stores prefix cache hit statistics."""

    reset: bool = False
    requests: int = 0
    queries: int = 0
    hits: int = 0


@dataclass
class SpecDecodingStats:
    """Per-step iteration decoding stats from scheduler.

    Each scheduler step, statistics on spec decoding performance are
    aggregated across requests by the scheduler and returned to the
    frontend in EngineCoreOutputs->SchedulerStats.
    """

    num_spec_tokens: int
    num_drafts: int = 0
    num_draft_tokens: int = 0
    num_accepted_tokens: int = 0
    num_accepted_tokens_per_pos: list[int] = field(default_factory=list)

    @classmethod
    def new(cls, num_spec_tokens: int) -> SpecDecodingStats:
        return cls(num_spec_tokens=num_spec_tokens, num_accepted_tokens_per_pos=[0] * num_spec_tokens)

    def observe_draft(self, num_draft_tokens: int, num_accepted_tokens: int):
        self.num_drafts += 1
        self.num_draft_tokens += num_draft_tokens
        self.num_accepted_tokens += num_accepted_tokens
        assert num_accepted_tokens <= self.num_spec_tokens
        for i in range(num_accepted_tokens):
            self.num_accepted_tokens_per_pos[i] += 1


@dataclass
class SchedulerStats:
    """Stats associated with the scheduler."""

    num_running_reqs: int = 0
    num_waiting_reqs: int = 0
    kv_cache_usage: float = 0.0
    prefix_cache_stats: PrefixCacheStats = field(default_factory=PrefixCacheStats)
    spec_decoding_stats: SpecDecodingStats | None = None


def is_list_of(
    value: object,
    typ: type[V] | tuple[type[V], ...],
    *,
    check: tp.Literal["first", "all"] = "first",
) -> list[V]:
    if not isinstance(value, list):
        return False

    if check == "first":
        return len(value) == 0 or isinstance(value[0], typ)
    elif check == "all":
        return all(isinstance(v, typ) for v in value)

    tp.assert_never(check)


class ConstantList(tp.Generic[V], Sequence):
    def __init__(self, x: list[V]) -> None:
        self._x = x

    def append(self, item):
        raise Exception("Cannot append to a constant list")

    def extend(self, item):
        raise Exception("Cannot extend a constant list")

    def insert(self, item):
        raise Exception("Cannot insert into a constant list")

    def pop(self, item):
        raise Exception("Cannot pop from a constant list")

    def remove(self, item):
        raise Exception("Cannot remove from a constant list")

    def clear(self):
        raise Exception("Cannot clear a constant list")

    def index(self, item: V, start: int = 0, stop: int | None = None) -> int:
        return self._x.index(item, start, stop if stop is not None else len(self._x))

    @tp.overload
    def __getitem__(self, item: int) -> V: ...

    @tp.overload
    def __getitem__(self, s: slice, /) -> list[V]: ...

    def __getitem__(self, item: int | slice) -> V | list[V]:
        return self._x[item]

    @tp.overload
    def __setitem__(self, item: int, value: V): ...

    @tp.overload
    def __setitem__(self, s: slice, value: V, /): ...

    def __setitem__(self, item: int | slice, value: V | list[V]):
        raise Exception("Cannot set item in a constant list")

    def __delitem__(self, item):
        raise Exception("Cannot delete item from a constant list")

    def __iter__(self):
        return iter(self._x)

    def __contains__(self, item):
        return item in self._x

    def __len__(self):
        return len(self._x)

    def __repr__(self):
        return f"ConstantList({self._x})"


class UtilityOutput(msgspec.Struct, array_like=True, gc=False):
    call_id: int
    failure_message: str | None = None
    result: tp.Any = None


class MetricsRecorder:
    """
    Records and provides access to various operational metrics.

    This class is responsible for collecting time-series data for various
    durations, counts, and queue sizes within the vDriver system. It provides
    methods to update these metrics and retrieve them in raw or aggregated forms.

    Attributes:
        metrics (dict): A dictionary holding all recorded metrics.
        metrics_log_interval_sec (float): Interval for logging metrics by a monitor.
        _lock (threading.Lock): A lock to ensure thread-safe updates to metrics.
        _max_list_len (int): Maximum length for lists storing time-series data
                             to prevent unbounded memory growth.
    """

    def __init__(self, metrics_log_interval_sec: float = 10.0):
        """
        Initializes the MetricsRecorder.

        Args:
            metrics_log_interval_sec (float): The interval in seconds at which
                a monitoring thread might log these metrics. Defaults to 10.0.
        """
        self.metrics = {
            "queue_sizes": {},
            "active_requests_count": 0,
            "ttft_ms": [],
            "prefill_op_ms": [],
            "decode_op_ms": [],
            "insert_op_ms": [],
            "transfer_op_ms": [],
            "operation_lock_wait_ms": [],
            "prefill_ops_count": 0,
            "decode_ops_count": 0,
            "insert_ops_count": 0,
            "completed_requests_count": 0,
            "submitted_requests_count": 0,
        }
        self._lock = threading.Lock()
        self.metrics_log_interval_sec = metrics_log_interval_sec
        self._max_list_len = 1000

    def _append_to_list(self, key: str, value: float):
        """
        Appends a value to a list metric, ensuring it doesn't exceed max length.

        Args:
            key (str): The key of the list metric in `self.metrics`.
            value (float): The value to append.
        """
        lst = self.metrics.get(key, [])
        lst.append(value)
        self.metrics[key] = lst[-self._max_list_len :]

    def update_queue_size(self, queue_name: str, size: int):
        """
        Updates the recorded size for a specific queue.

        Args:
            queue_name (str): The name of the queue.
            size (int): The current size of the queue.
        """
        with self._lock:
            self.metrics["queue_sizes"][queue_name] = size

    def set_active_requests_count(self, count: int):
        """
        Sets the current count of active requests.

        Args:
            count (int): The number of currently active requests.
        """
        with self._lock:
            self.metrics["active_requests_count"] = count

    def record_ttft(self, ttft_ms: float):
        """
        Records a Time To First Token (TTFT) duration.

        Args:
            ttft_ms (float): The TTFT duration in milliseconds.
        """
        with self._lock:
            self._append_to_list("ttft_ms", ttft_ms)

    def record_prefill_op_time(self, duration_ms: float):
        """
        Records the duration of a prefill operation and increments its count.

        Args:
            duration_ms (float): The prefill operation duration in milliseconds.
        """
        with self._lock:
            self._append_to_list("prefill_op_ms", duration_ms)
            self.metrics["prefill_ops_count"] += 1

    def record_decode_op_time(self, duration_ms: float):
        """
        Records the duration of a decode operation and increments its count.

        Args:
            duration_ms (float): The decode operation duration in milliseconds.
        """
        with self._lock:
            self._append_to_list("decode_op_ms", duration_ms)
            self.metrics["decode_ops_count"] += 1

    def record_insert_op_time(self, duration_ms: float):
        """
        Records the duration of an insert operation and increments its count.

        Args:
            duration_ms (float): The insert operation duration in milliseconds.
        """
        with self._lock:
            self._append_to_list("insert_op_ms", duration_ms)
            self.metrics["insert_ops_count"] += 1

    def record_transfer_op_time(self, duration_ms: float):
        """
        Records the duration of a transfer operation.

        Args:
            duration_ms (float): The transfer operation duration in milliseconds.
        """
        with self._lock:
            self._append_to_list("transfer_op_ms", duration_ms)

    def record_operation_lock_wait_time(self, duration_ms: float):
        """
        Records the time spent waiting for an operation lock.

        Args:
            duration_ms (float): The lock wait duration in milliseconds.
        """
        with self._lock:
            self._append_to_list("operation_lock_wait_ms", duration_ms)

    def increment_completed_requests(self):
        """Increments the count of completed requests."""
        with self._lock:
            self.metrics["completed_requests_count"] += 1

    def increment_submitted_requests(self):
        """Increments the count of submitted requests."""
        with self._lock:
            self.metrics["submitted_requests_count"] += 1

    def get_all_metrics(self) -> dict:
        """
        Returns a deep copy of all currently recorded metrics.

        Returns:
            dict: A copy of the metrics dictionary.
        """
        with self._lock:
            copied_metrics = {}
            for k, v in self.metrics.items():
                if isinstance(v, list):
                    copied_metrics[k] = list(v)
                elif isinstance(v, dict):
                    copied_metrics[k] = dict(v)
                else:
                    copied_metrics[k] = v
            return copied_metrics

    def get_aggregated_metrics_snapshot(self, window_size=100) -> dict:
        """
        Returns a snapshot of aggregated metrics.

        For list-based metrics (e.g., durations), it calculates average,
        percentiles (p50, p90, p99), min, and max over a specified window
        of recent samples.

        Args:
            window_size (int): The number of recent samples to use for
                aggregation. If 0, all samples are used. Defaults to 100.

        Returns:
            dict: A dictionary of aggregated metrics.
        """
        snapshot = self.get_all_metrics()
        aggregated = {}
        for key, value in snapshot.items():
            if isinstance(value, list) and value:
                sample = value[-window_size:] if window_size > 0 else value
                if sample:
                    aggregated[f"{key}_avg"] = round(np.mean(sample), 2)
                    aggregated[f"{key}_p50"] = round(np.percentile(sample, 50), 2)
                    aggregated[f"{key}_p90"] = round(np.percentile(sample, 90), 2)
                    aggregated[f"{key}_p99"] = round(np.percentile(sample, 99), 2)
                    aggregated[f"{key}_min"] = round(np.min(sample), 2)
                    aggregated[f"{key}_max"] = round(np.max(sample), 2)
                    aggregated[f"{key}_count_total"] = snapshot.get(f"{key.split('_ms')[0]}_ops_count", len(value))
                    aggregated[f"{key}_count_window"] = len(sample)
            elif isinstance(value, dict):
                aggregated[key] = dict(value)
            elif isinstance(value, int | float):
                aggregated[key] = value
        return aggregated


logger = get_logger("vSurge-Utils")


class SmartBytecodeDecoder:
    """A smart decoder that handles partial token sequences and recovers from malformed characters."""

    def __init__(self, processor, fallback_char: str = ""):
        self.processor = processor
        self.fallback_char = fallback_char
        self.malformed_indicators = {"�", "\\ufffd", "\ufffd"}  # noqa

    def contains_malformed_chars(self, text: str) -> bool:
        """Check if text contains malformed Unicode characters."""
        return any(indicator in text for indicator in self.malformed_indicators)

    def decode_with_recovery(
        self,
        all_tokens: list[int],
        previous_good_text: str = "",
        buffer_tokens: list[int] | None = None,
    ) -> tuple[str, list[int], bool]:
        """Decode tokens with smart error recovery."""
        if not all_tokens:
            return "", [], False
        if buffer_tokens:
            tokens_to_decode = buffer_tokens + all_tokens
        else:
            tokens_to_decode = all_tokens
        try:
            full_decoded = self.processor.decode(tokens_to_decode, skip_special_tokens=True)
            if self.contains_malformed_chars(full_decoded):
                logger.debug("Malformed characters detected in full decode")
                return self._handle_malformed_decode(tokens_to_decode, previous_good_text)
            else:
                if previous_good_text and full_decoded.startswith(previous_good_text):
                    new_text = full_decoded[len(previous_good_text) :]
                else:
                    new_text = full_decoded
                return new_text, [], False

        except Exception as e:
            logger.debug(f"Decode error: {e}, attempting recovery")
            return self._handle_decode_error(tokens_to_decode, previous_good_text)

    def _handle_malformed_decode(self, tokens: list[int], previous_good_text: str) -> tuple[str, list[int], bool]:
        """Handle cases where decoding produces malformed characters."""
        if len(tokens) <= 1:
            return self.fallback_char, [], True
        for i in range(len(tokens) - 1, 0, -1):
            try:
                partial_decoded = self.processor.decode(tokens[:i], skip_special_tokens=True)
                if not self.contains_malformed_chars(partial_decoded):
                    if previous_good_text and partial_decoded.startswith(previous_good_text):
                        good_new_text = partial_decoded[len(previous_good_text) :]
                    else:
                        good_new_text = partial_decoded
                    remaining_tokens = tokens[i:]
                    logger.debug(f"Buffering {len(remaining_tokens)} tokens due to malformed chars")
                    return good_new_text, remaining_tokens, True

            except Exception:
                continue

        logger.warning("Could not find any good partial decode, using fallback")
        return self.fallback_char, [], True

    def _handle_decode_error(self, tokens: list[int], previous_good_text: str) -> tuple[str, list[int], bool]:
        """Handle decode exceptions by trying progressive decoding."""
        if len(tokens) <= 1:
            return self.fallback_char, [], True

        for i in range(len(tokens) - 1, 0, -1):
            try:
                partial_decoded = self.processor.decode(tokens[:i], skip_special_tokens=True)
                if previous_good_text and partial_decoded.startswith(previous_good_text):
                    good_new_text = partial_decoded[len(previous_good_text) :]
                else:
                    good_new_text = partial_decoded
                remaining_tokens = tokens[i:]
                logger.debug(f"Decode error recovery: buffering {len(remaining_tokens)} tokens")
                return good_new_text, remaining_tokens, True

            except Exception:
                continue

        logger.warning("Complete decode failure, using fallback")
        return self.fallback_char, [], True
