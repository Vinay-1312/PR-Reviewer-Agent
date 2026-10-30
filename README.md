# PR Reviewer

A GitHub App that automatically reviews pull requests with Gemini and posts the result as a PR review (inline comments plus a summary). It runs on Google Cloud (Cloud Run, Pub/Sub, Firestore) and is provisioned with Terraform.

## How it works

```
GitHub webhook ─▶ receiver (public) ─▶ Pub/Sub ─▶ worker (private) ─▶ Gemini
                                          │              │
                                          ▼              └─▶ GitHub review
                                    dead-letter queue
```

1. **Receiver** ([src/pr_reviewer/receiver/app.py](src/pr_reviewer/receiver/app.py)) is the public webhook endpoint (`POST /webhook`). It verifies the `X-Hub-Signature-256` HMAC, keeps only `opened`, `synchronize`, `reopened` and `ready_for_review` events on non-draft PRs, skips authors who are not trusted, and publishes a small message to Pub/Sub.
2. **Worker** ([src/pr_reviewer/worker/app.py](src/pr_reviewer/worker/app.py)) is a private Cloud Run service that receives Pub/Sub pushes (`POST /pubsub`, authenticated with an OIDC token). For each message it:
   - claims the review in Firestore so duplicate deliveries are skipped,
   - fetches the PR and diff using a GitHub App installation token,
   - skips PRs that are closed, empty, or already superseded by a newer commit,
   - asks Gemini for a structured review,
   - sanitizes the model output and posts it as a single GitHub review.

### Reliability

- The worker returns `2xx` to acknowledge a message and `503` to make Pub/Sub retry. Only transient failures (rate limits, 5xx, network errors) are retried; permanent failures are logged and acknowledged.
- Firestore keeps a 5-minute lease per PR commit, so a crashed worker's review can be reclaimed on retry.
- After 7 failed deliveries, messages move to a dead-letter topic (`pr-events-dlq`) and are kept for 7 days on a pull subscription.
- If GitHub rejects an inline comment position (HTTP 422), the review is re-posted with all notes in the body.

### Security

- **Webhook signatures** are verified with a constant-time comparison.
- **Trusted authors only.** PR content is untrusted input to the model, so by default only `OWNER`, `MEMBER` and `COLLABORATOR` PRs are reviewed. Configure with `TRUSTED_ASSOCIATIONS` (`*` for everyone).
- **Prompt-injection hardening.** The title, description and diff are wrapped in tags with a random per-request suffix, and the system prompt tells the model to ignore instructions inside them.
- **Output sanitization** ([src/pr_reviewer/llm/sanitize.py](src/pr_reviewer/llm/sanitize.py)) strips images, links, `@mentions`, HTML and control characters from model output, and caps comment length and count (20 inline comments).
- **Secrets** live in Secret Manager; Terraform creates only the containers so values stay out of state.
- Each service runs under its own least-privilege service account.

## Project layout

```
src/pr_reviewer/
  receiver/   public webhook endpoint
  worker/     Pub/Sub push endpoint and review pipeline
  github/     App auth (JWT -> installation token), REST client, diff annotation
  llm/        Gemini reviewer, system prompt, output sanitizer
  storage/    Firestore dedupe / lease store
  common/     message schema shared by receiver and worker
  main.tf     Terraform for all GCP resources
docker/       Dockerfiles and Cloud Build configs for receiver and worker
tests/        pytest suite
```

## Configuration

**Receiver**

| Variable | Description |
| --- | --- |
| `PROJECT_ID` | GCP project ID |
| `TOPIC` | Pub/Sub topic name (`pr-events`) |
| `GITHUB_WEBHOOK_SECRET` | Webhook secret (from Secret Manager) |
| `TRUSTED_ASSOCIATIONS` | Comma-separated author associations to review, or `*`. Default `OWNER,MEMBER,COLLABORATOR` |

**Worker**

| Variable | Description |
| --- | --- |
| `GITHUB_APP_ID` | GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY` | App private key, PEM (from Secret Manager) |
| `GEMINI_API_KEY` | Gemini API key (from Secret Manager) |
| `GEMINI_MODEL` | Model name, default `gemini-3.8-flash` |

## Local development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev          # install receiver + worker + test dependencies
uv run pytest                # run the tests

# run a service locally (set the environment variables above first)
PYTHONPATH=src uv run uvicorn pr_reviewer.receiver.app:app --port 8080
PYTHONPATH=src uv run uvicorn pr_reviewer.worker.app:app --port 8081
```

Both services expose `GET /healthz`.

## Deployment

Deployment is two-phase because the secrets and images must exist before Cloud Run can start.

**1. Create the infrastructure** (APIs, service accounts, Pub/Sub, Firestore, Artifact Registry, secret containers):

```bash
cd src/pr_reviewer
terraform init
terraform apply -var project_id=YOUR_PROJECT
```

**2. Create the GitHub App** with a webhook secret, a private key, `Pull requests: Read & write` permission, and the *Pull request* event subscription. Then add the secret values:

```bash
printf '%s' "$WEBHOOK_SECRET" | gcloud secrets versions add github-webhook-secret --data-file=-
gcloud secrets versions add github-app-private-key --data-file=app-private-key.pem
printf '%s' "$GEMINI_API_KEY" | gcloud secrets versions add gemini-api-key --data-file=-
```

**3. Build and push the images** from the repo root:

```bash
gcloud builds submit --config docker/receiver/cloudbuild.yaml \
  --substitutions _IMAGE=us-central1-docker.pkg.dev/YOUR_PROJECT/pr-review/receiver:v1 .
gcloud builds submit --config docker/worker/cloudbuild.yaml \
  --substitutions _IMAGE=us-central1-docker.pkg.dev/YOUR_PROJECT/pr-review/worker:v1 .
```

**4. Deploy the services:**

```bash
terraform apply -var project_id=YOUR_PROJECT -var deploy_services=true \
  -var receiver_image=us-central1-docker.pkg.dev/YOUR_PROJECT/pr-review/receiver:v1 \
  -var worker_image=us-central1-docker.pkg.dev/YOUR_PROJECT/pr-review/worker:v1 \
  -var github_app_id=YOUR_APP_ID
```

**5. Point the GitHub App's webhook URL** at `<receiver_url>/webhook` (see the `receiver_url` Terraform output), then install the app on your repositories.

### Terraform variables

| Variable | Default | Description |
| --- | --- | --- |
| `project_id` | required | GCP project with billing enabled |
| `region` | `us-central1` | Deployment region |
| `deploy_services` | `false` | Set `true` once secrets and images exist |
| `receiver_image`, `worker_image` | `""` | Container image URLs |
| `github_app_id` | `""` | GitHub App ID |
| `trusted_author_associations` | `OWNER,MEMBER,COLLABORATOR` | Author associations to review |
| `gemini_model` | `gemini-3.8-flash` | Gemini model for reviews |
| `billing_account` | `""` | Billing account ID; leave empty to skip the budget alert |
| `budget_amount`, `budget_currency` | `5`, `USD` | Budget alert threshold (alerts at 50% and 100%) |

## Troubleshooting

- **`401 invalid signature`**: the webhook secret in Secret Manager does not match the GitHub App's. The receiver logs the reason (missing header, bad format, or mismatch) without revealing the secret.
- **No review appears**: check that the PR is not a draft and that the author's association is in `TRUSTED_ASSOCIATIONS`. The receiver responds with `ignored` and a reason.
- **Messages in the dead-letter queue**: pull from `pr-events-dlq-pull` to inspect them, and check worker logs for the permanent failure.
