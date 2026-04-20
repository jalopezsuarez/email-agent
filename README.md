# email-agent

Three cooperating Python agents that read a personal Microsoft 365 inbox,
classify mail into the user's existing folder structure, and draft replies
imitating the user's own writing style. Sent Items are also mined as a prior
for the kinds of correspondents the user actually replies to, so the
"personal" detector can lean on real reply history instead of prompt-only
guessing. **The system never sends email.**

```
           ┌─────────────────────┐
Outlook →  │ EmailCoordinator    │  polls INBOX, dispatches, logs
           └─┬─────────────┬─────┘
             │             │
             ▼             ▼
  ┌──────────────────┐ ┌──────────────────┐
  │ EmailClassifier  │ │ EmailResponder   │
  │ (SQLite +        │ │ (learns tone +   │
  │  LanceDB)        │ │  reply history)  │
  └──────────────────┘ └──────────────────┘
                           │
                           ▼
                  Drafts → "Atendidos IA"
```

## Safety model (mandatory)

1. **Scope `Mail.Send` is never requested.** The OAuth app cannot send email
   even if bypassed client-side — Graph rejects with `403 Forbidden`.
2. A client-side guard (`email_agent/services/safety.py`) blocks any HTTP
   request whose path targets a send endpoint. Tests pin this behaviour
   (`tests/test_safety.py`).
3. The only outbound operations allowed are `createReply` (draft), `PATCH
   /me/messages/{id}` (draft body edit), and `/move` (folder move).
4. The "Approve" button in the panel only marks a draft as user-reviewed.
   It does NOT send. The user sends manually from Outlook.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill MS_CLIENT_ID and the API key for the LLM provider you choose in .env
python main.py --port 8765
```

Complete the Microsoft device-code login printed in the console. Then open
<http://localhost:8765/> and use the HTML panel.

## LLM providers

Configure in `config.yaml` (or override from the panel):

- `gemini` (default, free tier)
- `openai`
- `claude`
- `ollama` (local, `http://localhost:11434`)

`openai`, `claude`, and `gemini` now accept explicit `base_url` and `api_key`
configuration. `Claude` does not offer embeddings, so when you use it for
generation you must pair it with `llm.embedding_provider` set to `openai`,
`gemini`, or `ollama`.

Only install the SDKs you use; all providers go through `httpx` so no SDK
is strictly required.

## Running inside `gemini-cli`

The `gemini` provider reads `GEMINI_API_KEY` from the environment, the same
variable `gemini-cli` uses. Authenticating once with `gemini-cli` is enough
to let this app reuse the free-tier key; the panel then runs as a regular
HTTP server on your chosen port.

## Progressive autonomy

The classifier starts conservative: it only auto-classifies when final
confidence ≥ `classifier.confidence_threshold` (default **0.90**). Anything
below is parked in *Pendientes* for you to resolve manually. Every manual
resolution is stored as a high-weight feedback sample in LanceDB so that
future runs become progressively more accurate. Lower the threshold from
the Configuration tab when the dashboard shows consistent high confidence.

The Sent Items training step now helps in two places: it improves reply
drafting and it gives the personal/work detector historical evidence about
which senders or domains the user already exchanges direct replies with.

## Tests

```bash
pytest
```

`tests/test_safety.py` is non-negotiable: it verifies both the path guard
and that no send-shaped method exists on `GraphClient`.
