# Measuring retrieval and answers

The procedure; the results on one real corpus are in `retrieval-
measurement.md`.

Every retrieval choice — chunk size, stemming, a fallback, embeddings, figure
descriptions — is a guess until it moves a number. Build the number first.

1. `scripts/sample_sections.py --root <corpus> --n 60` draws a stratified
   sample: every document gets a share by the square root of its size, and
   front matter, boilerplate and stubs are left out.
2. Write one question per sampled section while reading it, into
   `<corpus>/eval/questions.jsonl` with that section as the answer. Give each a
   kind: `identifier` (names a command, option or message), `concept` (natural
   wording), `paraphrase` (deliberately avoids the section's own terms — the
   gap an embedding index would close), `figure` (answered by a figure; sample
   with `--figures` and look at the image), and `real` for questions users
   actually asked.
3. `scripts/eval_search.py --db <index> --questions … --misses` scores hit@k and
   MRR per kind through the server's own `search()`.
4. **Review every miss before believing it.** When what came back answers the
   question just as well, add it as an answer and say so in the question's
   note. Two of the first twelve misses on a real corpus were right answers the
   test did not know about.
5. Compare configurations on the same questions and read the per-question
   changes, not just the totals — on 57 questions, one question is 1.75 points.
   Save each run with `--json`, then `eval_search.py --compare before.json
   after.json` prints the totals side by side and every question whose rank
   moved. Each run records the build time of its index and the commit of the
   code.
6. **After a reconversion, carry the answers across.** Section files are named
   by position, so converting a document again renames them and every answer
   by file reads as a miss. `scripts/remap_answers.py --root <corpus>
   --questions <file>` compares each lost section's text, kept in
   `.rebuild-backup/<slug>/`, with the sections now there, and writes a remapped
   copy (never over the input), reporting what it could not place. An answer
   can instead be written as `{"doc_id": …, "quote": "a short exact phrase"}`,
   which `eval_search.py` resolves to the sections holding the phrase and which
   needs no remap at all.
   A question can also be asked of one edition, with `"document"` and
   `"version"` beside `answers`: the search is then that edition's only, as
   `search_docs` takes them, so a question about an older edition can be scored.

Questions written by someone who has just read the answer share its words, so
they flatter lexical search. `paraphrase` measures that bias; `real` questions
are the only ones free of it, so ask the user for some.

## Real use: log, triage, answer

The questions above test search. Whether the user gets the right answer is a
second thing, and it can only be measured on what they really ask.

1. **Log.** Start the server with `PDF_TO_RAG_LOG=<file>` in its environment
   (the `env` block of `.vscode/mcp.json` or `.mcp.json`). Each tool call then
   appends a line: when, tool, arguments, the sections returned (by id and by
   collection, slug and file), characters, latency, errors, the client's name (over stdio) and, over HTTP, a
   `token_id`: the first eight hex characters of the SHA-256 of the bearer
   token, so one token's calls can be told from another's.
   It is off unless set and writes nothing to stdout. The file holds what people
   searched for, and up to 2,000 characters of any log they pasted into
   `explain_log`: keep it out of version control and shared folders unless
   everyone who can read it should see that. `triage_log.py --log <file>
   --summary` counts it, including searches that returned nothing.
2. **Triage.** `triage_log.py --log <file> --questions <questions.jsonl> --db
   <index>` shows each distinct search with its results and asks hit, miss,
   skip or ignore. A hit names the result that answers it; a miss names the
   right section by id, by `slug file`, or by searching the index. Each goes
   into the question file as kind `real`, appended and nothing else touched,
   so `eval_search.py` gives real questions a row of their own. What the agent
   typed into `search_docs` is usually keywords: the prompt also asks for the
   question in the user's words (`ask`) and a short answer (`gold`).
3. **Answer.** `eval_answers.py --db <index> --questions <file> --limit 1`
   runs each question that has a `gold` answer through `claude -p` with the
   corpus's server only (no file tools, no other servers, an empty working
   folder), then grades the answer with a second call: `correct` (matches the
   gold answer and cites a section that holds it), `unsupported` (an answer
   that misses it or cites nothing that holds it) or `declined`. Beside each
   verdict it records whether a gold section was returned by any tool call, so
   a wrong answer is told apart from a retrieval miss, and the tokens used,
   answering and grading apart. It spends tokens on every run: start with
   `--dry-run` and `--limit 1`. The grader is a model reading a short gold
   answer, so read the `unsupported` ones before believing them, as with
   search misses.
