#!/usr/bin/env python3
"""
AI Code Review Bot
-------------------
Fetches a GitHub PR's changed files, sends the diffs to an LLM (Gemini by
default, Groq optional) for review, and posts the results back as inline
review comments on the pull request.

Required environment variables:
    GITHUB_TOKEN     - token with `pull-requests: write` + `contents: read`
    GEMINI_API_KEY    - Gemini API key (or GROQ_API_KEY if LLM_PROVIDER=groq)

Optional environment variables:
    GITHUB_EVENT_PATH   - path to the GitHub Actions event payload
                          (default: /github/workflow/event.json, GitHub
                          Actions sets this automatically)
    GITHUB_REPOSITORY   - "owner/repo" (GitHub Actions sets this automatically)
    LLM_PROVIDER        - "gemini" (default) or "groq"
    MAX_DIFF_LINES      - total diff lines allowed before truncation (default 2000)
    MAX_FILES           - max number of files to review in one run (default 25)
    MODEL_NAME          - override the default model name
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

from github import Github, GithubException

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").lower()
EVENT_PATH = os.environ.get("GITHUB_EVENT_PATH", "/github/workflow/event.json")
REPO_NAME = os.environ.get("GITHUB_REPOSITORY")
MAX_DIFF_LINES = int(os.environ.get("MAX_DIFF_LINES", "2000"))
MAX_FILES = int(os.environ.get("MAX_FILES", "25"))
MODEL_NAME = os.environ.get(
    "MODEL_NAME",
    "gemini-2.0-flash" if LLM_PROVIDER == "gemini" else "llama-3.3-70b-versatile",
)

# File extensions worth reviewing. Skip lockfiles, images, binaries, etc.
REVIEWABLE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rb", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".php", ".swift", ".kt", ".scala",
    ".sh", ".sql", ".yml", ".yaml", ".tf", ".dockerfile",
}
SKIP_FILENAMES = {
    "package-lock.json", "yarn.lock", "poetry.lock", "Cargo.lock", "go.sum",
}

SEVERITY_ICONS = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🔵",
    "NIT": "⚪",
}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class ReviewComment:
    file_path: str
    line_number: int
    severity: str
    comment: str


@dataclass
class FileDiff:
    path: str
    patch: str
    valid_lines: set = field(default_factory=set)


# --------------------------------------------------------------------------
# Step 1: Load the PR event + fetch changed files
# --------------------------------------------------------------------------

def load_event() -> dict:
    if not os.path.exists(EVENT_PATH):
        print(f"::error::Event payload not found at {EVENT_PATH}")
        sys.exit(1)
    with open(EVENT_PATH, "r") as f:
        return json.load(f)


def get_pr_number(event: dict) -> int:
    if "pull_request" in event:
        return event["pull_request"]["number"]
    # Fallback for workflow_dispatch / manual testing
    if "number" in event:
        return event["number"]
    print("::error::Could not find a pull_request number in the event payload")
    sys.exit(1)


def extract_valid_lines(patch: str) -> set:
    """
    Parse a unified diff patch and return the set of line numbers (in the
    NEW version of the file) that actually appear in the diff. GitHub's
    review-comment API only accepts line numbers that are part of the diff
    hunk, so we validate against this set before posting.
    """
    valid_lines = set()
    new_line_num = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                new_line_num = int(match.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            valid_lines.add(new_line_num)
            new_line_num += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue  # removed lines don't exist in the new file
        else:
            valid_lines.add(new_line_num)
            new_line_num += 1
    return valid_lines


def get_changed_files(pr) -> list:
    files = []
    for f in pr.get_files():
        if f.status == "removed" or f.patch is None:
            continue
        if f.filename in SKIP_FILENAMES:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext and ext not in REVIEWABLE_EXTENSIONS:
            continue
        files.append(FileDiff(
            path=f.filename,
            patch=f.patch,
            valid_lines=extract_valid_lines(f.patch),
        ))
        if len(files) >= MAX_FILES:
            print(f"::warning::PR has more than {MAX_FILES} reviewable files; "
                  f"only reviewing the first {MAX_FILES}.")
            break
    return files


# --------------------------------------------------------------------------
# Step 2: Chunk / truncate diffs to respect token limits
# --------------------------------------------------------------------------

def truncate_diffs(files: list, max_lines: int) -> list:
    """
    Distribute a global line budget across files. Larger PRs get each file
    truncated proportionally rather than dropping whole files, so the model
    still sees a representative sample of every changed file.
    """
    total_lines = sum(len(f.patch.splitlines()) for f in files)
    if total_lines <= max_lines or not files:
        return files

    print(f"::notice::Diff is {total_lines} lines, over the {max_lines} line "
          f"budget. Truncating per-file.")
    budget_per_file = max(50, max_lines // len(files))
    for f in files:
        lines = f.patch.splitlines()
        if len(lines) > budget_per_file:
            kept = lines[:budget_per_file]
            kept.append(
                f"... [diff truncated: {len(lines) - budget_per_file} "
                f"more lines omitted to fit token budget] ..."
            )
            f.patch = "\n".join(kept)
    return files


# --------------------------------------------------------------------------
# Step 3: Prompt construction + LLM call
# --------------------------------------------------------------------------

SYSTEM_INSTRUCTIONS = """You are a senior software engineer performing an \
automated pull request code review. You will be given one file's diff (in \
unified diff format) at a time.

Review it for:
- Security vulnerabilities (SQL injection, XSS, secrets in code, unsafe deserialization, etc.)
- Bugs and logic errors
- Performance bottlenecks
- Code smells and maintainability issues
- Missing error handling

Respond with ONLY a JSON array (no markdown fences, no prose before or \
after). Each element must have this exact shape:

{"file_path": "<string>", "line_number": <integer>, "severity": \
"<CRITICAL|HIGH|MEDIUM|LOW|NIT>", "comment": "<string, 1-3 sentences>"}

Rules:
- line_number MUST refer to a line number in the NEW version of the file, \
and MUST be a line that is visible in the diff you were given (an added \
line or a context line), never a purely deleted line.
- Only flag genuine issues. If the diff has no issues, return an empty \
array: []
- Do not invent line numbers. Do not comment on style preferences unless \
they cause a real bug.
- Return at most 8 comments for this file, prioritizing the most severe \
issues.
"""


def build_prompt(file_diff: FileDiff) -> str:
    return (
        f"{SYSTEM_INSTRUCTIONS}\n\n"
        f"FILE: {file_diff.path}\n"
        f"DIFF:\n```diff\n{file_diff.patch}\n```\n\n"
        f"JSON array:"
    )


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_gemini(prompt: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    body = _post_json(url, payload, {"Content-Type": "application/json"})
    try:
        return body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        print(f"::warning::Unexpected Gemini response shape: {body}")
        return "[]"


def call_groq(prompt: str) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }
    body = _post_json(url, payload, headers)
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        print(f"::warning::Unexpected Groq response shape: {body}")
        return "[]"


def call_llm(prompt: str, retries: int = 3) -> str:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            if LLM_PROVIDER == "groq":
                return call_groq(prompt)
            return call_gemini(prompt)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"::warning::LLM call failed (attempt {attempt}/{retries}): "
                  f"{e}. Retrying in {wait}s.")
            time.sleep(wait)
    print(f"::error::LLM call failed after {retries} attempts: {last_err}")
    return "[]"


def parse_llm_response(raw: str, file_diff: FileDiff) -> list:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    # Sometimes models wrap the array in an object like {"issues": [...]}.
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not match:
            print(f"::warning::Could not parse LLM JSON for {file_diff.path}")
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            print(f"::warning::Could not parse LLM JSON for {file_diff.path}")
            return []

    if isinstance(parsed, dict):
        parsed = next((v for v in parsed.values() if isinstance(v, list)), [])

    comments = []
    for item in parsed:
        try:
            line_number = int(item["line_number"])
            severity = str(item.get("severity", "MEDIUM")).upper()
            if severity not in SEVERITY_ICONS:
                severity = "MEDIUM"
            if line_number not in file_diff.valid_lines:
                continue  # skip comments the API would reject
            comments.append(ReviewComment(
                file_path=file_diff.path,
                line_number=line_number,
                severity=severity,
                comment=str(item["comment"]).strip(),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return comments


def review_file(file_diff: FileDiff) -> list:
    prompt = build_prompt(file_diff)
    raw = call_llm(prompt)
    return parse_llm_response(raw, file_diff)


# --------------------------------------------------------------------------
# Step 4: Post the review back to GitHub
# --------------------------------------------------------------------------

def build_review_body(all_comments: list, files_reviewed: int) -> str:
    if not all_comments:
        return (
            "## 🤖 AI Code Review\n\n"
            f"Reviewed {files_reviewed} file(s) — no issues found. Nice work! ✅"
        )
    counts = {}
    for c in all_comments:
        counts[c.severity] = counts.get(c.severity, 0) + 1
    summary_line = " · ".join(
        f"{SEVERITY_ICONS[sev]} {sev}: {n}" for sev, n in sorted(counts.items())
    )
    return (
        "## 🤖 AI Code Review\n\n"
        f"Reviewed {files_reviewed} file(s), found **{len(all_comments)}** "
        f"item(s).\n\n{summary_line}\n\n"
        "_Automated review — use judgment, and flag false positives in the "
        "PR thread._"
    )


def post_review(pr, all_comments: list, files_reviewed: int):
    body = build_review_body(all_comments, files_reviewed)

    if not all_comments:
        pr.create_issue_comment(body)
        return

    review_comments = [
        {
            "path": c.file_path,
            "line": c.line_number,
            "side": "RIGHT",
            "body": f"{SEVERITY_ICONS[c.severity]} **{c.severity}**: {c.comment}",
        }
        for c in all_comments
    ]

    try:
        pr.create_review(
            body=body,
            event="COMMENT",
            comments=review_comments,
        )
    except GithubException as e:
        # If the batch review fails (e.g. one bad line slipped through),
        # fall back to a single summary comment so the run doesn't just fail.
        print(f"::warning::create_review failed ({e}); posting summary only.")
        fallback = body + "\n\n---\n\n" + "\n\n".join(
            f"**{c.file_path}:{c.line_number}** — "
            f"{SEVERITY_ICONS[c.severity]} {c.severity}: {c.comment}"
            for c in all_comments
        )
        pr.create_issue_comment(fallback)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    if not GITHUB_TOKEN:
        print("::error::GITHUB_TOKEN is not set")
        sys.exit(1)
    if LLM_PROVIDER == "gemini" and not GEMINI_API_KEY:
        print("::error::GEMINI_API_KEY is not set")
        sys.exit(1)
    if LLM_PROVIDER == "groq" and not GROQ_API_KEY:
        print("::error::GROQ_API_KEY is not set")
        sys.exit(1)
    if not REPO_NAME:
        print("::error::GITHUB_REPOSITORY is not set")
        sys.exit(1)

    event = load_event()
    pr_number = get_pr_number(event)

    gh = Github(GITHUB_TOKEN)
    repo = gh.get_repo(REPO_NAME)
    pr = repo.get_pull(pr_number)

    print(f"Reviewing PR #{pr_number} in {REPO_NAME} ({pr.title!r})")

    files = get_changed_files(pr)
    if not files:
        print("No reviewable files changed. Skipping.")
        pr.create_issue_comment(
            "## 🤖 AI Code Review\n\nNo reviewable source files changed in this PR."
        )
        return

    files = truncate_diffs(files, MAX_DIFF_LINES)

    all_comments = []
    for file_diff in files:
        print(f"Reviewing {file_diff.path} ({len(file_diff.patch.splitlines())} diff lines)...")
        comments = review_file(file_diff)
        print(f"  -> {len(comments)} finding(s)")
        all_comments.extend(comments)

    post_review(pr, all_comments, len(files))
    print(f"Done. Posted {len(all_comments)} inline comment(s) on {len(files)} file(s).")


if __name__ == "__main__":
    main()
