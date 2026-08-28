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

aws secretsmanager put-secret-value --secret-id "$SECRET_ID" \
  --secret-string "file://$TMPD/key" --region "$REGION" --query VersionId --output text

# The value is read once at task start, so the running task keeps the old one.
echo "[key] rolling the service"
DEPLOY_ID=$(aws ecs update-service --cluster "$NAME" --service "$NAME" \
  --force-new-deployment --region "$REGION" --query 'service.deployments[0].id' --output text)
echo "[key] deployment $DEPLOY_ID"

# Not `aws ecs wait services-stable`: its budget is a fixed 10 minutes, which a
# cold start can exceed while fetching and checksumming the snapshot, and it can
# also return on the PRE-roll steady state and hand us the old task to verify.
# Poll the named deployment instead.
deadline=$((SECONDS + ROLL_TIMEOUT)); state=""
while (( SECONDS < deadline )); do
  read -r pid state running <<<"$(aws ecs describe-services --cluster "$NAME" --services "$NAME" \
    --region "$REGION" --query 'services[0].deployments[?status==`PRIMARY`]|[0].[id,rolloutState,runningCount]' \
    --output text 2>/dev/null || echo ". . .")"
  [[ "$pid" == "$DEPLOY_ID" && "$state" == "COMPLETED" && "${running:-0}" -ge 1 ]] && break
  [[ "$state" == "FAILED" ]] && { echo "[key] rollout FAILED (circuit breaker may have rolled back)" >&2; exit 1; }
  sleep 15
done
if [[ "$state" != "COMPLETED" ]]; then
  echo "[key] rollout did not complete within ${ROLL_TIMEOUT}s (last state: ${state:-unknown})" >&2; exit 1
fi
echo "[key] rollout complete"

# A 200 proved nothing last time. Fail closed: a missing or unparseable tier is
# a failure, not a pass, because the consumer app is pinned only at image build
# time and its response shape can drift underneath us.
echo "[key] checking a real question"
set +e
ans=$(curl -sS --max-time 90 -X POST "$URL/query" -H 'Content-Type: application/json' \
      -d '{"message":"What is the total actual expenditure under each focus area in 2024-2025?"}')
rc=$?
set -e
[[ $rc -ne 0 ]] && { echo "[key] could not reach $URL (curl $rc). Key IS written; service IS rolled." >&2; exit 1; }

printf '%s' "$ans" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    sys.exit(f"[key] response was not JSON: {e}")
t = d.get("tier")
if not isinstance(t, str):
    sys.exit(f"[key] no usable tier field (got {t!r}) - cannot confirm the key works")
if t == "fallback":
    sys.exit("[key] tier=fallback - the key is not working for the router")
print(f"[key] tier={t} - the deployed task answered a real question")
' || { echo "[key] verification FAILED. To roll back: aws secretsmanager update-secret-version-stage \\
  --secret-id $SECRET_ID --version-stage AWSCURRENT --move-to-version-id ${PREV:-<previous>} --region $REGION" >&2; exit 1; }
