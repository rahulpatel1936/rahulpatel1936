# Review: `Word_Counter.py`

Review of the interview-transcript word-counting script, covering
accuracy risks and the revisions made in `word_counter.py`.

## Summary of issues found

1. **Word definition (tokenizer).** `[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?`
   only recognizes the straight apostrophe `'`. Any transcript using a
   curly/smart apostrophe (`’`, `‘`, common when text has passed
   through Word or an auto-transcription tool) splits contractions in
   two, e.g. `"didn't"` -> `["didn", "t"]`, verified directly:
   ```
   straight: ['I', "didn't", 'know', 'what', 'to', 'do']
   curly:    ['I', 'didn', 't', 'know', 'what', 'to', 'do']
   ```
   This inflates counts differently depending on which software
   produced the transcript, not on what the participant said.
   Hyphenated compounds (`well-being`, `follow-up`, `co-worker`) were
   also silently split into two tokens each.

2. **No interviewer/probe removal.** The original script only deletes
   4 hardcoded question-header lines and the phrase "no response." It
   does not detect or remove interviewer speaker turns, timestamps, or
   bracketed transcriber annotations (`[laughs]`, `(inaudible)`).
   Given the study has "With Probes" vs. "Without Probes" conditions,
   any interviewer probe text embedded in the merged file would be
   counted as if the participant said it -- directly contaminating the
   comparison the conditions are meant to test.

3. **Unanchored "no response" removal.** `\bno\s+response\b` deletes
   the phrase anywhere it occurs, including inside a genuine answer
   (e.g. "There was no response from my manager"), silently discarding
   real content rather than only recognizing a placeholder line.

4. **No per-question granularity.** Only a whole-transcript total per
   participant was produced, even though the script already parses
   question-header lines (just to delete them, not to segment). A
   single long answer can mask a "no response" elsewhere, and
   per-question breakdowns are needed to check whether the probe
   manipulation affects specific questions differently.

5. **Fragile/misleading implementation details.**
   - `total_words_via_counter` builds a `Counter` and sums
     `.values()` -- identical to `len(tokens)`, but the name and
     implementation misleadingly suggest something about unique words
     is being computed.
   - Missing files were recorded as the *string* `"NA"` in an
     otherwise numeric column, producing a mixed-type/object column
     that breaks simple aggregation downstream.
   - No `study`/`condition`/source-file columns, so once each
     condition's CSV is combined there's no way to trace a row back to
     its source without re-parsing folder names.
   - The script was five copy-pasted
     `TOP_LEVEL_FOLDER = ...` / `if __name__ == "__main__": main()`
     blocks. It "worked" only because Python executes top-to-bottom,
     but a single missing/renamed folder raises an unhandled
     `FileNotFoundError` and aborts every later condition in the same
     run.

## What changed in `word_counter.py`

- **Tokenizer** (`WORD_RE`) now accepts straight and curly apostrophes
  and treats internal hyphens as part of one word, both documented
  inline as decisions, not accidents.
- **`strip_noise`** removes timestamps, interviewer-labeled lines
  (`Interviewer:`, `I:`, `INT:` -- configurable via
  `INTERVIEWER_LABELS`), and non-verbal annotations. Square-bracket
  annotations (`[laughs]`) are stripped unconditionally; parenthetical
  content is only stripped when it matches a known non-verbal keyword
  list (`NONVERBAL_KEYWORDS`), because parentheses are also used in
  the interview questions themselves (e.g. Q3's
  "(or several others)") and in genuine participant speech.
- **`clean_response_text`** only treats "no response" as a placeholder
  when it is the entire trimmed line, preserving genuine mentions of
  the phrase inside real answers.
- **`segment_by_question`** uses the (whitespace-normalized) question
  headers to split the transcript into per-question segments, so
  per-question word counts are available in addition to the
  participant total. Question templates are compared after whitespace
  normalization, removing the need for hardcoded near-duplicate
  strings to cover minor formatting differences.
- **Disfluency handling is explicit and off by default**: filler-word
  stripping (`STRIP_FILLER_WORDS`, `FILLER_WORDS`) and immediate
  stutter-repeat collapsing (`STRIP_IMMEDIATE_REPEATS`) are flags with
  comments explaining the tradeoff, rather than silent omissions.
- **Output** includes `study`, `condition`, `source_file`,
  `file_found`, `raw_char_count`/`cleaned_char_count` (for QA -- how
  much of the raw file was excluded as noise), `total_word_count`,
  `distinct_word_count`, and one `<question_id>_word_count` column per
  question. Missing values use `NaN` via `pd.to_numeric(errors="coerce")`
  instead of the string `"NA"`.
- **`main`** now loops over a single `RUN_CONFIGS` list; a missing
  folder is logged with `[WARN]` and skipped, and processing continues
  for the remaining studies/conditions. A combined CSV
  (`merged_all_responses_word_counts.csv`) is written across all
  conditions in addition to each condition's own CSV, so downstream
  analysis doesn't need to re-parse folder names to know which
  study/condition a row belongs to.

## Verified behavior

Tested against a synthetic transcript containing a curly-quote-free
contraction, a hyphenated compound, an interviewer probe line, a
timestamp, a `(laughs)` annotation, a standalone "No response."
placeholder, and a genuine answer containing the phrase "no response."
All were handled as intended (interviewer/timestamp/annotation
excluded, contraction and hyphenated word counted as one token each,
placeholder zeroed out, genuine "no response" text preserved). Also
verified that a missing condition folder is skipped with a warning
instead of aborting the rest of the run.

## Remaining decisions for the research team

These are judgment calls that depend on the coding scheme, not bugs,
and are left as documented, off-by-default toggles:

- Whether filler words (um, uh) should count toward verbosity.
- Whether immediate stutter-repeats should be collapsed.
- Whether hyphenated compounds should count as one word or two
  (currently one; change the `-` out of `WORD_RE`'s character class to
  split them).
- Whether `INTERVIEWER_LABELS`/`PARTICIPANT_LABEL_RE` match your
  transcripts' actual speaker-label convention -- update the lists if
  your files use different tags.
