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
credentials, serializes runs, and has a 35-minute timeout. Do not automatically
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
endpoint, notes, or receipts are written to disk. The in-memory result is encrypted
on normal completion and caught failure. Abrupt runner termination/OOM may prevent
final encryption; the existing artifact can then contain only the preflight state.

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
`Qwen/Qwen2.5-1.5B-Instruct` (several GB). Dependencies are installed in a temporary
virtual environment, not globally. The model uses safetensors, disallows custom
remote Python code, uses deterministic generation, grounds each finding in an
exact segment substring, and performs a second coverage/timestamp review.

### Parser and extraction safeguards

The transcript parser accepts the job's `at: "m:ss"` segments and numeric `start`
segments. Supplied `at` strings are preserved exactly; whole-second `start` values
are formatted without rounding. Fractional, missing, invalid, or conflicting offsets
stop the worker rather than inventing a timestamp. Original segment data is not
rewritten.

Remote extraction creates a coverage ledger with a row for **every** segment,
including chatter, and a separate entry for every event occurrence. Notes quote
the actual event wording and any necessary clock/date/timezone/line context from
validated source substrings. Spoken times and timezone context are not normalized
or paraphrased. A second inference pass must positively audit every segment for
coverage, repeats, one-event grouping, timestamps, and inherited context. Audit
issues allow up to two remote repair passes before stopping, always before memo
writes. Generated responses and diagnostics remain encrypted. This increases
checks, but does not make a small language model infallible. Confirmation counts
duplicate notes rather than treating repeated findings as a set.

Failures now retain an encrypted phase, exception type/module/message, and traceback
frame basenames/function names/line numbers. No traceback source, locals, absolute
paths, or diagnostic messages are printed in public logs. Raw RPC logs remain
encrypted as before.

Offline synthetic tests (no private files, HTTP, model loading, or job execution):

```sh
python3 -B -m unittest discover -s tests -v
```

**Limitations:** a small local language model is not a guarantee of correct or
complete extraction; only the job's acceptance is authoritative. Its model
revision and transitive pip dependencies are not locked. The model may exceed
runner resources or the timeout. Approve this inference approach explicitly
before dispatch, or replace it with another separately approved runner-only
inference method. No expected notes, counts, or receipts are hardcoded.
