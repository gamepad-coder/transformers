import time
import warnings
from abc import ABC
from copy import deepcopy
from functools import lru_cache
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import torch
from torch.nn import functional as F

from ..tokenization_utils_base import PreTrainedTokenizerBase
from ..utils import add_start_docstrings, logging


logger = logging.get_logger(__name__)


STOPPING_CRITERIA_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (`torch.LongTensor` of shape `(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary.

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            [What are input IDs?](../glossary#input-ids)
        scores (`torch.FloatTensor` of shape `(batch_size, config.vocab_size)`):
            Prediction scores of a language modeling head. These can be scores for each vocabulary token before SoftMax
            or scores for each vocabulary token after SoftMax. If this stopping criteria depends on the `scores` input,
            make sure you pass `return_dict_in_generate=True, output_scores=True` to `generate`.
        kwargs (`Dict[str, Any]`, *optional*):
            Additional stopping criteria specific kwargs.

    Return:
        `torch.BoolTensor`. (`torch.BoolTensor` of shape `(batch_size, 1)`), where `True` indicates we stop generation
            for a particular row, `True` indicates we should continue.

"""


class StoppingCriteria(ABC):
    """Abstract base class for all stopping criteria that can be applied during generation.

    If your stopping criteria depends on the `scores` input, make sure you pass `return_dict_in_generate=True,
    output_scores=True` to `generate`.
    """

    @add_start_docstrings(STOPPING_CRITERIA_INPUTS_DOCSTRING)
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs) -> torch.BoolTensor:
        raise NotImplementedError("StoppingCriteria needs to be subclassed")


class MaxLengthCriteria(StoppingCriteria):
    """
    This class can be used to stop generation whenever the full generated number of tokens exceeds `max_length`. Keep
    in mind for decoder-only type of transformers, this will include the initial prompted tokens.

    Args:
        max_length (`int`):
            The maximum length that the output sequence can have in number of tokens.
        max_position_embeddings (`int`, *optional*):
            The maximum model length, as defined by the model's `config.max_position_embeddings` attribute.
    """

    def __init__(self, max_length: int, max_position_embeddings: Optional[int] = None):
        self.max_length = max_length
        self.max_position_embeddings = max_position_embeddings

    @add_start_docstrings(STOPPING_CRITERIA_INPUTS_DOCSTRING)
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs) -> torch.BoolTensor:
        cur_len = input_ids.shape[-1]
        is_done = cur_len >= self.max_length
        if self.max_position_embeddings is not None and not is_done and cur_len >= self.max_position_embeddings:
            logger.warning_once(
                "This is a friendly reminder - the current text generation call will exceed the model's predefined "
                f"maximum length ({self.max_position_embeddings}). Depending on the model, you may observe "
                "exceptions, performance degradation, or nothing at all."
            )
        return torch.full((input_ids.shape[0],), is_done, device=input_ids.device)


class MaxNewTokensCriteria(StoppingCriteria):
    """
    This class can be used to stop generation whenever the generated number of tokens exceeds `max_new_tokens`. Keep in
    mind for decoder-only type of transformers, this will **not** include the initial prompted tokens. This is very
    close to `MaxLengthCriteria` but ignores the number of initial tokens.

    Args:
        start_length (`int`):
            The number of initial tokens.
        max_new_tokens (`int`):
            The maximum number of tokens to generate.
    """

    def __init__(self, start_length: int, max_new_tokens: int):
        warnings.warn(
            "The class `MaxNewTokensCriteria` is deprecated. "
            f"Please use `MaxLengthCriteria(max_length={start_length + max_new_tokens})` "
            "with `max_length = start_length + max_new_tokens` instead.",
            FutureWarning,
        )
        self.start_length = start_length
        self.max_new_tokens = max_new_tokens
        self.max_length = start_length + max_new_tokens

    @add_start_docstrings(STOPPING_CRITERIA_INPUTS_DOCSTRING)
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs) -> torch.BoolTensor:
        is_done = input_ids.shape[-1] >= self.max_length
        return torch.full((input_ids.shape[0],), is_done, device=input_ids.device)


class MaxTimeCriteria(StoppingCriteria):
    """
    This class can be used to stop generation whenever the full generation exceeds some amount of time. By default, the
    time will start being counted when you initialize this function. You can override this by passing an
    `initial_time`.

    Args:
        max_time (`float`):
            The maximum allowed time in seconds for the generation.
        initial_time (`float`, *optional*, defaults to `time.time()`):
            The start of the generation allowed time.
    """

    def __init__(self, max_time: float, initial_timestamp: Optional[float] = None):
        self.max_time = max_time
        self.initial_timestamp = time.time() if initial_timestamp is None else initial_timestamp

    @add_start_docstrings(STOPPING_CRITERIA_INPUTS_DOCSTRING)
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs) -> torch.BoolTensor:
        is_done = time.time() - self.initial_timestamp > self.max_time
        return torch.full((input_ids.shape[0],), is_done, device=input_ids.device)


class StopStringCriteria(StoppingCriteria):
    """
    This class can be used to stop generation whenever specific string sequences are generated. It preprocesses
    the strings together with the tokenizer vocab to find positions where tokens can validly complete the stop strings.

    Generation is stopped as soon as a token is generated that completes any of the stop strings.
    We want to catch any instance in which the stop string would be present in the decoded output, which means
    we must also catch cases with "overhangs" off one or both ends. To make this more concrete, for the stop string
    "stop", any of the following token sequences would trigger the match:

    - ["st", "op"]
    - ["stop"]
    - ["st", "opera"]
    - ["sto", "opper"]
    - ["las", "topper"]

    Note that a match will only be triggered if the stop string is at the end of the generated sequence. In other
    words, these sequences will not trigger a match:

    - ["stop", "at"]
    - ["st", "op", "at"]
    - ["st", "opera", "tion"]

    The reason these are not a match is that the stop string does not overlap with the final token. If you can remove
    one or more tokens from the end of the sequence without destroying the stop string, then this criterion will not
    match that stop string. This is by design; because this check is run after each token is generated, we can't miss a
    valid stop string if one is generated, but we don't want to halt generation just because the stop string exists
    somewhere in the past input_ids.

    How is the match actually performed, though? We do it in quite a confusing way, because we want the entire match
    process to be compilable with Torch or XLA, which means we cannot use standard string methods. However, it is possible,
    with some work, to do string matching with pure tensor operations. We'll begin by describing the algorithm we use
    with standard string operations, and then at the end we'll explain how this is converted to pure tensor operations
    at generation time.

    The key to the algorithm is an observation: Because the stop string must overlap with the end of the token sequence, we can start at
    the end of the sequence and work backwards. Specifically, we check that there is an overlap between the *start* of
    the final token and the *end* of the stop_string, or to put it another way, stop_string[-i:] == token[:i] for
    some i > 0. If you look at the positive examples above, you'll see the last token in all of them fulfills this
    property:

    - ["st", "op"] (overlap is "op")
    - ["stop"]  (overlap is "stop")
    - ["st", "opera"]  (overlap is "op")
    - ["sto", "pper"]  (overlap is "p")
    - ["las", "topper"]  (overlap is "top")

    It's impossible to construct a matching sequence that does not have this property (feel free to verify this
    yourself). However, although this overlap between the start of the final token and the end of the stop string is
    necessary for a match, it is not sufficient. We also need to check that the rest of the token sequence is
    consistent with the stop string.

    How do we do that? Let's say the stop string is N characters long, and the initial overlap covers the final
    M characters. Then, we have N - M characters left to match. If the next token is less than N - M tokens long, then
    the entire token must match: token == stop_string[-M - len(token): -M]. If the next token is longer than N - M
    tokens, then we consider only the final N - M characters of the token. This allows for the token to have an overhang
    off the start of the stop string.

    Again, let's make this concrete with a worked example. We'll use the stop string "stop" and the token sequence
    ["las", "topper"]. The length of the stop string is 4. The final token is "topper", and its overlap with the stop
    string is "top", which has length 3. We continue to the next token, "las", and we have 4 - 3 = 1 character left to
    match. This is less than the length of "las", so we only need a partial match for this token to complete the string.
    We check that "las"[-1:] == stop[:1], which is true. We have now matched 4 characters, which is the length of
    the stop string, and we are done.

    At this point, hopefully you agree that we have an algorithm that detects the presence of a stop string, but you
    may not see how we can convert this to tensor operations, particularly since we want to avoid data-dependent
    conditional branching in the compiled code, and ideally vectorize everything so it can be efficiently computed on
    GPU. The key is to realize that although we don't have access to string operations inside the generation loop,
    we can use them in a precomputation stage!

    For every token in the tokenizer vocabulary, we precompute the values
    we need for the above algorithm: The length of that token's overlap with the end of the stop string, the
    position(s) in the stop string where that token matches perfectly, and the length of the token. We then pack
    these values into a single vector per token, and stack those vectors into an embedding tensor which we can
    gather from at runtime to get the values we need.

    This is the approach we take in this class. The precomputation is done in the `_stop_string_create_embedding_vec`
    function. Then, at runtime in the `__call__()` method, we implement the algorithm above in purely vectorized
    fashion, starting from an input_ids vector containing the token IDs in the sequence:

    - Gather from the embedding vector using input_ids as indices, and split the packed vectors into end overlap lengths,
      valid token positions, and token lengths.
    - Make a vector of the length of every token in the sequence, except for the final token, where we use the
      end-overlap length instead.
    - Compute the cumulative sum of the sequence, starting from the end. This represents the number of characters in the stop string that
      we would match after each token, assuming that token is a valid fit for the sequence at that point.
    - To determine if the tokens are valid at each position, we check that the cumulative length so far matches
      one of the values in their valid positions vector. Where it does not, we mask that token and all tokens
      preceding it.
    - We then check the highest unmasked value in the cumulative sum. This represents the length of the total string
      match before we reached a token that did not match the stop string. If it is equal to or greater than the length
      of the stop string, the stop string is matched.

    This is almost the complete algorithm, and the remaining details just handle edge cases: For example, what do
    we do if a token can have multiple possible overlap lengths with the stop string? For example, consider the
    stop string "banana", and the token sequences ["ba", "nana"] and ["bana", "nana"]. Both of these sequences
    contain the stop string and should trigger a match. However, the overlap of the final token is different! In
    the first case, the overlap is "nana". In the second case, the overlap is "na". When we start from the end
    of the sequence and work backwards, we cannot know in advance which overlap length, if any, will lead to a valid
    match, and therefore we must test all possible overlap lengths.

    Therefore, for the stop string "banana" and the token "nana", we store two valid end overlap lengths: 2 and 4.
    We then perform the above algorithm, starting from each value, and test whether each results in a match.
    Thanks to vectorization, we can run these tests in parallel (in fact, we can run the test for every possible
    overlap length and all stop strings in parallel).

    Args:
        tokenizer (`PreTrainedTokenizer`):
            The model's associated tokenizer (necessary to extract vocab and tokenize the termination sequences)
        stop_strings (`Union[str, List[str]]`):
            A list of strings that should end generation. If a string is passed, it will be treated like a
            list with a single element.
    """

    def __init__(self, tokenizer: PreTrainedTokenizerBase, stop_strings: Union[str, List[str]]):
        if isinstance(stop_strings, str):
            stop_strings = [stop_strings]

        self.vocab = tokenizer.get_vocab()
        self.token_list, self.tok_indices = tuple(self.vocab.keys()), tuple(self.vocab.values())
        self.stop_strings: Tuple[str, ...] = tuple(stop_strings)

        self.embedding_vec, self.max_valid_positions, self.max_valid_end_lens = _stop_string_create_embedding_vec(
            self.token_list, self.tok_indices, self.stop_strings
        )
        self.maximum_token_len = max([len(stop_string) for stop_string in self.stop_strings])
        self.num_stop_strings = len(self.stop_strings)
        self.target_lens = torch.tensor([len(stop_string) for stop_string in stop_strings], dtype=torch.int32)

    @add_start_docstrings(STOPPING_CRITERIA_INPUTS_DOCSTRING)
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs) -> torch.Tensor:
        self.embedding_vec = self.embedding_vec.to(input_ids.device)
        self.target_lens = self.target_lens.to(input_ids.device)
        # The maximum length we need to consider is 1 token per character. Note that input_ids can also be
        # *shorter* than the global max, and the code below should be ready for that
        input_ids = input_ids[:, -self.maximum_token_len :]

        # Flip input_ids because we're only matching strings at the end of the generated sequence
        flipped_ids = torch.flip(input_ids, (1,))

        # Size of the vector of positions a single token can match
        max_valid_positions = self.max_valid_positions

        # The embedding vec contains the valid positions, end_lengths and total lengths for each token
        embedded = F.embedding(flipped_ids, self.embedding_vec)

        # Now we split the embedding vector. valid_positions is the positions in the stop string the token can fit
        valid_positions = embedded[:, 1:, : max_valid_positions * self.num_stop_strings].unflatten(
            -1, (self.num_stop_strings, -1)
        )
        # end_lengths is the number of characters from the string, counting from the end, that the token
        # contains. It can have multiple values if the same token can overlap different end lengths
        end_lengths = embedded[:, :1, max_valid_positions * self.num_stop_strings : -1].unflatten(
            -1, (self.num_stop_strings, -1)
        )
        # Lengths is the total length of each token. Unlike the others, it always has a single value
        lengths = embedded[:, 1:, None, -1:]  # Insert a dummy dimension for stop_strings even though lengths are const

        # Concatenate lengths onto each possible end_lengths value
        lengths = lengths.expand((-1, -1, end_lengths.shape[-2], end_lengths.shape[-1]))
        lengths_with_ends = torch.cat([end_lengths, lengths], dim=1)

        # cumsum() to get the number of matched characters in the stop string after each token
        cumsum = lengths_with_ends.cumsum(dim=1)  # B x maximum_token_len x num_stop_strings x max_valid_end_lens

        # The calculation above assumes that all tokens are in valid positions. Now we mask the ones that are not.
        # First, tokens match the start of the string if they have a positive value in the end_lengths vector
        initial_match = end_lengths > 0

        # Tokens continue the string if the cumsum() so far is one of the valid positions for that token
        # Note that we're actually tracking one cumsum() for for each possible end_length
        later_match = torch.any(cumsum[:, :-1, :, None] == valid_positions[:, :, :, :, None], axis=-2)

        # The match vector is a boolean vector that indicates which positions have valid tokens
        match = torch.cat([initial_match, later_match], dim=1)

        # Once a single position does not match, all positions following that position are masked
        mask = (~match).cumsum(dim=1, dtype=torch.int32)
        mask = mask == 0

        # The string is matched if we reached a cumsum equal to or greater than the length of the string
        # before hitting the mask
        string_matches = torch.amax(cumsum * mask, dim=(1, -1)) >= self.target_lens[None, :]

        # We return a per-sample vector that is True if any stop string is matched for that sample
        return torch.any(string_matches, dim=-1)


class StoppingCriteriaList(list):
    @add_start_docstrings(STOPPING_CRITERIA_INPUTS_DOCSTRING)
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs) -> torch.BoolTensor:
        is_done = torch.full((input_ids.shape[0],), False, device=input_ids.device)
        for criteria in self:
            is_done = is_done | criteria(input_ids, scores, **kwargs)
        return is_done

    @property
    def max_length(self) -> Optional[int]:
        for stopping_criterium in self:
            if isinstance(stopping_criterium, MaxLengthCriteria):
                return stopping_criterium.max_length
            elif isinstance(stopping_criterium, MaxNewTokensCriteria):
                return stopping_criterium.max_length
        return None


def validate_stopping_criteria(stopping_criteria: StoppingCriteriaList, max_length: int) -> StoppingCriteriaList:
    stopping_max_length = stopping_criteria.max_length
    new_stopping_criteria = deepcopy(stopping_criteria)
    if stopping_max_length is not None and stopping_max_length != max_length:
        warnings.warn("You set different `max_length` for stopping criteria and `max_length` parameter", UserWarning)
    elif stopping_max_length is None:
        new_stopping_criteria.append(MaxLengthCriteria(max_length=max_length))
    return new_stopping_criteria


def _stop_string_get_matching_positions(
    token_list, tok_indices, stop_strings
) -> Tuple[Dict[str, Dict[str, List[int]]], Dict[str, Dict[str, List[int]]]]:
    """This function preprocesses stop strings and the tokenizer vocabulary to determine where tokens can
    validly appear in the stop strings. For each token, it computes a list of positions in the stop string where the
    token appears, as well as a list of the possible "end overlaps" for that token - that is, the number of characters
    from the end of the stop string that overlap with the start of the token, which can have more than one value.

    The reason for computing these may seem a bit cryptic - please see the docstring for StopStringCriteria for a full
    explanation of what these values are for!"""

    def _cleanup_token(token: str) -> str:
        if token[0] in ["▁", "Ġ"]:
            token = " " + token[1:]
        elif token[0] == "##":
            token = token[2:]
        return token

    reversed_filtered_token_list = [_cleanup_token(token)[::-1] for token in token_list]
    token_valid_positions = {}
    token_end_overlaps = {}
    for stop_string in stop_strings:
        reversed_stop_string = stop_string[::-1]
        token_valid_positions[stop_string] = {}
        token_end_overlaps[stop_string] = {}
        for token, reversed_filtered_token, tok_idx in zip(token_list, reversed_filtered_token_list, tok_indices):
            matching_positions = []
            possible_end_lengths = []
            for i in range(1 - len(token), len(stop_string)):
                if i < 0:
                    tok = reversed_filtered_token[-i:]
                    i = 0
                else:
                    tok = reversed_filtered_token
                stop = reversed_stop_string[i : i + len(tok)]
                if tok.startswith(stop):
                    if i == 0:
                        possible_end_lengths.append(min(len(tok), len(stop)))
                    else:
                        matching_positions.append(i)

            if matching_positions:
                token_valid_positions[stop_string][tok_idx] = matching_positions
            if possible_end_lengths:
                token_end_overlaps[stop_string][tok_idx] = possible_end_lengths
    return token_valid_positions, token_end_overlaps


@lru_cache(8)
def _stop_string_create_embedding_vec(token_list, tok_indices, stop_strings) -> Dict[str, torch.tensor]:
    """This function precomputes everything needed for the run-time checks in StopStringCriteria, and packs
    them into an embedding tensor that can be accessed with pure tensor operations. For the specifics of the values
    that are precomputed and what they are used for, please refer to the StopStringCriteria docstring!"""
    token_valid_positions, token_end_overlaps = _stop_string_get_matching_positions(
        token_list, tok_indices, stop_strings
    )

    max_valid_positions = max(len(val) for positions in token_valid_positions.values() for val in positions.values())
    max_valid_end_lens = max(len(val) for positions in token_end_overlaps.values() for val in positions.values())
    vec_size = len(stop_strings) * (max_valid_positions + max_valid_end_lens) + 1
    gather_vec = np.full((len(token_list), vec_size), dtype=np.int32, fill_value=-1)

    for i, stop_string in enumerate(stop_strings):
        positions = token_valid_positions[stop_string]
        end_lens = token_end_overlaps[stop_string]

        # Since this is lots of very small assignments of lists, we build it with numpy rather
        # than torch for speed + simplicity, then convert to torch at the end
        for token_idx, valid_positions in positions.items():
            gather_vec[
                token_idx, max_valid_positions * i : max_valid_positions * i + len(valid_positions)
            ] = valid_positions
        for token_idx, possible_end_lens in end_lens.items():
            gather_vec[
                token_idx,
                max_valid_positions * len(stop_strings) + max_valid_end_lens * i : max_valid_positions
                * len(stop_strings)
                + max_valid_end_lens * i
                + len(possible_end_lens),
            ] = possible_end_lens
        for token, token_idx in zip(token_list, tok_indices):
            gather_vec[token_idx, -1] = len(token)

    gather_vec = torch.tensor(gather_vec, dtype=torch.int32)

    return gather_vec, max_valid_positions, max_valid_end_lens
