"""
Нутри — сервер ИИ для Yandex Cloud Functions (Python 3.12, только стандартная библиотека).

Принимает от сайта и Mini App:  {"messages":[{"role":"user"|"assistant","text":"..."}], "anon_id":"...", "utm":{...}}
Возвращает:                      {"reply":"..."}

Переменные окружения функции:
  YANDEX_API_KEY   — API-ключ сервисного аккаунта (роль ai.languageModels.user)   ОБЯЗАТЕЛЬНО
  FOLDER_ID        — идентификатор каталога Yandex Cloud                           ОБЯЗАТЕЛЬНО
  MODEL            — модель, по умолчанию deepseek-v4.1-flash (дешевле: qwen3.6-35b-a3b)
  ALLOWED_ORIGINS  — адреса сайта через запятую, например https://k1b35.github.io  (по умолчанию *)
  MAX_TOKENS       — максимум токенов в ответе, по умолчанию 700
  DAILY_LIMIT      — сообщений в сутки на одного пользователя, по умолчанию 40
  API_BASE         — адрес API, по умолчанию https://ai.api.cloud.yandex.net/v1
"""
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("API_BASE", "https://ai.api.cloud.yandex.net/v1").rstrip("/")
API_KEY = os.environ.get("YANDEX_API_KEY", "")
FOLDER_ID = os.environ.get("FOLDER_ID", "")
MODEL = os.environ.get("MODEL", "deepseek-v4.1-flash")
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "700"))
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "40"))
MAX_MESSAGES = 16      # сколько последних реплик отправляем модели
MAX_CHARS = 2000       # максимум символов в одной реплике
TIMEOUT_SEC = 25       # поставьте таймаут функции 30 с

_DIR = os.path.dirname(os.path.abspath(__file__))


def _read(name, default=""):
    try:
        with open(os.path.join(_DIR, name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


SYSTEM_PROMPT = _read("system_prompt.md")
try:
    RECIPES = json.loads(_read("recipes.json", "{}"))
except ValueError:
    RECIPES = {}

# Счётчик сообщений живёт в памяти экземпляра функции: это мягкая защита от злоупотреблений, не точный учёт.
_usage = {}


def _recipes_block(utm_content):
    ready = {k: v for k, v in RECIPES.items() if isinstance(v, dict) and v.get("recipe") and not v.get("draft")}
    if not ready:
        return ""
    lines = ["", "## Рецепты выпусков (утверждены врачом)"]
    for key, r in ready.items():
        lines.append(f"\n### {key}: {r.get('title', key)}\n{r['recipe'].strip()}")
    if utm_content in ready:
        lines.append(f"\nПользователь пришёл по QR из выпуска {utm_content} («{ready[utm_content].get('title', utm_content)}»). "
                     "Если он не назвал другое блюдо, речь об этом рецепте.")
    return "\n".join(lines)


def _model_uri():
    # Для Yandex AI Studio модель задаётся как gpt://<каталог>/<модель>; для других провайдеров — как есть
    if MODEL.startswith("gpt://") or "yandex" not in API_BASE:
        return MODEL
    return f"gpt://{FOLDER_ID}/{MODEL}"


def _cors(origin):
    if "*" in ALLOWED_ORIGINS:
        allow = "*"
    elif origin.rstrip("/") in ALLOWED_ORIGINS:
        allow = origin
    else:
        allow = ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else "*"
    return {
        "Access-Control-Allow-Origin": allow,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
        "Content-Type": "application/json; charset=utf-8",
    }


def _resp(code, payload, headers):
    return {"statusCode": code, "headers": headers, "body": json.dumps(payload, ensure_ascii=False)}


def _clean_messages(raw):
    out = []
    for m in (raw or [])[-MAX_MESSAGES:]:
        if not isinstance(m, dict):
            continue
        role = "assistant" if m.get("role") == "assistant" else "user"
        text = str(m.get("text") or m.get("content") or "").strip()[:MAX_CHARS]
        if text:
            out.append({"role": role, "content": text})
    while out and out[0]["role"] == "assistant":  # начинаем с реплики пользователя
        out.pop(0)
    return out


def _limit_ok(anon):
    day = time.strftime("%Y-%m-%d", time.gmtime())
    d, n = _usage.get(anon, (day, 0))
    if d != day:
        n = 0
    if n >= DAILY_LIMIT:
        return False
    _usage[anon] = (day, n + 1)
    return True


def _call_model(messages):
    payload = {
        "model": _model_uri(),
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": MAX_TOKENS,
    }
    req = urllib.request.Request(
        API_BASE + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
            "OpenAI-Project": FOLDER_ID,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)       # на случай рассуждений в тексте
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                      # чат показывает простой текст
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    return text.strip(), data.get("usage") or {}


def handler(event, context):
    headers_in = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    cors = _cors(headers_in.get("origin", ""))
    method = (event.get("httpMethod") or "POST").upper()

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": cors, "body": ""}
    if method == "GET":
        return _resp(200, {"ok": True, "model": MODEL}, cors)   # проверка, что функция жива
    if method != "POST":
        return _resp(405, {"error": "method_not_allowed"}, cors)

    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", "replace")
    try:
        data = json.loads(body or "{}")
    except ValueError:
        return _resp(400, {"error": "bad_json"}, cors)

    messages = _clean_messages(data.get("messages"))
    if not messages or messages[-1]["role"] != "user":
        return _resp(400, {"error": "no_user_message"}, cors)

    anon = str(data.get("anon_id") or headers_in.get("x-forwarded-for", "anon"))[:64]
    anon_hash = hashlib.sha256(anon.encode()).hexdigest()[:12]
    if not _limit_ok(anon_hash):
        return _resp(429, {"error": "daily_limit", "reply": "На сегодня лимит сообщений исчерпан. Возвращайтесь завтра!"}, cors)

    if not API_KEY or not FOLDER_ID:
        return _resp(500, {"error": "server_not_configured"}, cors)

    utm = data.get("utm") if isinstance(data.get("utm"), dict) else {}
    system = SYSTEM_PROMPT + _recipes_block(str(utm.get("utm_content", "")))
    t0 = time.time()
    try:
        reply, usage = _call_model([{"role": "system", "content": system}] + messages)
    except urllib.error.HTTPError as e:
        print(json.dumps({"event": "model_error", "status": e.code, "detail": e.read()[:300].decode("utf-8", "replace")}, ensure_ascii=False))
        return _resp(502, {"error": "model_error"}, cors)
    except Exception as e:  # таймаут, сеть
        print(json.dumps({"event": "model_error", "detail": str(e)[:300]}, ensure_ascii=False))
        return _resp(502, {"error": "model_unavailable"}, cors)

    if not reply:
        reply = "Не получилось сформулировать ответ. Попробуйте переформулировать вопрос."

    # Лог без текста сообщений: только то, что нужно для метрик и стоимости
    print(json.dumps({
        "event": "chat",
        "anon": anon_hash,
        "utm": utm,
        "n_user_msgs": sum(1 for m in messages if m["role"] == "user"),
        "tokens_in": usage.get("prompt_tokens"),
        "tokens_out": usage.get("completion_tokens"),
        "ms": int((time.time() - t0) * 1000),
        "model": MODEL,
    }, ensure_ascii=False))
    return _resp(200, {"reply": reply}, cors)
