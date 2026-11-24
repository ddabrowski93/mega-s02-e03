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
4. Ensure repository secret `OPENROUTER_API_KEY` is set (already configured for
   this repository); never put its value in files, prompts, or logs.
5. Dispatch **MEGA job 2 (remote only)** with `approve_openrouter=true`. This
   explicitly authorizes sending the full transcript and skill to OpenRouter and
   its upstream provider, at most two GPT-4.1-mini requests under a $1 provider
   budget, and the job's memo writes and final submission from the GitHub-hosted
   runner. Transcription still uses only the job-provided tool.

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
the latest successful checkpoint (including transcription before provider calls), not
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
as `github-hosted`, explicit `MEGA_APPROVE_OPENROUTER=true` is present, and the API
key is available. This is an accident guard, not remote attestation. The manual-only
workflow runs offline synthetic tests before execution; only the execution step
receives secrets.

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

All worker HTTP, parsing, accounting, audit, and encryption orchestration use
Python's standard library and the runner's preinstalled OpenSSL. There are no pip
installs, model downloads, or local-model dependencies.

### Provider disclosure and cost controls

Inference uses only `openai/gpt-4.1-mini` at
`https://openrouter.ai/api/v1/chat/completions`. The full transcript, original
offsets, and skill are sent in both requests; the second also includes the candidate
ledger/notes (or malformed candidate response). MCP capability URLs, step tokens,
tool schemas, RPC logs, and authorization headers are not included in prompts.
Request projection and sensitive-value checks provide additional protection.

Routing sets `max_price: {prompt: 0.4, completion: 1.6}` (USD per million tokens),
`require_parameters: true`, `data_collection: deny`, and `allow_fallbacks: false`.
There are no plugins, tools, web searches, alternative models, or automatic retries.
The data-collection setting requests eligible providers; it is not a guarantee of
zero retention or a substitute for OpenRouter/upstream privacy policies. Encryption
of result artifacts does not hide transcript data from these inference providers.

At most **two paid request attempts per worker execution** are allowed. Every
request is capped at 60,000 serialized messages UTF-8 bytes (not characters) and
8,000 completion tokens. Oversize inputs fail rather than truncate. Before each
network attempt, the worker checkpoints a conservative reservation:

```text
((message_bytes + 8192) × $0.0000004) + (max_tokens × $0.0000016)
Two-request maximum: $0.0801536
User-approved provider ceiling: $1
```

Thus expected provider cost is **at most about $0.10**, with a conservative
reservation below that amount under the supplied pricing and byte/token-overhead
assumptions. The worker checks both accumulated reservations and reported cost
against the $1 ceiling. A timeout, cancellation, or unknown outcome never refunds
the reservation: the provider may still charge. Each HTTP request has a 180-second
socket timeout. Any request/HTTP failure, missing or invalid usage/cost, excessive
reported usage, wrong model, refusal, or truncated completion stops further work;
no third request is available. Reported usage and cost are retained encrypted.
These are client-side safeguards, not an account-wide billing limit or an absolute
guarantee against incorrect provider billing. Set a separate key/account spending
limit if a provider-enforced ceiling is required. A new workflow execution starts
a new budget; never automatically rerun an uncertain execution.

**GitHub Actions billing is separate** from OpenRouter/provider charges. Runner
minutes and artifact storage depend on the repository's plan and allowances and
are not covered by the $1 inference ceiling.

### Parser and extraction safeguards

The transcript parser accepts the job's `at: "m:ss"` segments and numeric `start`
segments. Supplied `at` strings are preserved exactly; whole-second `start` values
are formatted without rounding. Fractional, missing, invalid, or conflicting offsets
stop the worker rather than inventing a timestamp. Original segment data is not
rewritten.

1. The first request receives the entire source and returns a structured ledger:
   one ordered row per segment, including empty rows for chatter, with one event
   per actual occurrence. Each event supplies an exact evidence substring, its
   zero-based occurrence index among identical substrings, and exact context
   quotes referencing source segments.
2. The second, fresh request independently rereads the entire transcript and skill
   and returns a **corrected full ledger**. It may repair omissions or malformed/
   ungrounded first-candidate content within this same second request. It must
   assert `verified=true` for every segment, `complete_and_exact=true`,
   `uncertain=false`, and no unresolved issues. There is no third repair call.

Both requests use strict JSON Schema structured output. Python additionally
validates complete ordered segment coverage, integer occurrence indices, exact
source substrings, non-overlapping ordered evidence, and grounded context quotes.
It constructs notes from these quotes and the original offsets, preserving
spoken clocks/timezone wording and repeated findings. It rejects text that cannot
fit the one-line `<what happened> @m:ss` format. All final checks must pass before
any memo write. Confirmation uses multiplicity, so identical repeated notes are
not deduplicated. No expected notes, event totals, or receipts are hardcoded.

Failures now retain an encrypted phase, exception type/module/message, and traceback
frame basenames/function names/line numbers. No traceback source, locals, absolute
paths, or diagnostic messages are printed in public logs. Raw RPC logs remain
encrypted as before.

Offline synthetic tests (no private files, HTTP, provider calls, or job execution):

```sh
python3 -B -m unittest discover -s tests -v
```

**Limitations:** independent verification uses a fresh request to the same model,
not an independently trained verifier. Exact substring grounding and complete
segment coverage cannot prove semantic completeness, one-event grouping, or
correct clock/timezone scope. A model can still omit an event or approve a wrong
association. Only the job's actual acceptance response is authoritative. Provider
availability, structured-output support, model revisions, response length, and
latency are not guaranteed; failures stop rather than switch providers/models or
weaken validation. No actual clip inference is performed by the offline tests.
