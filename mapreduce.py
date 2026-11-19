"""
Summarize a document that is too big for ONE LLM request (map-reduce).

    chunks -> batches -> MAP: short notes per batch -> (shrink notes again if still big) -> REDUCE: final summary

Why: the LLM provider limits tokens per request AND per minute (Groq free tier: 8,000 tokens/minute).
So we (1) keep every request small, (2) pace the requests so a minute never goes over the budget.

This file has no model code: the caller passes functions in, so it can be tested without any model.
All numbers are estimates. Our chunker's tokenizer counts fewer tokens than the LLM's, so we add a safety factor.
"""

import time
from collections import deque

from errors import DocumentTooLargeError

# ---------------------------------------------------------------
# Limits (tune here)
# ---------------------------------------------------------------
SAFETY_FACTOR = 1.3        # our tokenizer under-counts compared to the LLM's tokenizer
FINAL_MAX = 5000           # est. tokens: the most the FINAL call may read (also the "small document" limit)
MAP_BATCH_MAX = 3000       # est. tokens of document text per map call
MAP_PROMPT_TOKENS = 300    # est. size of the instructions around the text
MAP_MAX_OUTPUT = 700       # most tokens a map call may write (notes + the model's own reasoning)
FINAL_OVERHEAD = 1500      # est. instructions + answer of the final call
TPM_BUDGET = 7000          # tokens per minute we allow ourselves (the provider's limit is 8,000)
WINDOW_SECONDS = 60
MAX_MAP_CALLS = 12         # ~7 minutes of paced calls; with Docling it still fits the 900 s Lambda timeout
MAX_ROUNDS = 3             # how many times the notes may be shrunk again
RETRIES_ON_429 = 3
MAX_RETRY_WAIT = 60        # seconds


# ---------------------------------------------------------------
# Token estimates
# ---------------------------------------------------------------
def estimate_tokens(text, count_tokens):
    """count_tokens(text) -> int, from the chunker's tokenizer."""
    return int(max(count_tokens(text) * SAFETY_FACTOR, len(text) / 3))


def make_batches(texts, count_tokens, max_tokens):
    """Group consecutive texts (keeps the document order) so each group is <= max_tokens.
    A single text bigger than max_tokens gets a batch of its own."""
    batches, current, size = [], [], 0
    for text in texts:
        tokens = estimate_tokens(text, count_tokens) #calculate+ the current tokens and size is basically what batch size is curently happening
        if current and size + tokens > max_tokens: #size+token is calculating whether we can create batch now or not
            batches.append(current)
            current, size = [], 0
        current.append(text)  #storing each chunks so that we can add in batch
        size += tokens #all the tokens combined size of the current batch
    if current:
        batches.append(current)
    return batches


# ---------------------------------------------------------------
# Pacing: never spend more than `budget` tokens in any 60 seconds
# ---------------------------------------------------------------
class RateLimiter:
    def __init__(self, budget=TPM_BUDGET, window=WINDOW_SECONDS, clock=time.monotonic, sleep=time.sleep):
        self.budget, self.window, self.clock, self.sleep = budget, window, clock, sleep
        self.spent = deque()   # (time, tokens)

    def wait_for(self, tokens):
        """Block until `tokens` fit in the current window, then record them."""
        while True:
            now = self.clock()
            while self.spent and now - self.spent[0][0] >= self.window:
                self.spent.popleft()
            used = sum(t for _, t in self.spent)
            # Nothing recent: go ahead, even if this one request is bigger than the budget
            if not self.spent or used + tokens <= self.budget:
                self.spent.append((now, tokens))
                return
            wait = self.spent[0][0] + self.window - now
            print(f"  pacing: waiting {wait:.0f}s for the token budget")
            self.sleep(max(wait, 0) + 0.5)


def _retry_after(error):
    """Seconds the provider asks us to wait (HTTP Retry-After header), or a default."""
    try:
        return min(float(error.response.headers.get("retry-after")), MAX_RETRY_WAIT)
    except (AttributeError, TypeError, ValueError):
        return 20.0


def paced_call(invoke, arg, tokens, limiter, sleep=time.sleep):
    """Run invoke(arg) after pacing. On HTTP 429 (rate limit that clears) wait and try again."""
    for attempt in range(RETRIES_ON_429 + 1):
        limiter.wait_for(tokens)
        try:
            return invoke(arg)
        except Exception as error:
            if getattr(error, "status_code", None) != 429 or attempt == RETRIES_ON_429:
                raise   # not a rate limit (or out of tries): the handler decides what to do
            wait = _retry_after(error)
            print(f"  rate limited (429), waiting {wait:.0f}s ({attempt + 1}/{RETRIES_ON_429})")
            sleep(wait)


# ---------------------------------------------------------------
# The map-reduce itself
# ---------------------------------------------------------------
def summarize_large(texts, *, map_invoke, final_invoke, count_tokens, limiter=None, sleep=time.sleep):
    """
    texts         list of chunk texts, in document order
    map_invoke    f(text) -> notes (str)            one small LLM call
    final_invoke  f(notes_text) -> SummaryResult    the final structured call
    """
    limiter = limiter or RateLimiter(sleep=sleep)
    pieces, calls = list(texts), 0

    for round_number in range(1, MAX_ROUNDS + 1):
        batches = make_batches(pieces, count_tokens, MAP_BATCH_MAX)
        calls += len(batches)
        # Fail BEFORE spending minutes of paced calls on a document we cannot finish
        if calls > MAX_MAP_CALLS:
            raise DocumentTooLargeError(f"needs {calls} map calls (limit {MAX_MAP_CALLS})")

        print(f"map round {round_number}: {len(batches)} batches")
        notes = []
        for number, batch in enumerate(batches, 1):
            text = "\n\n".join(batch)
            cost = estimate_tokens(text, count_tokens) + MAP_PROMPT_TOKENS + MAP_MAX_OUTPUT
            notes.append(paced_call(map_invoke, text, cost, limiter, sleep))
            print(f"  map {number}/{len(batches)} done")
        pieces = notes

        if sum(estimate_tokens(p, count_tokens) for p in pieces) <= FINAL_MAX:
            break
    else:
        raise DocumentTooLargeError("the notes are still too big after shrinking them")

    notes_text = "\n\n".join(pieces)
    cost = estimate_tokens(notes_text, count_tokens) + FINAL_OVERHEAD
    print("reduce: final summary")
    return paced_call(final_invoke, notes_text, cost, limiter, sleep)
