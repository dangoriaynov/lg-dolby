#!/usr/bin/env python3
"""subs-translate — переклад SRT англійською → українською локальною моделлю.

Виконується всередині LXC `media-server`, ходить до llama-server у контейнері
`llm`. Нічого не йде назовні.

    subs-translate.py <файл.en.srt> [--out файл.uk.srt] [--batch 20] [--dry N]

Чому не просто «переклади рядок за рядком»:

* **Тайм-коди недоторканні.** Перекладається лише текст між мітками, самі мітки
  копіюються байт у байт — тому синхронність результату дорівнює синхронності
  джерела, зіпсувати її неможливо.
* **Контекст.** Кожна пачка бачить попередні репліки і їхній переклад, інакше
  займенники й звертання «пливуть»: та сама фраза перекладається то на «ти»,
  то на «ви».
* **Глосарій.** Імена й повторювані слова фіксуються один раз наперед, щоб
  персонаж не був «Панда» в одній репліці й «Пандою» в іншій.
* **Перевірка довжини.** Модель зобов'язана повернути рівно стільки рядків,
  скільки отримала. Якщо ні — пачка ділиться навпіл, у крайньому разі
  перекладається порядково. Зсув на один рядок зіпсував би весь фільм.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

LLM_URL = os.environ.get("LLM_URL", "http://10.48.230.217:8081")

SYSTEM = """Ти перекладаєш субтитри повнометражного дитячого мультфільму з англійської українською.

Правила:
- Жива розмовна українська, як говорять із дітьми. Не канцелярит, не калька з англійської.
- Вигуки й звуконаслідування передавай українськими відповідниками, не транслітеруй.
- Субтитр читають за секунди: тримай рядок коротким, без зайвих слів.
- НЕ додавай пояснень, приміток, лапок навколо відповіді.
- Зберігай перенос рядка всередині репліки як \\n.
- Порожній рядок на вході — порожній рядок на виході.
- Повертай ЛИШЕ JSON: {"lines": [...]} — рівно стільки елементів, скільки на вході."""


def llm(messages, max_tokens=3000, temperature=0.3, timeout=600):
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Без цього gemma пише довгий ланцюжок міркувань у reasoning_content:
        # 3.2 с проти 0.2 с на репліку, тобто години замість хвилин на фільм.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(LLM_URL + "/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"].get("content") or ""


def model_name():
    req = urllib.request.Request(LLM_URL + "/v1/models")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["data"][0]["id"]


def unfence(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
    return text.strip()


def parse_json_lines(text, expected):
    """Витягти список рядків із відповіді моделі."""
    raw = unfence(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    lines = data.get("lines") if isinstance(data, dict) else data
    if not isinstance(lines, list) or len(lines) != expected:
        return None
    return ["" if x is None else str(x) for x in lines]


# --------------------------------------------------------------------------
# SRT
# --------------------------------------------------------------------------

TIMING = re.compile(r"-->")


def parse_srt(path):
    raw = open(path, encoding="utf-8-sig", errors="replace").read()
    cues = []
    for block in re.split(r"\r?\n\r?\n", raw.strip()):
        lines = [l.rstrip("\r") for l in block.split("\n") if l.strip() != ""]
        if not lines:
            continue
        idx = 0
        if not TIMING.search(lines[0]):
            idx = 1
        if idx >= len(lines) or not TIMING.search(lines[idx]):
            continue  # блок без тайм-коду — пропускаємо
        cues.append({"number": lines[0] if idx == 1 else str(len(cues) + 1),
                     "timing": lines[idx],
                     "text": lines[idx + 1:]})
    return cues


def write_srt(path, cues, translations):
    out = []
    for i, (cue, text) in enumerate(zip(cues, translations), start=1):
        body = text.replace("\\n", "\n").strip("\n")
        out.append("%d\n%s\n%s\n" % (i, cue["timing"], body))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))


# --------------------------------------------------------------------------
# глосарій
# --------------------------------------------------------------------------

STOP = {"The", "And", "But", "You", "Your", "That", "This", "What", "When", "Where",
        "Why", "How", "There", "Here", "They", "Then", "With", "For", "Not", "Are",
        "Was", "Yes", "No", "Oh", "Ah", "Well", "Now", "Come", "Look", "Let", "All",
        "Its", "Have", "Just", "Like", "Okay", "Hey", "Get", "Good", "Don", "Oi"}


def build_glossary(cues):
    text = " ".join(" ".join(c["text"]) for c in cues)
    counts = {}
    for word in re.findall(r"\b[A-Z][a-z]{2,}\b", text):
        if word in STOP:
            continue
        counts[word] = counts.get(word, 0) + 1
    names = [w for w, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n >= 3][:25]
    if not names:
        return {}
    answer = llm([
        {"role": "system", "content": "Ти складаєш глосарій для перекладу дитячого "
                                      "мультфільму. Поверни ЛИШЕ JSON-обʼєкт "
                                      "{\"англійське\":\"українське\"} без пояснень."},
        {"role": "user", "content": "Дай усталені українські відповідники цих імен і слів "
                                    "з мультфільму про панд (Японія, 1972). Якщо це звичайне "
                                    "слово, а не імʼя — теж переклади.\n"
                                    + json.dumps(names, ensure_ascii=False)},
    ], max_tokens=1200)
    try:
        data = json.loads(unfence(answer))
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except (json.JSONDecodeError, AttributeError):
        return {}


# --------------------------------------------------------------------------
# переклад
# --------------------------------------------------------------------------

def translate_batch(sources, glossary, context, attempt=0):
    """Повертає список перекладів тієї ж довжини, або None."""
    hints = ""
    if glossary:
        hints = "\n\nГлосарій (тримайся його): " + json.dumps(glossary, ensure_ascii=False)
    if context:
        hints += "\n\nПопередні репліки для контексту (НЕ перекладай їх повторно):\n" + \
                 "\n".join("EN: %s\nUA: %s" % (s, t) for s, t in context[-3:])
    strict = ""
    if attempt:
        strict = ("\n\nУВАГА: попередня спроба повернула хибну кількість рядків. "
                  "Поверни РІВНО %d елементів." % len(sources))
    answer = llm([
        {"role": "system", "content": SYSTEM + hints + strict},
        {"role": "user", "content": json.dumps({"lines": sources}, ensure_ascii=False)},
    ], max_tokens=200 + 120 * len(sources))
    return parse_json_lines(answer, len(sources))


def translate_all(cues, glossary, batch_size, log):
    sources = ["\\n".join(c["text"]) for c in cues]
    result, context, stats = [], [], {"retries": 0, "splits": 0, "single": 0}

    def run(chunk):
        for attempt in range(3):
            try:
                got = translate_batch(chunk, glossary, context, attempt)
            except (urllib.error.URLError, TimeoutError) as exc:
                log("   мережа: %s — повтор" % exc)
                time.sleep(5)
                continue
            if got is not None:
                return got
            stats["retries"] += 1
        if len(chunk) == 1:
            stats["single"] += 1
            return [chunk[0]]  # лишаємо оригінал, ніж зсунути весь файл
        stats["splits"] += 1
        half = len(chunk) // 2
        return run(chunk[:half]) + run(chunk[half:])

    total = (len(sources) + batch_size - 1) // batch_size
    for n, start in enumerate(range(0, len(sources), batch_size), start=1):
        chunk = sources[start:start + batch_size]
        began = time.time()
        got = run(chunk)
        result.extend(got)
        context.extend(zip(chunk, got))
        log("   пачка %d/%d — %d реплік за %.1f с" % (n, total, len(chunk), time.time() - began))
    return result, stats


# --------------------------------------------------------------------------

def consistency_pass(sources, translations, log):
    """Однакова репліка в оригіналі — однаковий переклад скрізь.

    Пачки перекладаються незалежно, тому приспів пісні або повторюване
    звертання щоразу виходить трохи інакшим ("Матусю, Матусю" / "Матусю,
    матусю"). У діалозі це дрібниця, у пісні — чутно одразу. Тут ми збираємо
    всі різночитання, просимо модель обрати один найкращий варіант і
    підставляємо його в усі входження.
    """
    groups = {}
    for i, (src, dst) in enumerate(zip(sources, translations)):
        key = src.strip().lower()
        if len(key) > 6:
            groups.setdefault(key, []).append(i)

    conflicts = {k: v for k, v in groups.items()
                 if len({translations[i].strip() for i in v}) > 1}
    if not conflicts:
        log("узгодження: різночитань немає")
        return translations, 0

    log("узгодження: %d повторюваних реплік перекладено по-різному" % len(conflicts))
    fixed = 0
    for key, idxs in conflicts.items():
        variants = sorted({translations[i].strip() for i in idxs})
        try:
            answer = llm([
                {"role": "system", "content":
                    "Ти редактор субтитрів дитячого мультфільму. Тобі дають англійський "
                    "рядок і кілька його українських перекладів. Обери НАЙКРАЩИЙ (точний, "
                    "живий, короткий) або напиши свій, якщо всі погані. Поверни ЛИШЕ JSON: "
                    "{\"best\": \"...\"}"},
                {"role": "user", "content": json.dumps(
                    {"en": sources[idxs[0]], "variants": variants}, ensure_ascii=False)},
            ], max_tokens=400)
            best = json.loads(unfence(answer)).get("best")
        except (json.JSONDecodeError, urllib.error.URLError, KeyError, AttributeError):
            best = None
        if not best or not isinstance(best, str) or not best.strip():
            # без відповіді моделі беремо найкоротший варіант — субтитр читають бігцем
            best = min(variants, key=len)
        for i in idxs:
            translations[i] = best
        fixed += 1
    log("узгодження: зведено %d реплік" % fixed)
    return translations, fixed


def quality_report(sources, translations):
    latin = re.compile(r"[A-Za-z]")
    cyr = re.compile(r"[А-Яа-яЇїІіЄєҐґ]")
    suspicious, empty = [], []
    for i, (src, dst) in enumerate(zip(sources, translations)):
        if src.strip() and not dst.strip():
            empty.append(i)
            continue
        if not dst.strip():
            continue
        if len(latin.findall(dst)) > len(cyr.findall(dst)):
            suspicious.append(i)
    return empty, suspicious


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("srt")
    parser.add_argument("--out")
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--dry", type=int, default=0, help="перекласти лише N перших реплік")
    parser.add_argument("--repair", metavar="UK.SRT",
                        help="не перекладати наново, лише узгодити наявний переклад")
    opts = parser.parse_args()

    global MODEL
    MODEL = model_name()
    print("модель:", MODEL)

    cues = parse_srt(opts.srt)
    if opts.dry:
        cues = cues[:opts.dry]
    print("реплік:", len(cues))
    sources = ["\\n".join(c["text"]) for c in cues]

    if opts.repair:
        existing = parse_srt(opts.repair)
        if len(existing) != len(cues):
            sys.exit("ФАТАЛЬНО: у перекладі %d реплік, в оригіналі %d"
                     % (len(existing), len(cues)))
        translations = ["\\n".join(c["text"]) for c in existing]
        translations, _ = consistency_pass(sources, translations, print)
        opts.out = opts.out or opts.repair
    else:
        glossary = build_glossary(cues)
        print("глосарій:", json.dumps(glossary, ensure_ascii=False)[:300] or "(порожній)")

        began = time.time()
        translations, stats = translate_all(cues, glossary, opts.batch, print)
        print("переклад за %.0f с | повторів %d, поділів %d, порядково %d"
              % (time.time() - began, stats["retries"], stats["splits"], stats["single"]))
        translations, _ = consistency_pass(sources, translations, print)
    empty, suspicious = quality_report(sources, translations)
    print("перевірка: реплік на виході %d (на вході %d), порожніх %d, "
          "схоже неперекладених %d" % (len(translations), len(cues), len(empty), len(suspicious)))
    if len(translations) != len(cues):
        sys.exit("ФАТАЛЬНО: кількість реплік не збіглась — файл не записано")

    out = opts.out or re.sub(r"\.en\.srt$", ".uk.srt", opts.srt)
    if out == opts.srt:
        out = opts.srt + ".uk.srt"
    write_srt(out, cues, translations)
    print("записано:", out, "(%d Б)" % os.path.getsize(out))
    if suspicious:
        print("варті перегляду репліки:", suspicious[:20])


if __name__ == "__main__":
    main()
