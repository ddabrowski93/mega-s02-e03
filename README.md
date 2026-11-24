# MEGA S02E04 — job 2 remote worker

Adding this workflow does not run it. No remote job calls, transcription,
submission, installs, secret writes, or workflow dispatch are performed on push.

## Approval boundary

Pushing these files does **not** authorize running the workflow. Separate approval
is needed to:

1. Generate a private decryption key **outside this repository**, under the main
   agent/user's control. Keep it out of public artifacts and logs.
2. Set repository secret `MEGA_JOB2_URL` to the exact Streamable HTTP URL, without
   altering its path or query. Do not print it. This requires no OAuth.
3. Set repository Actions variable `MEGA_RESULT_CERT` to the corresponding public
   X.509 certificate. The certificate is public; the private key must stay local.
4. Dispatch **MEGA job 2 (remote only)**, explicitly approving runner-local pip
   installs and model downloads with `approve_local_model=true`. This authorizes
   calls to the job's tools, including memo writes and final submission, from the
   GitHub-hosted runner. No separate transcription service is used.

The workflow has no push/PR trigger, uses `contents: read`, persists no checkout
credentials, serializes runs, and has a 35-minute job timeout. The worker step has
a separate 15-minute timeout, leaving time for artifact upload. Do not automatically
retry a submitted or failed run: first inspect its encrypted evidence for partial
side effects. A retry starts again with transcription as required by the skill.

## Secure result handoff

Example commands for the **main agent after approval**, not steps run here. Choose
a private directory outside the repository; the key command prompts for a
passphrase. Never put the passphrase in a command or environment variable.

```sh
openssl req -x509 -newkey rsa:3072 -sha256 -days 7 \
  -keyout /PRIVATE/DIRECTORY/job2.key \
  -out /PRIVATE/DIRECTORY/job2-cert.pem -subj /CN=job2-result
```

Set `MEGA_RESULT_CERT` to `job2-cert.pem`'s contents. After the approved run,
download the artifact named `job2-encrypted-RUN_ID-RUN_ATTEMPT`. Decrypt privately:

```sh
openssl cms -decrypt -binary -inform DER -in result.cms \
  -recip /PRIVATE/DIRECTORY/job2-cert.pem \
  -inkey /PRIVATE/DIRECTORY/job2.key \
  -out /PRIVATE/DIRECTORY/job2-result.json
```

Output uses authenticated CMS AES-256-GCM encryption with an RSA recipient.
Encryption is preflighted before any job call. No plaintext transcript, tokens,
endpoint, notes, or receipts are written to disk. Encrypted checkpoints are written
before/after every RPC, after tool decoding/token update, at inference phase
boundaries, and after each generated response. Ciphertext is written to a temporary
file and atomically renamed over the last successful `result.cms` only after
encryption succeeds. Failed encryption leaves the prior checkpoint intact.

SIGINT/SIGTERM request a graceful stop with encrypted failure diagnostics; a signal
during checkpointing is deferred until that checkpoint is complete. Hard kill/OOM
can still prevent the final checkpoint or upload, but any uploaded artifact retains
the latest successful checkpoint (including transcription before model load), not
just preflight. `pending_rpc` means a request may have had remote effects even if
no reply was recorded: inspect privately and never assume it is safe to retry.

The encrypted JSON includes **exact unmodified parsed JSON-RPC tool reply objects**
under `exchanges`, including the submit reply and its receipt; `submission` contains
the decoded tool result. This preserves string values exactly, not HTTP whitespace.
The main agent must inspect the server's actual acceptance response and exact
receipt; a successful workflow or `submitted_check_reply_for_acceptance` status
does **not** assert acceptance. Only combine accepted results. Never quote private
payloads in public logs or issue comments.

The second artifact file, `audit.jsonl`, deliberately contains only fixed operation
labels and fixed status labels. No request/reply values or counts are included.
Artifacts expire after three days. Repository maintainers can see Actions metadata
and ciphertext but need the local private key to recover its contents. Remove the
secret/variable, delete the artifact, and disable/delete the workflow to revoke
future use; remote memo writes/submission may not be reversible.

## Worker behavior and dependencies

`worker/job2.py` refuses normal execution unless Actions identifies its environment
as `github-hosted`. This is an accident guard, not remote attestation.

On the runner it downloads and **reads** `voice-to-memo/SKILL.md` from the supplied
archive, verifies the SHA-256 of the inspected v1, initializes MCP at the exact secret
URL, negotiates protocol/session headers, lists tools dynamically, and binds their
declared input properties. Redirects are refused to avoid leaking capability URLs.
It supports JSON and finite SSE JSON-RPC replies. Unknown required fields, changed
skill bytes, ambiguous tokens/content, unsupported transcript shapes, and model
verification failures stop execution rather than guessing. It does not implement
SSE reconnection or arbitrary JSON Schema compositions; an unexpected API shape
requires private inspection of the encrypted evidence and a code update.

The worker follows `transcribe` → one `memo_write` per finding → unfiltered
`memo_search` → fill missing exact notes/search again → `submit` with v1. Each
post-transcription call uses the freshly returned step token. The job-provided
`transcribe` tool is required by this skill, so **no Whisper or schema-first
transcription fallback is installed or called**. If that tool cannot transcribe,
stop and obtain approval for a revised approach; do not silently switch services.

MCP, skill download, audit, and encryption use Python's standard library and the
runner's OpenSSL. Runner-only NLP dependencies are CPU PyTorch 2.9.1 and Transformers
4.57.1 (plus their pip dependencies), and public model
`Qwen/Qwen2.5-0.5B-Instruct` (about 1 GB model download, roughly 2 GB float32 weights
in memory, plus runtime overhead). It runs on CPU with float32, avoiding potentially
unaccelerated CPU bfloat16. Dependencies are installed in a temporary virtual
environment, not globally. The model uses safetensors, disallows custom remote
Python code, uses deterministic generation, and outputs only compact numeric spans
and audit flags, never a generated prose ledger.

### Parser and extraction safeguards

The transcript parser accepts the job's `at: "m:ss"` segments and numeric `start`
segments. Supplied `at` strings are preserved exactly; whole-second `start` values
are formatted without rounding. Fractional, missing, invalid, or conflicting offsets
stop the worker rather than inventing a timestamp. Original segment data is not
rewritten.

Remote extraction splits each source segment conservatively at sentence/semicolon
boundaries into candidate units, retaining exact source coordinates. Conjunctions
are not assumed to be event boundaries. **Python enumerates every unit**, owns the
coverage ledger, and records selected/verified progress; the model cannot omit,
rename, or reorder rows. For each unit independently:

1. Count actual event occurrences with a tiny JSON response such as `[0]` or `[2]`.
   The count can be zero through eight; `[9]` explicitly means uncertain/over limit
   and stops execution. These are generic safety limits, not expected clip counts.
2. For each event, select only `[first_word,after_last_word]` from valid half-open
   source-word intervals. Multiple events have distinct ordered non-overlapping
   spans. Text is copied verbatim, not generated.
3. After context assembly, independently audit **each** unit, including zero-event
   units: `[1]` accepts, `[0]` rejects, `[2]` is uncertain. All units must pass before
   any memo write. This remains the same model in fresh stateless calls, not an
   independently trained verifier.

Decoding is constrained using a tokenizer-based finite-choice trie and Hugging
Face's `prefix_allowed_tokens_fn`. Only allowed numeric JSON replies can complete;
there is no vocabulary-wide scan or extra grammar dependency. A model therefore
cannot replace indices with strings or output an incomplete global ledger as it
could under prompt-only formatting. Bounds, ordering, and exact grounding are also
validated outside the model. A malformed/truncated response may receive **one**
format-repair attempt for that single call; a second failure stops. Semantic audit
rejection or uncertainty is never retried into agreement. No keyword filter decides
whether an incident happened; no clip-specific answers or event totals are encoded.

Clock/date/timezone/line context is assembled automatically from exact source text
using generic English context patterns. Context-only date/zone declarations are
quoted verbatim; line identity can be inherited from an earlier explicit source
mention. The latest preceding context is preferred. A later date/zone declaration
is a tentative fallback only if no earlier one exists. Numeric clocks outside a
selected span may be attached only to their unique closest event in the same unit;
ties fail rather than guess. Spoken clocks are never converted or rounded. The
independent per-unit audit must explicitly approve every unit for completeness,
one-event grouping, no chatter, correct context scope, and original offsets.
Any rejection, unrepaired invalid output, or uncertainty stops before memo writes.
There are no global ledger/prose repair loops. Confirmation counts duplicate notes.

Inference limits: model loading, all counts/spans, any format repairs, and all audits
share an eight-minute SIGALRM deadline. Each tiny generation has a 30-second
`max_time`, an output-token limit derived from the longest allowed choice plus one
EOS token, and early stopping at a complete allowed response. A total
1,536-generated-token budget is enforced. Inputs above 128 candidate units, 96
words in one unit, or 6,144 prompt tokens fail instead of truncating/skipping text.
Prompts target only one unit, its immediate neighbors, and verbatim date/zone
references; they do not repeatedly request a full-transcript generated ledger.
Normal inference is intended to fit within about ten minutes including downloads,
but throughput/download latency is not guaranteed. A blocked native operation can
delay Python signal handling; the preceding encrypted checkpoint remains available.
Generated responses and diagnostics stay encrypted.

Failures now retain an encrypted phase, exception type/module/message, and traceback
frame basenames/function names/line numbers. No traceback source, locals, absolute
paths, or diagnostic messages are printed in public logs. Raw RPC logs remain
encrypted as before.

Offline synthetic tests (no private files, HTTP, model loading, or job execution):

```sh
python3 -B -m unittest discover -s tests -v
```

**Limitations:** the smaller model is faster but less capable; agreement with its
own independent audit is not a guarantee of complete/correct extraction. English
context patterns are not a complete parser of natural-language clock/timezone
scope, uncommon zone names, or spoken dates; incorrect or uncertain associations
must be rejected by the audit. Only the job's acceptance is authoritative. Its model
revision and transitive pip dependencies are not locked. The model may exceed
runner resources or the timeout. Approve this inference approach explicitly
before dispatch, or replace it with another separately approved runner-only
inference method. No expected notes, counts, or receipts are hardcoded.
