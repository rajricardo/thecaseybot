"""
Offline evaluator: sends every line in trim_conditions.txt to Claude for an
independent TRIM/EXIT/ENTRY/NOISE judgment and diffs it against what
signal_classifier.classify() (the regex path used live today) already
produces. trim_conditions.txt was mined from real Casey messages, not
hand-labeled, so neither side is ground truth here — a disagreement just
means "worth a human look" before the regex gets tightened or an LLM
fallback goes anywhere near live orders.

Batches messages (--batch-size per API call) rather than one call per line
— at ~1800 lines that's ~70 calls instead of ~1800, and the model doesn't
need to see a message in isolation to judge it.

Usage:
    python llm_trim_evaluator.py --limit 50        # cheap sanity check first
    python llm_trim_evaluator.py                   # full trim_conditions.txt
    python llm_trim_evaluator.py --out report.csv   # keep every row, not just disagreements
"""

import argparse
import csv
import sys

import anthropic
import yaml

from signal_classifier import classify

DEFAULT_BATCH_SIZE = 25
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are grading raw Discord messages from a day-trading options-alert channel (0DTE SPY/QQQ/IWM-style calls/puts). One trader posts these live while a position is open. Classify each message independently, on its own, as exactly one of:

TRIM  - explicitly reducing part of an already-open options position (selling half/some/most/a fraction, "taking some off", "scaling out", "trimming").
EXIT  - fully closing the entire remaining position ("I'm out", "sold all/the rest", "stopped out", "closed").
ENTRY - opening a brand new position (buying a call/put, "taking QQQ 400c", "I got IWM 199c").
NOISE - anything else: chart/market commentary, a strike/level being watched (not bought), a recap/PnL summary, an aside, a typo-only fragment, etc. Not an actionable position-change statement.

Judge only the message text given — don't assume context from other messages in the batch. Be decisive: pick the single best label even for terse or informal phrasing ("Out half", "Trimming", "Sold most" are all real trade actions, not noise, even without a ticker or percentage attached)."""

RECORD_LABELS_TOOL = {
    "name": "record_labels",
    "description": "Record the classification for each message in the batch, in the same order given.",
    "input_schema": {
        "type": "object",
        "properties": {
            "labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "integer", "description": "0-based index matching the input order"},
                        "label": {"type": "string", "enum": ["TRIM", "EXIT", "ENTRY", "NOISE"]},
                        "note": {"type": "string", "description": "One short clause only if genuinely ambiguous, else empty string"},
                    },
                    "required": ["i", "label", "note"],
                },
            },
        },
        "required": ["labels"],
    },
}


def load_llm_config(config_path):
    with open(config_path) as f:
        config = yaml.safe_load(f)
    try:
        llm_cfg = config["llm"]
        return llm_cfg["api_key"], llm_cfg.get("model", DEFAULT_MODEL)
    except KeyError:
        sys.exit(f"{config_path} needs an `llm: api_key:` entry — see config.example.yaml")


def load_lines(path):
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def classify_batch(client, model, texts):
    numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(texts))
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[RECORD_LABELS_TOOL],
        tool_choice={"type": "tool", "name": "record_labels"},
        messages=[{"role": "user", "content": numbered}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            labels = {item["i"]: (item["label"], item.get("note", "")) for item in block.input["labels"]}
            return [labels.get(i, ("NOISE", "missing from model response")) for i in range(len(texts))]
    raise RuntimeError(f"no tool_use block in response: {resp.content}")


def run(path, config_path, limit, batch_size, model):
    api_key, configured_model = load_llm_config(config_path)
    model = model or configured_model
    client = anthropic.Anthropic(api_key=api_key)

    lines = load_lines(path)
    if limit:
        lines = lines[:limit]

    results = []
    for start in range(0, len(lines), batch_size):
        batch = lines[start:start + batch_size]
        llm_labels = classify_batch(client, model, batch)
        for text, (llm_label, note) in zip(batch, llm_labels):
            regex_label = classify(text).type.value
            results.append((text, regex_label, llm_label, note))
        print(f"...classified {min(start + batch_size, len(lines))}/{len(lines)}", file=sys.stderr)

    return results


def report(results, out_path):
    agreements = [r for r in results if r[1] == r[2]]
    disagreements = [r for r in results if r[1] != r[2]]

    print(f"\n{len(agreements)}/{len(results)} agree ({len(agreements) / len(results):.1%})\n")

    if disagreements:
        print(f"{len(disagreements)} disagreements (regex -> llm):\n")
        for text, regex_label, llm_label, note in disagreements:
            note_part = f"  ({note})" if note else ""
            print(f"  [{regex_label:>5} -> {llm_label:<5}]{note_part}  {text!r}")

    if out_path:
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["text", "regex_label", "llm_label", "agree", "llm_note"])
            for text, regex_label, llm_label, note in results:
                writer.writerow([text, regex_label, llm_label, regex_label == llm_label, note])
        print(f"\nfull results written to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default="trim_conditions.txt")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--limit", type=int, default=None, help="only classify the first N lines (cheap sanity check)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--model", default=None, help="overrides the model set in config.yaml's llm.model")
    parser.add_argument("--out", default=None, help="write every row (not just disagreements) to this CSV path")
    args = parser.parse_args()

    results = run(args.file, args.config, args.limit, args.batch_size, args.model)
    report(results, args.out)
