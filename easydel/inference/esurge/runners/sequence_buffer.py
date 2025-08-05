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


from dataclasses import field
from typing import Any, cast

import numpy as np
from eformer.pytree import auto_pytree

from easydel.utils.helpers import get_logger

from ...sampling_params import SamplingType
from ..outputs import LogprobsTensors, swap_dict_values
from ..page_table import MultiGroupPageTable

logger = get_logger(__name__)


class SequenceBuffer:
    def __init__(
        self,
        max_num_reqs: int,
        max_model_len: int,
        max_num_batched_tokens: int,
        vocab_size: int,
        page_sizes: list[int],
    ):
        self.max_num_reqs = max_num_reqs
        self.max_model_len = max_model_len
        self.max_num_batched_tokens = max_num_batched_tokens
        self.vocab_size = vocab_size

        self._req_ids: list[str | None] = []
        self.req_id_to_index: dict[str, int] = {}
        self.req_output_token_ids: list[list[int] | None] = []

        self.token_ids = np.zeros((max_num_reqs, max_model_len), dtype=np.int32)
        self.num_tokens = np.zeros(max_num_reqs, dtype=np.int32)
        self.num_tokens_no_spec = np.zeros(max_num_reqs, dtype=np.int32)
        self.num_prompt_tokens = np.zeros(max_num_reqs, dtype=np.int32)
        self.num_computed_tokens = np.zeros(max_num_reqs, dtype=np.int32)

        self.page_table = MultiGroupPageTable(
            max_num_reqs=max_num_reqs,
            max_model_len=max_model_len,
            max_num_batched_tokens=max_num_batched_tokens,
            page_sizes=page_sizes,
        )

        self._init_sampling_arrays()
        self._init_request_sets()
        self._init_sparse_storage()

    def _init_sampling_arrays(self):
        """Initialize sampling parameter arrays with appropriate dtypes."""
        self.temperature = np.full(self.max_num_reqs, -1.0, dtype=np.float32)
        self.top_p = np.ones(self.max_num_reqs, dtype=np.float32)
        self.top_k = np.full(self.max_num_reqs, self.vocab_size, dtype=np.int32)
        self.min_p = np.zeros(self.max_num_reqs, dtype=np.float32)
        self.frequency_penalties = np.zeros(self.max_num_reqs, dtype=np.float32)
        self.presence_penalties = np.zeros(self.max_num_reqs, dtype=np.float32)
        self.repetition_penalties = np.ones(self.max_num_reqs, dtype=np.float32)

    def _init_request_sets(self):
        """Initialize request tracking sets."""
        self.greedy_reqs: set[str] = set()
        self.random_reqs: set[str] = set()
        self.top_p_reqs: set[str] = set()
        self.top_k_reqs: set[str] = set()
        self.min_p_reqs: set[str] = set()
        self.frequency_penalties_reqs: set[str] = set()
        self.presence_penalties_reqs: set[str] = set()
        self.repetition_penalties_reqs: set[str] = set()
        self.has_allowed_token_ids: set[str] = set()

    def _init_sparse_storage(self):
        """Initialize sparse storage for less common parameters."""
        self.min_tokens: dict[int, tuple[int, set[int]]] = {}
        self.generator_seeds: dict[int, int] = {}
        self.num_logprobs: dict[str, int] = {}
        self.num_prompt_logprobs: dict[str, int] = {}
        self.in_progress_prompt_logprobs_cpu: dict[str, LogprobsTensors] = {}
        self.logit_bias: list[dict[int, float] | None] = [None] * self.max_num_reqs
        self.allowed_token_ids_mask: np.ndarray | None = None
        self.bad_words_token_ids: dict[int, list[list[int]]] = {}

    @property
    def req_ids(self) -> list[str]:
        return cast(list[str], self._req_ids)

    def add_request(self, request: Any, req_index: int | None = None) -> None:
        if req_index is None:
            req_index = self.num_reqs
        assert req_index < self.max_num_reqs

        req_id = request.req_id

        if req_index == len(self._req_ids):
            self._req_ids.append(req_id)
            self.req_output_token_ids.append(request.output_token_ids)
        else:
            self._req_ids[req_index] = req_id
            self.req_output_token_ids[req_index] = request.output_token_ids

        self.req_id_to_index[req_id] = req_index

        self._copy_tokens(request, req_index)

        self.num_tokens[req_index] = request.num_tokens
        self.num_tokens_no_spec[req_index] = request.num_tokens
        self.num_computed_tokens[req_index] = request.num_computed_tokens

        self.page_table.add_row(request.page_ids, req_index)

        sampling_params = request.sampling_params
        assert sampling_params is not None, "pooling requests not supported yet"
        self._process_sampling_params(sampling_params, req_id, req_index)

        self._process_optional_params(request, sampling_params, req_id, req_index)

    def _copy_tokens(self, request: Any, req_index: int) -> None:
        """Efficiently copy prompt and output tokens."""
        num_prompt_tokens = len(request.prompt_token_ids)
        self.num_prompt_tokens[req_index] = num_prompt_tokens

        self.token_ids[req_index, :num_prompt_tokens] = request.prompt_token_ids

        if request.output_token_ids:
            start_idx = num_prompt_tokens
            end_idx = start_idx + len(request.output_token_ids)
            self.token_ids[req_index, start_idx:end_idx] = request.output_token_ids

    def _process_sampling_params(self, sampling_params: Any, req_id: str, req_index: int) -> None:
        """Process core sampling parameters."""

        if sampling_params.sampling_type == SamplingType.GREEDY:
            self.temperature[req_index] = -1.0
            self.greedy_reqs.add(req_id)
        else:
            self.temperature[req_index] = sampling_params.temperature
            self.random_reqs.add(req_id)

        self.top_p[req_index] = sampling_params.top_p
        if sampling_params.top_p < 1:
            self.top_p_reqs.add(req_id)

        top_k = sampling_params.top_k
        if 0 < top_k < self.vocab_size:
            self.top_k_reqs.add(req_id)
            self.top_k[req_index] = top_k
        else:
            self.top_k[req_index] = self.vocab_size

        self.min_p[req_index] = sampling_params.min_p
        if sampling_params.min_p > 1e-5:
            self.min_p_reqs.add(req_id)

        if sampling_params.frequency_penalty != 0.0:
            self.frequency_penalties[req_index] = sampling_params.frequency_penalty
            self.frequency_penalties_reqs.add(req_id)

        if sampling_params.presence_penalty != 0.0:
            self.presence_penalties[req_index] = sampling_params.presence_penalty
            self.presence_penalties_reqs.add(req_id)

        if sampling_params.repetition_penalty != 1.0:
            self.repetition_penalties[req_index] = sampling_params.repetition_penalty
            self.repetition_penalties_reqs.add(req_id)

    def _process_optional_params(self, request: Any, sampling_params: Any, req_id: str, req_index: int) -> None:
        """Process optional/sparse parameters."""
        if sampling_params.min_tokens:
            self.min_tokens[req_index] = (sampling_params.min_tokens, sampling_params.all_stop_token_ids)

        if hasattr(request, "generator_seed") and request.generator_seed is not None:
            self.generator_seeds[req_index] = request.generator_seed

        if sampling_params.logprobs is not None:
            self.num_logprobs[req_id] = sampling_params.logprobs

        if sampling_params.prompt_logprobs is not None:
            self.num_prompt_logprobs[req_id] = sampling_params.prompt_logprobs

        if sampling_params.logit_bias is not None:
            self.logit_bias[req_index] = sampling_params.logit_bias

        if sampling_params.allowed_token_ids:
            self._set_allowed_token_ids(req_id, req_index, sampling_params.allowed_token_ids)

        if sampling_params.bad_words_token_ids:
            self.bad_words_token_ids[req_index] = sampling_params.bad_words_token_ids

    def _set_allowed_token_ids(self, req_id: str, req_index: int, allowed_token_ids: list[int]) -> None:
        """Efficiently set allowed token IDs mask."""
        self.has_allowed_token_ids.add(req_id)
        if self.allowed_token_ids_mask is None:
            self.allowed_token_ids_mask = np.zeros((self.max_num_reqs, self.vocab_size), dtype=bool)

        self.allowed_token_ids_mask[req_index] = True
        self.allowed_token_ids_mask[req_index, allowed_token_ids] = False

    def remove_request(self, req_id: str) -> int | None:
        """Remove a request. Must be followed by condense()."""
        req_index = self.req_id_to_index.pop(req_id, None)
        if req_index is None:
            return None

        self._req_ids[req_index] = None
        self.req_output_token_ids[req_index] = None

        for req_set in [
            self.greedy_reqs,
            self.random_reqs,
            self.top_p_reqs,
            self.top_k_reqs,
            self.min_p_reqs,
            self.frequency_penalties_reqs,
            self.presence_penalties_reqs,
            self.repetition_penalties_reqs,
            self.has_allowed_token_ids,
        ]:
            req_set.discard(req_id)

        self.min_tokens.pop(req_index, None)
        self.generator_seeds.pop(req_index, None)
        self.num_logprobs.pop(req_id, None)
        self.num_prompt_logprobs.pop(req_id, None)
        self.in_progress_prompt_logprobs_cpu.pop(req_id, None)
        self.logit_bias[req_index] = None
        self.bad_words_token_ids.pop(req_index, None)

        if self.allowed_token_ids_mask is not None:
            self.allowed_token_ids_mask[req_index] = False

        return req_index

    def swap_states(self, i1: int, i2: int) -> None:
        """Swap states between two indices."""

        old_id_i1, old_id_i2 = self._req_ids[i1], self._req_ids[i2]
        self._req_ids[i1], self._req_ids[i2] = old_id_i2, old_id_i1
        self.req_output_token_ids[i1], self.req_output_token_ids[i2] = (
            self.req_output_token_ids[i2],
            self.req_output_token_ids[i1],
        )

        assert old_id_i1 is not None and old_id_i2 is not None
        self.req_id_to_index[old_id_i1] = i2
        self.req_id_to_index[old_id_i2] = i1

        self._swap_array_values(i1, i2)

        swap_dict_values(self.generator_seeds, i1, i2)
        swap_dict_values(self.min_tokens, i1, i2)
        swap_dict_values(self.bad_words_token_ids, i1, i2)

        self.logit_bias[i1], self.logit_bias[i2] = self.logit_bias[i2], self.logit_bias[i1]
        self.page_table.swap_row(i1, i2)

    def _swap_array_values(self, i1: int, i2: int) -> None:
        """Efficiently swap array values between two indices."""

        scalar_arrays = [
            self.num_tokens,
            self.num_tokens_no_spec,
            self.num_prompt_tokens,
            self.num_computed_tokens,
            self.temperature,
            self.top_p,
            self.top_k,
            self.frequency_penalties,
            self.presence_penalties,
            self.repetition_penalties,
            self.min_p,
        ]

        for array in scalar_arrays:
            array[[i1, i2]] = array[[i2, i1]]

        self.token_ids[[i1, i2]] = self.token_ids[[i2, i1]]

        if self.allowed_token_ids_mask is not None:
            self.allowed_token_ids_mask[[i1, i2]] = self.allowed_token_ids_mask[[i2, i1]]

    def condense(self, empty_req_indices: list[int]) -> None:
        """Efficiently condense the buffer by moving requests to fill empty slots."""
        num_reqs = self.num_reqs
        if num_reqs == 0:
            self._req_ids.clear()
            self.req_output_token_ids.clear()
            return

        last_req_index = num_reqs + len(empty_req_indices) - 1

        for empty_index in reversed(empty_req_indices):
            while last_req_index in empty_req_indices and last_req_index > empty_index:
                last_req_index -= 1

            if empty_index >= last_req_index:
                continue

            self._move_request(last_req_index, empty_index)
            last_req_index -= 1

        del self._req_ids[self.num_reqs :]
        del self.req_output_token_ids[self.num_reqs :]

    def _move_request(self, from_idx: int, to_idx: int) -> None:
        """Move a request from one index to another."""
        req_id = self._req_ids[from_idx]
        assert req_id is not None

        self._req_ids[to_idx] = req_id
        self._req_ids[from_idx] = None
        self.req_output_token_ids[to_idx] = self.req_output_token_ids[from_idx]
        self.req_output_token_ids[from_idx] = None
        self.req_id_to_index[req_id] = to_idx

        num_tokens = self.num_tokens[from_idx].item()
        self.token_ids[to_idx, :num_tokens] = self.token_ids[from_idx, :num_tokens]

        scalar_arrays = [
            self.num_tokens,
            self.num_tokens_no_spec,
            self.num_prompt_tokens,
            self.num_computed_tokens,
            self.temperature,
            self.top_p,
            self.top_k,
            self.frequency_penalties,
            self.presence_penalties,
            self.repetition_penalties,
            self.min_p,
        ]

        for array in scalar_arrays:
            array[to_idx] = array[from_idx]

        self.page_table.move_row(from_idx, to_idx)

        self._move_sparse_data(from_idx, to_idx)

    def _move_sparse_data(self, from_idx: int, to_idx: int) -> None:
        """Move sparse/optional data from one index to another."""

        if from_idx in self.generator_seeds:
            self.generator_seeds[to_idx] = self.generator_seeds.pop(from_idx)

        if from_idx in self.min_tokens:
            self.min_tokens[to_idx] = self.min_tokens.pop(from_idx)

        if from_idx in self.bad_words_token_ids:
            self.bad_words_token_ids[to_idx] = self.bad_words_token_ids.pop(from_idx)

        self.logit_bias[to_idx] = self.logit_bias[from_idx]
        self.logit_bias[from_idx] = None

        if self.allowed_token_ids_mask is not None:
            self.allowed_token_ids_mask[to_idx] = self.allowed_token_ids_mask[from_idx]
            self.allowed_token_ids_mask[from_idx] = False

    def _make_prompt_token_ids_tensor(self) -> np.ndarray:
        """Create a padded tensor of prompt token IDs."""
        if self.num_reqs == 0:
            return np.empty((0, 0), dtype=np.int64)

        max_prompt_len = np.max(self.num_prompt_tokens[: self.num_reqs]).item()
        prompt_token_ids = np.full((self.num_reqs, max_prompt_len), self.vocab_size, dtype=np.int64)

        for i in range(self.num_reqs):
            num_prompt = self.num_prompt_tokens[i].item()
            prompt_token_ids[i, :num_prompt] = self.token_ids[i, :num_prompt]

        return prompt_token_ids

    @property
    def num_reqs(self) -> int:
        return len(self.req_id_to_index)

    @property
    def all_greedy(self) -> bool:
        return len(self.random_reqs) == 0

    @property
    def all_random(self) -> bool:
        return len(self.greedy_reqs) == 0

    @property
    def no_top_p(self) -> bool:
        return len(self.top_p_reqs) == 0

    @property
    def no_top_k(self) -> bool:
        return len(self.top_k_reqs) == 0

    @property
    def no_min_p(self) -> bool:
        return len(self.min_p_reqs) == 0

    @property
    def no_penalties(self) -> bool:
        return (
            len(self.presence_penalties_reqs) == 0
            and len(self.frequency_penalties_reqs) == 0
            and len(self.repetition_penalties_reqs) == 0
        )

    @property
    def max_num_logprobs(self) -> int | None:
        return max(self.num_logprobs.values()) if self.num_logprobs else None

    @property
    def no_prompt_logprob(self) -> bool:
        return not self.num_prompt_logprobs

    @property
    def no_allowed_token_ids(self) -> bool:
        return len(self.has_allowed_token_ids) == 0

    def get_request_indices_with_penalty(self) -> np.ndarray:
        """Get indices of requests that have any penalty applied."""
        penalty_req_ids = self.frequency_penalties_reqs | self.presence_penalties_reqs | self.repetition_penalties_reqs
        if not penalty_req_ids:
            return np.array([], dtype=np.int32)

        indices = [self.req_id_to_index[req_id] for req_id in penalty_req_ids]
        return np.array(indices, dtype=np.int32)

    def get_active_sampling_params(self, req_index: int) -> dict[str, Any]:
        """Get active sampling parameters for a specific request."""
        req_id = self._req_ids[req_index]
        if req_id is None:
            return {}

        params = {
            "temperature": self.temperature[req_index],
            "top_p": self.top_p[req_index],
            "top_k": self.top_k[req_index],
        }

        if req_id in self.min_p_reqs:
            params["min_p"] = self.min_p[req_index]
        if req_id in self.frequency_penalties_reqs:
            params["frequency_penalty"] = self.frequency_penalties[req_index]
        if req_id in self.presence_penalties_reqs:
            params["presence_penalty"] = self.presence_penalties[req_index]
        if req_id in self.repetition_penalties_reqs:
            params["repetition_penalty"] = self.repetition_penalties[req_index]

        return params

    def clear(self) -> None:
        """Clear all data in the buffer."""
        self._req_ids.clear()
        self.req_id_to_index.clear()
        self.req_output_token_ids.clear()

        self.token_ids.fill(0)
        self.num_tokens.fill(0)
        self.num_tokens_no_spec.fill(0)
        self.num_prompt_tokens.fill(0)
        self.num_computed_tokens.fill(0)

        self.temperature.fill(-1.0)
        self.top_p.fill(1.0)
        self.top_k.fill(self.vocab_size)
        self.min_p.fill(0.0)
        self.frequency_penalties.fill(0.0)
        self.presence_penalties.fill(0.0)
        self.repetition_penalties.fill(1.0)

        for req_set in [
            self.greedy_reqs,
            self.random_reqs,
            self.top_p_reqs,
            self.top_k_reqs,
            self.min_p_reqs,
            self.frequency_penalties_reqs,
            self.presence_penalties_reqs,
            self.repetition_penalties_reqs,
            self.has_allowed_token_ids,
        ]:
            req_set.clear()

        self.min_tokens.clear()
        self.generator_seeds.clear()
        self.num_logprobs.clear()
        self.num_prompt_logprobs.clear()
        self.in_progress_prompt_logprobs_cpu.clear()
        self.bad_words_token_ids.clear()

        self.logit_bias = [None] * self.max_num_reqs
        if self.allowed_token_ids_mask is not None:
            self.allowed_token_ids_mask.fill(False)


@auto_pytree
class ModelRunnerSamplingMetadata:
    temperature: np.ndarray
    min_p: np.ndarray
    top_k: np.ndarray
    top_p: np.ndarray

    all_greedy: bool = True
    logprobs: bool = False
    no_penalties: bool = True

    prompt_token_ids: Any = None
    frequency_penalties: Any = None
    presence_penalties: Any = None
    repetition_penalties: Any = None

    output_token_ids: list[list[int]] = field(default_factory=list)
    min_tokens: Any = None
    logit_bias: list[dict[int, float]] = field(default_factory=list)
    allowed_token_ids_mask: Any = None
    bad_words_token_ids: Any = None

    @classmethod
    def from_sequence_buffer(
        cls,
        sequence_buffer: SequenceBuffer,
        padded_num_reqs: int,
        generate_params_if_all_greedy: bool = False,
    ):
        """
        Copy sampling tensors slices from `sequence_buffer` to numpy arrays.

        Args:
            sequence_buffer: The input batch containing sampling parameters.
            padded_num_reqs: The padded number of requests.
            generate_params_if_all_greedy: If True, generate sampling parameters
                even if all requests are greedy.
        """

        if sequence_buffer.all_greedy is True and not generate_params_if_all_greedy:
            return cls(
                temperature=np.zeros((padded_num_reqs,)),
                min_p=np.zeros((padded_num_reqs,)),
                top_k=np.zeros((padded_num_reqs,)),
                top_p=np.zeros((padded_num_reqs,)),
                all_greedy=True,
                logprobs=False,
            )

        num_reqs = sequence_buffer.num_reqs

        def fill_slice(arr, fill_val):
            arr = arr.copy()
            arr[num_reqs:padded_num_reqs] = fill_val
            return arr

        return cls(
            temperature=fill_slice(sequence_buffer.temperature, -1.0)[:padded_num_reqs],
            min_p=fill_slice(sequence_buffer.min_p, 0.0)[:padded_num_reqs],
            top_k=fill_slice(sequence_buffer.top_k, 0)[:padded_num_reqs],
            top_p=fill_slice(sequence_buffer.top_p, 1.0)[:padded_num_reqs],
            all_greedy=sequence_buffer.all_greedy,
            logprobs=False,
        )
