"""Generate and cache query rewrites for the planted questions.

    uv run python scripts/gen_rewrites.py --model anthropic/claude-haiku-4.5

Rewriting is the one lever the offline sweep left standing: handing BM25 the
answer's own text lifts recall from 0.459 to 0.714, so the gap is vocabulary,
not ranking. Generation is the only part that costs money, so it happens once
and lands in a JSON cache the harness reads for free on every later run.

Two strategies per question, because they fail differently:

  paraphrase  restate the question in the vocabulary a source document would
              use. Cheap, and safe — it cannot invent a false fact because it
              never asserts anything.
  hyde        write the passage that *would* answer it, and use that as the
              query. Closer to the oracle, and correspondingly riskier: an
              invented specific ('account 4100') is a term that matches the
              wrong chunks confidently.
"""

import argparse
import asyncio
import json
import pathlib

from cadence_backend.llm.client import llm
from cadence_backend.llm.json_parse import extract_json

ROOT = pathlib.Path(__file__).resolve().parent.parent
EGGS = ROOT / "data" / "corpus" / "easter_eggs.json"
CACHE = ROOT / "data" / "corpus" / "rewrites.json"

PROMPT = """You are preparing search queries for a keyword search engine (BM25) over a \
corpus of market-research documents about the US streaming industry: analyst notes, \
earnings-call transcripts, press releases, provider methodology docs, support articles, \
internal memos and trade press.

Keyword search only matches words that literally appear. The user's question and the \
document that answers it often share almost no words — a user asks why "our books and the \
provider don't agree" and the document says "billing divergence". Your job is to supply the \
words the document would actually use.

Question: {question}

Return JSON only:
{{
  "paraphrases": ["...", "...", "..."],
  "hyde": "..."
}}

paraphrases: three restatements using vocabulary a professional writing such a document \
would use. Vary the register — accounting, operational, trade press. Introduce domain terms \
the question does not contain. Do not merely reorder the question's own words.

hyde: one or two sentences written as if excerpted from the document that answers the \
question. Write it as an assertion in the document's voice. Do not invent specific figures, \
account numbers or dates that the question does not supply — a wrong specific is worse than \
no specific, because it matches confidently against the wrong passage."""


async def one(model: str, question: str, sem: asyncio.Semaphore) -> tuple[str, dict]:
    async with sem:
        try:
            r = await llm().chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": PROMPT.format(question=question)}],
                max_tokens=700,
            )
            data = extract_json(r.choices[0].message.content or "")
            return question, {
                "paraphrases": [str(p) for p in (data.get("paraphrases") or [])][:3],
                "hyde": str(data.get("hyde") or ""),
            }
        except Exception as exc:  # noqa: BLE001 — one bad question must not lose the batch
            print(f"  ! {question[:60]}: {type(exc).__name__}")
            return question, {"paraphrases": [], "hyde": ""}


async def main_async(model: str) -> None:
    questions = sorted({e["question"] for e in json.loads(EGGS.read_text())})
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    todo = [q for q in questions if model not in cache.get(q, {})]
    print(f"{len(questions)} questions · {len(todo)} to generate with {model}")
    if not todo:
        return

    sem = asyncio.Semaphore(8)
    done = await asyncio.gather(*(one(model, q, sem) for q in todo))
    for q, payload in done:
        cache.setdefault(q, {})[model] = payload
    CACHE.write_text(json.dumps(cache, indent=2))

    ok = sum(1 for _, p in done if p["paraphrases"] and p["hyde"])
    print(f"generated {ok}/{len(todo)} complete -> {CACHE}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="anthropic/claude-haiku-4.5")
    asyncio.run(main_async(p.parse_args().model))


if __name__ == "__main__":
    main()
