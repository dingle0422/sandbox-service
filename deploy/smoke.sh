#!/usr/bin/env bash
# sandbox_service 端到端冒烟：对已起的服务（docker-compose.smoke.yml）打一整条会话生命周期，
# 覆盖真 Docker + 真网络路径——这是 fake 后端单测碰不到的地方：
#   容器 create/start、就绪探测、通用代理（含 SSE 流式）、workspace 文件 CRUD、销毁。
#
# 用法（先起编排）：
#   docker compose -f deploy/docker-compose.smoke.yml up -d --build
#   bash deploy/smoke.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8001}"
SID="${SID:-smoke-$(date +%s)}"
PORT="${AGENT_PORT:-8080}"
PASS=0
FAIL=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
need() { command -v "$1" >/dev/null || { echo "缺少命令: $1"; exit 2; }; }

need curl
need jq

say "0. 服务健康 (GET /health)"
if curl -fsS "$BASE/health" | jq -e '.ok == true' >/dev/null; then
  ok "/health ok=true"
else
  bad "/health 不可达或 ok!=true —— 服务是否已起？(docker compose ... up -d)"
  exit 1
fi

say "1. 创建沙箱 + 就绪探测 (POST /sandboxes)"
CREATE=$(curl -fsS -X POST "$BASE/sandboxes" \
  -H 'content-type: application/json' \
  -d "{\"id\":\"$SID\",\"wait_ready\":{\"path\":\"/agent/health\",\"timeout_s\":60}}")
echo "$CREATE" | jq .
if echo "$CREATE" | jq -e '.status == "ready" or .status == "reused"' >/dev/null; then
  ok "沙箱就绪 status=$(echo "$CREATE" | jq -r .status)"
else
  bad "创建/就绪失败"; exit 1
fi

say "2. 通用代理透传健康 (GET /sandboxes/$SID/proxy/$PORT/agent/health)"
HP=$(curl -fsS "$BASE/sandboxes/$SID/proxy/$PORT/agent/health")
echo "$HP" | jq .
echo "$HP" | jq -e '.contractVersion | startswith("1")' >/dev/null \
  && ok "代理到 agent，contractVersion=$(echo "$HP" | jq -r .contractVersion)" \
  || bad "代理健康异常"

say "3. 提交 run (POST .../proxy/$PORT/agent/input) → 202"
IN=$(curl -fsS -o /dev/null -w '%{http_code}' -X POST \
  "$BASE/sandboxes/$SID/proxy/$PORT/agent/input" \
  -H 'content-type: application/json' \
  -d "{\"run_id\":\"run-1\",\"session_id\":\"$SID\",\"user_text\":\"hello sandbox\"}")
[ "$IN" = "202" ] && ok "input 返回 202" || bad "input 返回 $IN（期望 202）"

say "4. SSE 事件流透传 (GET .../proxy/$PORT/agent/events)"
EV=$(curl -fsS -N --max-time 15 "$BASE/sandboxes/$SID/proxy/$PORT/agent/events" || true)
echo "$EV" | sed 's/^/    /'
echo "$EV" | grep -q 'RUN_STARTED'  && ok "收到 RUN_STARTED"  || bad "缺 RUN_STARTED"
echo "$EV" | grep -q '__finalize__' && ok "收到 __finalize__ 收尾" || bad "缺 __finalize__"
echo "$EV" | grep -q 'echo: hello sandbox' && ok "echo 语义正确" || bad "echo 内容不符"

say "5. workspace 文件读写 (PUT/GET .../workspace/files/...)"
curl -fsS -X PUT "$BASE/sandboxes/$SID/workspace/files/note.txt" \
  -H 'content-type: application/json' -d '{"content":"hi from smoke"}' >/dev/null
RD=$(curl -fsS "$BASE/sandboxes/$SID/workspace/files/note.txt" | jq -r .content)
[ "$RD" = "hi from smoke" ] && ok "文件写入后读回一致" || bad "文件读回不一致: $RD"

say "6. 路径逃逸防护 (GET .../workspace/files/%2e%2e/%2e%2e/etc/passwd → 403)"
ESC=$(curl -fsS -o /dev/null -w '%{http_code}' \
  "$BASE/sandboxes/$SID/workspace/files/%2e%2e/%2e%2e/etc/passwd" || true)
[ "$ESC" = "403" ] && ok "逃逸被拦 (403)" || bad "逃逸未拦，返回 $ESC（期望 403）"

say "7. 销毁沙箱 (DELETE /sandboxes/$SID)"
curl -fsS -X DELETE "$BASE/sandboxes/$SID" | jq .
sleep 1
LEFT=$(docker ps -a --filter "label=sandbox-service.sandbox_id=$SID" --format '{{.ID}}' || true)
[ -z "$LEFT" ] && ok "agent 容器已清理" || bad "残留容器: $LEFT"

say "结果"
printf '  通过 %d / 失败 %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && { echo "  冒烟全绿 ✅"; exit 0; } || { echo "  存在失败 ❌"; exit 1; }
