"""
Word counter for interview transcript files.

Revised version of Word_Counter.py. Changes from the original are
documented inline and summarized in transcript-word-counter/REVIEW.md.

Key behavioral changes:
  - Tokenizer now recognizes curly/smart apostrophes ('/'/`) in addition
    to the straight apostrophe, so contractions like "didn't" are not
    split into two tokens just because of quote style.
  - Interviewer speaker turns, timestamps, and bracketed/parenthetical
    transcriber annotations (e.g. "[laughs]", "(inaudible)") are now
    stripped before tokenization so they are never counted as
    participant words.
  - "No response" is only treated as a placeholder when it is the
    entire (trimmed) line, not whenever the phrase appears inside a
    real answer.
  - Question headers are used to *segment* the transcript by question
    (in addition to being removed), so per-question word counts are
    available alongside the participant total.
  - Filler-word and immediate-word-repeat handling are explicit,
    documented, off-by-default toggles rather than silent omissions.
  - Output includes study/condition/source-file metadata, proper
    missing-value handling (NaN instead of the string "NA"), and a
    raw-vs-cleaned character count for QA.
  - A single config-driven loop replaces the five copy-pasted
    module-level blocks; a missing/bad folder is logged and skipped
    instead of aborting the whole run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# =========================
# CONFIG
# =========================

# Each run processes one (study, condition, folder) combination.
# A missing folder logs a warning and is skipped rather than crashing
# the whole batch (see original bug: one missing folder halted all
# later conditions because the FileNotFoundError was unhandled).
RUN_CONFIGS = [
    ("Study 1", "Without Probes", Path("Study 1/Condition Without Probes")),
    ("Study 1", "With Probes", Path("Study 1/Condition With Probes")),
    ("Study 2", "Without Probes", Path("Study 2/Condition Without Probes")),
    ("Study 2", "With Probes", Path("Study 2/Condition With Probes")),
    ("Study 2", "Matching Response Length", Path("Study 2/Condition Matching Response Length")),
]

# Combined output across all studies/conditions (recommended for analysis).
COMBINED_OUTPUT_CSV = Path("merged_all_responses_word_counts.csv")

# Question headers, in question order. Whitespace is normalized before
# matching, so line wraps / double spaces / extra indentation no longer
# require a separate hardcoded variant per question (the original file
# carried two near-duplicate strings for Q1 to work around this).
QUESTIONS = [
    (
        "Q1",
        "Interview question 1: Can you describe a time when you were asked "
        "to provide ideas for solving work issues in which you didn't have "
        "much experience?",
    ),
    ("Q2", "Interview question 2: Describe a situation where you delegated effectively."),
    (
        "Q3",
        "Interview question 3: Describe a time when you successfully "
        "persuaded someone (or several others) to see or do things your way",
    ),
]

# Only treat "no response" as a placeholder when it is the *entire*
# trimmed line (optionally followed by punctuation), not whenever the
# phrase occurs inside a genuine answer (e.g. "There was no response
# from my manager"), which the original unanchored regex would delete.
NO_RESPONSE_LINE_RE = re.compile(r"^\s*no\s+response\.?\s*$", re.IGNORECASE)

# Lines that open an interviewer turn, e.g. "Interviewer:", "I:", "INT:".
# Add labels here to match your transcription convention.
INTERVIEWER_LABELS = ["interviewer", "int", "i"]
INTERVIEWER_LINE_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(lbl) for lbl in INTERVIEWER_LABELS) + r")\s*:\s*.*$",
    re.IGNORECASE,
)

# Optional: if participant turns are also explicitly labeled (e.g.
# "Participant:", "P:"), stripping just the label (not the turn) keeps
# the answer text while removing the tag itself from the word count.
PARTICIPANT_LABEL_RE = re.compile(r"^\s*(?:participant|p|r|respondent)\s*:\s*", re.IGNORECASE)

# Timestamps like 00:01:23, [00:01:23], (00:12), 1:02:03.
TIMESTAMP_RE = re.compile(r"[\[(]?\b\d{1,2}:\d{2}(?::\d{2})?\b[\])]?")

# Transcriber annotations of non-verbal content. Square brackets are
# stripped unconditionally, since that convention ("[laughs]",
# "[inaudible]", "[crosstalk]") is reserved for transcriber notes.
# Parentheses are NOT stripped unconditionally -- interview questions
# and genuine participant speech legitimately use parentheses (see
# question 3's "(or several others)"), so only a known list of
# non-verbal keywords is stripped when it appears inside parentheses.
BRACKETED_ANNOTATION_RE = re.compile(r"\[[^\[\]]*\]")
NONVERBAL_KEYWORDS = [
    "laughs?", "laughing", "chuckles?", "sighs?", "coughs?",
    "inaudible", "crosstalk", "pause", "silence", "unintelligible",
]
NONVERBAL_PAREN_RE = re.compile(
    r"\((?:" + "|".join(NONVERBAL_KEYWORDS) + r")\)", re.IGNORECASE
)

# Tokenizer. Recognizes straight and curly apostrophes for contractions
# ("didn't" / "didn't") and treats internal hyphens as part of a single
# word ("well-being", "follow-up", "co-worker" each count as ONE word,
# not two). If your analysis wants hyphenated compounds split instead,
# drop the "-" from the character class below.
APOSTROPHE_CHARS = "'’‘`"
WORD_RE = re.compile(
    r"[A-Za-z0-9]+(?:[" + APOSTROPHE_CHARS + r"-][A-Za-z0-9]+)*"
)

# --- Disfluency handling: explicit, off-by-default, documented -------
# These affect what counts as a "word" and materially change results,
# so they are exposed as flags rather than silently baked in.

# Filler words to optionally exclude from the count (um, uh, etc.).
FILLER_WORDS = {"um", "umm", "uh", "uhh", "erm", "hmm"}
STRIP_FILLER_WORDS = False

# Collapse immediate stutter-repeats ("the the meeting" -> "the
# meeting") to a single occurrence. Off by default because repeated
# words can be meaningful emphasis ("very very good"), not just a
# false start; enable only if your coding scheme wants disfluency-
# adjusted counts.
STRIP_IMMEDIATE_REPEATS = False


# =========================
# DATA MODEL
# =========================


@dataclass
class ParticipantResult:
    study: str
    condition: str
    participant_id: str
    source_file: str | None
    file_found: bool
    raw_char_count: int | None = None
    cleaned_char_count: int | None = None
    total_word_count: int | None = None
    distinct_word_count: int | None = None
    per_question_word_counts: dict[str, int] = field(default_factory=dict)


# =========================
# HELPERS
# =========================


def _normalize_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _build_question_line_res(questions: list[tuple[str, str]]) -> list[tuple[str, re.Pattern]]:
    """Builds one whitespace-insensitive, whole-line regex per question.

    Whitespace in the header text is normalized before compiling, so a
    question line with extra spaces, tabs, or a mid-sentence line wrap
    still matches without needing a hardcoded duplicate string per
    variant (the original script carried two near-identical Q1 strings
    to cover exactly this case).
    """
    compiled = []
    for qid, text in questions:
        normalized = _normalize_whitespace(text)
        pattern = re.escape(normalized).replace(r"\ ", r"\s+")
        compiled.append((qid, re.compile(r"^\s*" + pattern + r"\s*$", re.IGNORECASE)))
    return compiled


QUESTION_LINE_RES = _build_question_line_res(QUESTIONS)


def strip_noise(raw: str) -> str:
    """Removes timestamps, non-verbal annotations, and interviewer turns.

    Order matters: annotations/timestamps are stripped before line-based
    interviewer detection so a line like "[00:01:23] Interviewer: ..."
    is still recognized as an interviewer line after the timestamp is
    gone.
    """
    s = TIMESTAMP_RE.sub(" ", raw)
    s = BRACKETED_ANNOTATION_RE.sub(" ", s)
    s = NONVERBAL_PAREN_RE.sub(" ", s)

    kept_lines = []
    for line in s.splitlines():
        if not line.strip():
            kept_lines.append(line)
            continue
        if INTERVIEWER_LINE_RE.match(line):
            continue
        line = PARTICIPANT_LABEL_RE.sub("", line)
        kept_lines.append(line)
    return "\n".join(kept_lines)


def segment_by_question(text: str) -> dict[str, str]:
    """Splits transcript text into per-question segments.

    Text before the first recognized question header is discarded
    (front matter / file header). Any question header line found is
    used as a delimiter and then excluded from its own segment's text.
    Returns {question_id: segment_text}; a question absent from the
    file simply is not a key in the result (its count should be
    reported as missing/NaN, not 0, so a missing question is
    distinguishable from a genuinely empty response).
    """
    segments: dict[str, list[str]] = {}
    current_qid = None
    for line in text.splitlines():
        matched_qid = None
        for qid, qre in QUESTION_LINE_RES:
            if qre.match(line):
                matched_qid = qid
                break
        if matched_qid is not None:
            current_qid = matched_qid
            segments.setdefault(current_qid, [])
            continue
        if current_qid is not None:
            segments[current_qid].append(line)
    return {qid: "\n".join(lines) for qid, lines in segments.items()}


def clean_response_text(raw: str) -> str:
    """Removes the standalone 'No response' placeholder line.

    Only matches when the entire trimmed line is 'no response' (plus
    optional trailing period), so a genuine answer that happens to
    contain that phrase (e.g. "There was no response from my manager")
    is left intact.
    """
    kept_lines = [
        line for line in raw.splitlines() if not NO_RESPONSE_LINE_RE.match(line)
    ]
    return "\n".join(kept_lines)


def tokenize(text: str) -> list[str]:
    tokens = WORD_RE.findall((text or "").lower())

    if STRIP_IMMEDIATE_REPEATS:
        deduped = []
        for tok in tokens:
            if not deduped or deduped[-1] != tok:
                deduped.append(tok)
        tokens = deduped

    if STRIP_FILLER_WORDS:
        tokens = [tok for tok in tokens if tok not in FILLER_WORDS]

    return tokens


def count_words(text: str) -> tuple[int, int]:
    """Returns (total_word_count, distinct_word_count) for text."""
    tokens = tokenize(text)
    return len(tokens), len(set(tokens))


def find_merged_file_for_pid(pid_folder: Path, pid: str) -> Path | None:
    """Searches a participant folder for <pid>_merged_all_responses.txt."""
    target_re = re.compile(rf"^{re.escape(pid)}_merged_all_responses\.txt$", re.IGNORECASE)
    for p in pid_folder.rglob("*.txt"):
        if target_re.match(p.name):
            return p
    return None


def process_participant_file(study: str, condition: str, pid_folder: Path) -> ParticipantResult:
    participant_id = pid_folder.name
    merged_path = find_merged_file_for_pid(pid_folder, participant_id)

    if merged_path is None:
        return ParticipantResult(
            study=study,
            condition=condition,
            participant_id=participant_id,
            source_file=None,
            file_found=False,
        )

    raw = merged_path.read_text(encoding="utf-8", errors="replace").strip()
    denoised = strip_noise(raw)
    segments = segment_by_question(denoised)

    per_question_counts: dict[str, int] = {}
    all_segment_text = []
    for qid, _ in QUESTIONS:
        segment_text = segments.get(qid)
        if segment_text is None:
            continue
        segment_text = clean_response_text(segment_text)
        wc, _ = count_words(segment_text)
        per_question_counts[qid] = wc
        all_segment_text.append(segment_text)

    # Total is computed from the concatenated per-question segments
    # (i.e. participant responses only), not the raw file, so any
    # front matter/header content outside a recognized question is
    # excluded from the total by construction.
    combined_text = "\n".join(all_segment_text)
    total_wc, distinct_wc = count_words(combined_text)

    return ParticipantResult(
        study=study,
        condition=condition,
        participant_id=participant_id,
        source_file=str(merged_path),
        file_found=True,
        raw_char_count=len(raw),
        cleaned_char_count=len(combined_text),
        total_word_count=total_wc,
        distinct_word_count=distinct_wc,
        per_question_word_counts=per_question_counts,
    )


def results_to_dataframe(results: list[ParticipantResult]) -> pd.DataFrame:
    question_ids = [qid for qid, _ in QUESTIONS]
    rows = []
    for r in results:
        row = {
            "study": r.study,
            "condition": r.condition,
            "participant_id": r.participant_id,
            "source_file": r.source_file,
            "file_found": r.file_found,
            "raw_char_count": r.raw_char_count,
            "cleaned_char_count": r.cleaned_char_count,
            "total_word_count": r.total_word_count,
            "distinct_word_count": r.distinct_word_count,
        }
        for qid in question_ids:
            row[f"{qid}_word_count"] = r.per_question_word_counts.get(qid)
        rows.append(row)

    df = pd.DataFrame(rows)
    # Proper missing values (NaN) instead of the string "NA", so the
    # numeric columns stay numeric (df["total_word_count"].mean() works
    # without a manual coercion step downstream).
    numeric_cols = ["raw_char_count", "cleaned_char_count", "total_word_count", "distinct_word_count"]
    numeric_cols += [f"{qid}_word_count" for qid in question_ids]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# =========================
# MAIN
# =========================


def run_config(study: str, condition: str, top_level_folder: Path) -> list[ParticipantResult]:
    if not top_level_folder.exists():
        print(f"[WARN] Skipping {study} / {condition}: folder not found: {top_level_folder}")
        return []

    participant_folders = sorted(p for p in top_level_folder.iterdir() if p.is_dir())
    if not participant_folders:
        print(f"[WARN] {study} / {condition}: no participant folders under {top_level_folder}")
        return []

    results = [
        process_participant_file(study, condition, pf) for pf in participant_folders
    ]

    df = results_to_dataframe(results)
    output_path = top_level_folder / "merged_all_responses_word_counts.csv"
    df.to_csv(output_path, index=False)
    print(f"[OK] {study} / {condition}: saved {len(df)} rows to {output_path}")
    return results


def main() -> None:
    all_results: list[ParticipantResult] = []
    for study, condition, folder in RUN_CONFIGS:
        all_results.extend(run_config(study, condition, folder))

    if not all_results:
        raise FileNotFoundError(
            "No participant data found for any configured study/condition. "
            "Check the RUN_CONFIGS paths."
        )

    combined_df = results_to_dataframe(all_results)
    combined_df.to_csv(COMBINED_OUTPUT_CSV, index=False)
    print(f"Saved combined word counts to: {COMBINED_OUTPUT_CSV}")

    missing = combined_df.loc[~combined_df["file_found"], "participant_id"].tolist()
    if missing:
        print(f"[WARN] {len(missing)} participant folder(s) had no merged file: {missing}")


if __name__ == "__main__":
    main()
