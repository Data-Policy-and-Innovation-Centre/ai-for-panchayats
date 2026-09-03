#!/usr/bin/env bash
# Push OPENAI_API_KEY from the local gitignored .env into Secrets Manager and
# roll the service onto it.
#
# The key cannot live in the image (public repo, ECR) and is deliberately not
# set by Terraform (anything Terraform sets lands in state), so the value has
# to be delivered out of band. This is that step, made repeatable and checked.
#
# The guard that matters is the last one. Ask/main.py only tests that
# OPENAI_API_KEY is non-empty, so a wrong, unfunded or wrongly-scoped key
# starts the app cleanly and then fails every call into a polite fallback:
# health checks pass, the page renders, and every user gets "I couldn't find a
# question that matches". Nothing looks broken. So this script refuses to
# report success unless it has seen the deployed task answer a real question.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-ap-south-1}"
NAME="${NAME:-prdw-chatbot}"
SECRET_ID="${SECRET_ID:-$NAME/openai-api-key}"
URL="${URL:-https://d1ahy2bofi6eol.cloudfront.net}"

# Probe credentials are validated HERE, before anything is mutated. Checking
# caller input after the secret has been rotated and the service rolled is the
# same ordering mistake this probe block exists to fix: the destructive half
# succeeds and the operator learns they mistyped a variable afterwards.
CHATBOT_USER="${CHATBOT_USER:-}"
CHATBOT_PASSWORD="${CHATBOT_PASSWORD:-}"
if [[ -n "$CHATBOT_USER$CHATBOT_PASSWORD" && ( -z "$CHATBOT_USER" || -z "$CHATBOT_PASSWORD" ) ]]; then
  echo "[key] set both CHATBOT_USER and CHATBOT_PASSWORD, or neither" >&2; exit 2
fi
CHAT_MODEL="${CHAT_MODEL:-gpt-5.4-mini}"
EMBED_MODEL="${EMBED_MODEL:-text-embedding-3-large}"
ROLL_TIMEOUT="${ROLL_TIMEOUT:-1500}"   # entrypoint fetches ~1GB before uvicorn

# Secrets never travel in argv: `ps auxww` is world-readable, so a key passed
# as a flag is visible to every local process. curl reads its Authorization
# header from a config file and the AWS CLI reads the value from file://.
TMPD="$(mktemp -d)"; chmod 700 "$TMPD"
trap 'rm -rf "$TMPD"' EXIT

if [[ -z "${OPENAI_API_KEY:-}" && -f "$REPO_ROOT/.env" ]]; then
  # Tolerate `export KEY=`, CRLF line endings and surrounding quotes. Interior
  # characters are preserved: stripping spaces would silently corrupt a value.
  # -E throughout: BSD sed (macOS, where this is run) has no \+ or \? in basic
  # regex and would silently match nothing, reporting a present key as missing.
  OPENAI_API_KEY="$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?OPENAI_API_KEY=//p' \
                    "$REPO_ROOT/.env" | head -1 | tr -d '\r' \
                    | sed -E -e 's/^"(.*)"$/\1/' -e "s/^'(.*)'\$/\1/")"
fi

: "${OPENAI_API_KEY:?set OPENAI_API_KEY in .env or the environment}"
[[ "$OPENAI_API_KEY" == PLACEHOLDER* ]] && { echo "[key] refusing: still the placeholder" >&2; exit 1; }

printf 'header = "Authorization: Bearer %s"\n' "$OPENAI_API_KEY" > "$TMPD/curlrc"
printf '%s' "$OPENAI_API_KEY" > "$TMPD/key"

probe() { # $1 endpoint, $2 json body, $3 label
  local body code
  body=$(curl -sS --max-time 60 -w '\n%{http_code}' --config "$TMPD/curlrc" \
         -H 'Content-Type: application/json' -d "$2" "https://api.openai.com/v1/$1") || {
           echo "[key] $3 probe could not reach OpenAI. Nothing was written." >&2; exit 1; }
  code=$(printf '%s' "$body" | tail -1)
  if [[ "$code" != "200" ]]; then
    echo "[key] $3 probe rejected (HTTP $code). Nothing was written." >&2
    printf '%s\n' "$body" | sed '$d' | head -c 400 >&2; echo >&2
    exit 1
  fi
}

# Both probes, because they fail independently. A key on an account with no
# credits authenticates but 429s on spend; a project-scoped key can be allowed
# embeddings and denied the chat models the router actually routes with. Chat
# needs real token headroom: these are reasoning models and spend tokens
# internally before emitting output, so a tiny cap returns 400, not an answer.
echo "[key] validating against OpenAI before writing anything"
probe chat/completions "{\"model\":\"$CHAT_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"max_completion_tokens\":256}" chat
probe embeddings "{\"model\":\"$EMBED_MODEL\",\"input\":\"ok\"}" embedding
echo "[key] ok - authenticates, has quota, and can reach both models"

# Captured before the write so a bad outcome has a way back.
PREV=$(aws secretsmanager describe-secret --secret-id "$SECRET_ID" --region "$REGION" \
       --query 'VersionIdsToStages' --output json 2>/dev/null \
       | python3 -c 'import sys,json;d=json.load(sys.stdin);print(next((k for k,v in d.items() if "AWSCURRENT" in v),""))' || true)

# NEW is captured, not just printed: AWSCURRENT is attached to this version
# the moment the write lands, and update-secret-version-stage refuses to move
# a label that is already attached elsewhere unless the command names the
# version to remove it from ("you must include this parameter" -- the CLI's
# own reference). A rollback hint without it is a command that fails.
NEW=$(aws secretsmanager put-secret-value --secret-id "$SECRET_ID" \
  --secret-string "file://$TMPD/key" --region "$REGION" --query VersionId --output text)
echo "[key] wrote version $NEW"

rollback_hint() {
  echo "[key] To roll the secret back: aws secretsmanager update-secret-version-stage \\" >&2
  echo "[key]   --secret-id $SECRET_ID --version-stage AWSCURRENT \\" >&2
  echo "[key]   --move-to-version-id ${PREV:-<previous>} --remove-from-version-id ${NEW:-<current>} \\" >&2
  echo "[key]   --region $REGION" >&2
  echo "[key] Then roll the service again: aws ecs update-service --cluster $NAME --service $NAME \\" >&2
  echo "[key]   --force-new-deployment --region $REGION" >&2
}

# The value is read once at task start, so the running task keeps the old one.
echo "[key] rolling the service"
DEPLOY_ID=$(aws ecs update-service --cluster "$NAME" --service "$NAME" \
  --force-new-deployment --region "$REGION" --query 'service.deployments[0].id' --output text)
echo "[key] deployment $DEPLOY_ID"

# Not `aws ecs wait services-stable`: its budget is a fixed 10 minutes, which a
# cold start can exceed while fetching and checksumming the snapshot, and it can
# also return on the PRE-roll steady state and hand us the old task to verify.
# Poll the named deployment instead.
deadline=$((SECONDS + ROLL_TIMEOUT)); state=""; rolled=0
while (( SECONDS < deadline )); do
  read -r pid state running <<<"$(aws ecs describe-services --cluster "$NAME" --services "$NAME" \
    --region "$REGION" --query 'services[0].deployments[?status==`PRIMARY`]|[0].[id,rolloutState,runningCount]' \
    --output text 2>/dev/null || echo ". . .")"
  [[ "$pid" == "$DEPLOY_ID" && "$state" == "COMPLETED" && "${running:-0}" -ge 1 ]] && { rolled=1; break; }
  [[ "$state" == "FAILED" ]] && { echo "[key] rollout FAILED (circuit breaker may have rolled back)" >&2; rollback_hint; exit 1; }
  sleep 15
done
# A flag set at the break, not a re-test of one variable. Checking `state`
# alone was wrong in a way that mattered: if ECS restores an earlier
# deployment, PRIMARY becomes a DIFFERENT id that is already COMPLETED, the
# loop correctly keeps waiting, and then the deadline passes with state ==
# COMPLETED -- so the old postcondition declared success and the probe ran
# against a task still holding the OLD key. The check now cannot disagree
# with the condition it is meant to confirm.
if (( ! rolled )); then
  echo "[key] rollout did not complete within ${ROLL_TIMEOUT}s" >&2
  echo "[key]   wanted deployment $DEPLOY_ID COMPLETED with at least one running task" >&2
  echo "[key]   saw deployment ${pid:-unknown} ${state:-unknown} running ${running:-0}" >&2
  rollback_hint; exit 1
fi
echo "[key] rollout complete"

# A 200 proved nothing last time. Fail closed: a missing or unparseable tier is
# a failure, not a pass, because the consumer app is pinned only at image build
# time and its response shape can drift underneath us.
# The endpoint is behind CloudFront Basic authentication (#59), so an
# unauthenticated probe gets a 401 and this script would report FAILURE on
# every run -- after it had already rotated the secret and rolled the service.
# Worst possible order: the destructive half succeeds, the reporting half lies.
#
# Credentials come from the environment first so a caller can supply them
# without Terraform, then from the app module's own output. Never in argv:
# `ps auxww` is world-readable.
if [[ -z "$CHATBOT_PASSWORD" ]] && command -v terraform >/dev/null 2>&1; then
  CHATBOT_USER="$(terraform -chdir="$REPO_ROOT/infra/terraform/app" output -raw basic_auth_username 2>/dev/null || true)"
  CHATBOT_PASSWORD="$(terraform -chdir="$REPO_ROOT/infra/terraform/app" output -raw basic_auth_password 2>/dev/null || true)"
fi
# curl reads a config value as a quoted string in which \\ and \" are escapes,
# so a password containing either is silently mangled -- `a\b"c` transmits as
# `ab`. Terraform generates this one without punctuation, but an env-supplied
# credential is outside our control.
esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
supplied_creds=0
if [[ -n "$CHATBOT_PASSWORD" ]]; then
  printf 'user = "%s:%s"\n' "$(esc "$CHATBOT_USER")" "$(esc "$CHATBOT_PASSWORD")" > "$TMPD/probe"
  supplied_creds=1
else
  : > "$TMPD/probe"
fi

echo "[key] checking a real question"
set +e
ans=$(curl -sS --max-time 90 -X POST "$URL/query" -H 'Content-Type: application/json' \
      --config "$TMPD/probe" -w '\n%{http_code}' \
      -d '{"message":"What is the total actual expenditure under each focus area in 2024-2025?"}')
rc=$?
set -e
[[ $rc -ne 0 ]] && { echo "[key] could not reach $URL (curl $rc). Key IS written; service IS rolled." >&2; rollback_hint; exit 1; }

code="${ans##*$'\n'}"
ans="${ans%$'\n'*}"
if [[ "$code" == "401" ]]; then
  if [[ "$supplied_creds" == "1" ]]; then
    echo "[key] the endpoint rejected the credentials used, so the key could NOT be verified." >&2
    echo "[key] They may be stale: the pilot password is rotated by replacing random_password.basic_auth." >&2
  else
    echo "[key] the endpoint asked for credentials and none were available, so the key could NOT be verified." >&2
  fi
  echo "[key] The key IS written and the service IS rolled; only the proof is missing." >&2
  echo "[key] Verify by hand with the current pilot credentials:" >&2
  echo "[key]   CHATBOT_USER=\$(terraform -chdir=infra/terraform/app output -raw basic_auth_username) \\" >&2
  echo "[key]   CHATBOT_PASSWORD=\$(terraform -chdir=infra/terraform/app output -raw basic_auth_password) \\" >&2
  echo "[key]   CHATBOT_URL=$URL uv run python scripts/benchmark_deployment.py --repeat 1" >&2
  # Deliberately no rollback_hint. A 401 means the PROBE could not
  # authenticate; it says nothing about whether the new key works. Rolling the
  # secret back here would undo a good rotation on the strength of an
  # unrelated failure.
  exit 1
fi
if [[ "$code" != "200" ]]; then
  echo "[key] probe returned HTTP $code, not 200. Key IS written; service IS rolled." >&2
  rollback_hint
  exit 1
fi

printf '%s' "$ans" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    sys.exit(f"[key] response was not JSON: {e}")
t = d.get("tier")
if not isinstance(t, str):
    sys.exit(f"[key] no usable tier field (got {t!r}) - cannot confirm the key works")
# clarify and fallback are both non-answers, and both are HTTP 200. The same
# classification benchmark_deployment.py uses: a deployment that never
# executes a query must not pass a check whose whole point is proving it can.
if t in ("fallback", "clarify"):
    sys.exit(f"[key] tier={t} - the router declined to answer, so the key is unproven")
if not d.get("query_id"):
    sys.exit(f"[key] tier={t} but no query_id - no query was executed")
print(f"[key] tier={t} query_id={d['query_id']} - the deployed task answered a real question")
' || { echo "[key] verification FAILED." >&2; rollback_hint; exit 1; }
