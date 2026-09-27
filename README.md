# AI Code Review Bot

A self-hosted GitHub Action that reviews pull request diffs with an LLM
(Gemini by default, Groq optional) and posts findings as **inline comments**
on the exact changed lines — security issues, bugs, performance problems,
and code smells.

## How it works (pipeline overview)

```
PR opened/updated
      │
      ▼
GitHub Actions triggers ai-review.yml
      │
      ▼
Docker container runs reviewer.py
      │
      ├─ 1. Read GITHUB_EVENT_PATH → get PR number
      ├─ 2. PyGithub → pr.get_files() → per-file diff patches
      ├─ 3. extract_valid_lines() → parse each unified diff into the set
      │      of line numbers GitHub will actually accept a comment on
      ├─ 4. truncate_diffs() → cap total diff lines (default 2000),
      │      truncating proportionally per file instead of dropping files
      ├─ 5. For each file: build_prompt() → call_llm() (Gemini/Groq)
      │      → model returns a JSON array of {file_path, line_number,
      │        severity, comment}
      ├─ 6. parse_llm_response() → validate + drop any line number the
      │      model hallucinated that isn't actually in the diff
      └─ 7. post_review() → POST /pulls/{n}/reviews with all inline
             comments in a single review, with a summary body
```

## Files

| File | Purpose |
|---|---|
| `reviewer.py` | Core logic: fetch diffs, prompt the LLM, post the review |
| `requirements.txt` | Python deps (`PyGithub`) |
| `Dockerfile` | Containerizes `reviewer.py` for GitHub Actions |
| `action.yml` | Defines the Action's inputs and Docker entrypoint |
| `.github/workflows/ai-review.yml` | Example consumer workflow |

## Setup

### 1. Get an API key
- Gemini: [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) (free tier available)
- Groq (alternative, faster/cheaper): [console.groq.com/keys](https://console.groq.com/keys)

### 2. Publish the Action
Push this folder as its own repo, e.g. `your-username/ai-code-review-bot`,
then tag a release (`git tag v1 && git push origin v1`) so other repos can
reference `your-username/ai-code-review-bot@v1`.

*(For local testing, skip publishing and just reference `./` from a
workflow in the same repo — see "Local/same-repo testing" below.)*

### 3. Add the secret
In the repo that will **use** the bot: **Settings → Secrets and variables →
Actions → New repository secret**
- Name: `GEMINI_API_KEY`
- Value: your key

### 4. Add the workflow
Copy `.github/workflows/ai-review.yml` into the target repo, updating the
`uses:` line to point at your published tag.

### 5. Open a PR
The bot runs automatically on `opened`, `synchronize` (new commits pushed),
and `reopened` events, and posts a review with inline comments.

## Local / same-repo testing

If you don't want to publish the Action separately yet, put `action.yml`,
`Dockerfile`, and `reviewer.py` at the root of the *same* repo as your
workflow, and reference it as:

```yaml
- uses: ./
  with:
    gemini_key: ${{ secrets.GEMINI_API_KEY }}
```

You can also dry-run the script locally without Docker:

```bash
export GITHUB_TOKEN=ghp_xxx
export GEMINI_API_KEY=xxx
export GITHUB_REPOSITORY=your-username/some-test-repo
export GITHUB_EVENT_PATH=./sample_event.json   # a PR "opened" payload
pip install -r requirements.txt
python reviewer.py
```

Grab a sample event payload by copying the JSON from GitHub's
[webhook payload docs](https://docs.github.com/en/webhooks/webhook-events-and-payloads#pull_request)
and filling in a real PR number/repo you have access to.

## Configuration (Action inputs)

| Input | Default | Description |
|---|---|---|
| `gemini_key` | — | Gemini API key |
| `llm_provider` | `gemini` | `gemini` or `groq` |
| `groq_key` | — | Groq API key (if `llm_provider: groq`) |
| `max_diff_lines` | `2000` | Total diff line budget before truncation kicks in |
| `max_files` | `25` | Max files reviewed per run |
| `model_name` | provider default | Override model, e.g. `gemini-2.0-flash` |

## Design notes / why it's built this way

- **Line-safe comments**: GitHub's review API rejects comments on lines not
  present in the diff. `extract_valid_lines()` parses each unified diff
  hunk header (`@@ -a,b +c,d @@`) and walks it to build the exact set of
  valid "new file" line numbers, and any LLM-hallucinated line number
  outside that set is silently dropped rather than crashing the run.
- **Token budget handling**: rather than truncating the *last* files in a
  huge PR (which biases review coverage toward the first files), the line
  budget is split proportionally across all changed files.
- **Batch review, not one-comment-per-call**: all inline comments are sent
  in a single `POST /pulls/{n}/reviews` call so they land as one review
  instead of spamming the PR timeline with individual comment events.
- **Resilience**: LLM calls retry with exponential backoff; if the batched
  review call itself fails (e.g. GitHub rejects one comment), the script
  falls back to posting a single consolidated issue comment so the whole
  run doesn't fail silently.

## Extending it

- Swap the JSON-mode prompt for tool calling if you move to the OpenAI/
  Anthropic APIs.
- Add a `.aireviewignore` file and skip globs from it in `get_changed_files`.
- Post a `REQUEST_CHANGES` review event instead of `COMMENT` when any
  `CRITICAL` finding exists, to block merges via branch protection.
