# Search-effort timing pilot

The pilot contains 12 answerable questions across two organizations. The
[timing sheet](timing_sheet.csv) gives the participant only the questions. The
[gold set](questions.jsonl) gives the reviewer the expected answer and source.
Both modes use the same [four synthetic pilot documents](pilot_documents.json).

## Start the pilot

From the repository root, with Docker Compose and Python 3.11 or later available:

```sh
bash scripts/demo.sh
```

The script creates a disposable local project, applies the database migrations,
and registers an operator and reader using each organization's configured roles.
It uploads and indexes the pilot documents through Core, then prints the login
URL, Core URL, and generated logins. The original files, credential file, and
document ID map are under the printed `data/demo/intellidocs-pilot-*` directory.
Use an operator login for the answer endpoint. Keep `access.json` private.

The default stack uses a local fixture provider and fake embeddings. Use this
mode to rehearse the protocol. Its API timings describe the scripted stack.
For a model-backed trial, set `DEMO_LLM_MODE=live` and export `LLM_PROVIDER`,
`LLM_MODEL`, `LLM_API_KEY`, and `LLM_BASE_URL` when using an OpenAI-compatible
provider. The default embedder is still fake. Record the actual model and
retrieval setup, and use the setup and corpus you intend the result to represent.
The demo prints a stop command that removes its containers and volumes.

## Measure and review

1. Pick a participant familiar with the document languages and a reviewer.
   Record the participant's familiarity, machine, corpus, cache conditions,
   and time limit for the trial. Give the participant the document folder and
   a copy of the timing sheet. Keep the gold answers and system output hidden
   from the participant until they finish.
2. **A person must time every manual attempt with a stopwatch. Never estimate
   the manual baseline or fill in a missing time.** Start when the participant
   reads the question, with the file folder already available. Allow ordinary
   manual tools such as folder browsing, document search, and text find within
   the question's organization folder. Stop when the participant records an
   answer and its source, or hits the agreed limit. Record elapsed seconds even
   for a wrong answer or timeout. Keep unattempted rows blank and preserve the
   question IDs and text.
3. Have the reviewer set `manual_outcome` to `correct`, `incorrect`, or
   `timeout` against the gold set. Count an answer as correct only when its
   content and cited source both support it. Equivalent wording is acceptable.
4. Run the system trial once. It signs into Core once per organization before
   timing, then measures each authenticated `GET /api/answer` from request
   start through the full response body. The timer includes network and server
   work. It excludes login, JSON decoding, and browser rendering. There are no
   answer retries. Record cache conditions and keep every error or abstention.
5. Review the recorded answers and their citations against the same gold set.
   Fill `system_outcome` with `correct` or `incorrect` for returned answers.
   Resolve cited document IDs using the pilot's `documents.json`. Leave the
   review cell blank for abstentions and request failures, which the script
   records automatically.

```sh
# Set this to the path printed by the demo.
PILOT=data/demo/intellidocs-pilot-REPLACE_ME
OUT=eval/results/timing/pilot-01
mkdir -p "$OUT"
cp eval/timing/timing_sheet.csv "$OUT/sheet.csv"
# Enter the measured manual times and reviewer outcomes in that copy.
python3.11 -m eval.timing.run measure \
  --credentials "$PILOT/access.json" --output "$OUT/system.json"
# Review system.json and enter the system outcomes before writing the report.
python3.11 -m eval.timing.run report \
  --sheet "$OUT/sheet.csv" --system "$OUT/system.json" --output "$OUT/report.json"
```

Use the same interpreter as `DEMO_PYTHON` or the intelligence `.venv` if
`python3.11` is unavailable by that name. For an existing stack, supply a private
JSON credential file with `core_url`, `organizations[org_id].operator` containing
`email` and `password`, and a `stack` object describing the measurement setup.
Keep measurements, answers, and credentials out of Git.

## Interpret the result

The report shows median and linearly interpolated p95 in seconds for all attempts
and separately for questions answered correctly by both methods. Speedup is the
paired manual median divided by the paired system median. The report also gives
the p95 ratio on those same questions. It withholds ratios until all manual
attempts are recorded and all returned answers reviewed. Check the outcome counts
and paired sample size alongside any ratio. These 12 synthetic questions and four
documents cannot substantiate a general claim that search drops from hours to
seconds.
