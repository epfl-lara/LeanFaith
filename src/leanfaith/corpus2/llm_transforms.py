"""Track D-3: LLM transform generation with trusted self-labels (pilot harness).

Prompts an external LLM (codex / claude / lemex) to rewrite a headless Lean 4
statement with an explicitly chosen consistency-PRESERVING or consistency-BREAKING
transformation drawn from the TRANSFORM_CATALOG_V2 taxonomy, and trusts the model's
self-label (subject to later typecheck + stratified audits, per PLAN.md Track D-3).

New-regime reproducibility: no attestation — every pilot run emits a plain JSON
manifest (config, seed, git rev, input hashes, few-shot pair ids, per-provider stats).

GOLD RULE (enforced in code, see :func:`build_fewshots`): few-shot examples may come
ONLY from ``partition == "golden_train"`` pairs of the canonical golden pairs file.
Pairs from any other partition (dev / final_test / quarantine) are dropped at load
time and never rendered into a prompt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GOLDEN_PAIRS_PATH = Path("/storage/milikic/leanfaith/golden/canonical/golden_pairs_v1.jsonl")
MATHLIB_REPRESENTATIONS_PATH = Path("data/representations/mathlib.jsonl")
DEFAULT_OUTPUT_ROOT = Path("/storage/milikic/leanfaith/lf023_llm_transforms/pilot_v1")

FEWSHOT_PARTITION = "golden_train"
FEWSHOT_PROVENANCE = "expert_human"
DIRECTIONS = ("preserve", "break")
PROVIDERS = ("codex", "claude", "lemex")
PROVIDER_TIMEOUT_SECONDS = 240
MIN_STATEMENT_CHARS = 60
MAX_STATEMENT_CHARS = 600

_PRESERVE_MENU = (
    "currying/uncurrying of hypotheses (`A -> B -> C` <-> `A /\\ B -> C`)",
    "contrapositive (`A -> B` <-> `not B -> not A`)",
    "De Morgan rewrites and double-negation insertion/removal",
    "quantifier motion over independent binders (`A -> forall x, B x` <-> `forall x, A -> B x`)",
    "definitional fold/unfold of a transparent definition",
    "associativity/commutativity (AC) rewrites backed by standard lemmas",
    "extensionality expansion (funext / set ext) of an equality",
    "iff decomposition (`A <-> B` <-> `(A -> B) /\\ (B -> A)`)",
)

_BREAK_MENU = (
    "negate exactly one atom that influences the claim",
    "swap a logical connective (e.g. `/\\` <-> `\\/`, `->` <-> `<->`)",
    "drop or weaken a hypothesis that the claim actually depends on",
    "flip an inequality direction (`<` <-> `>`, `<=` <-> `>=`, or `<` <-> `<=` where it matters)",
    "perturb a numeric constant or exponent with real semantic effect",
    "swap quantifier scopes (`forall x, exists y` <-> `exists y, forall x`)",
)

_JSON_SCHEMA_LINE = (
    '{"rewritten_statement": str, "intended_label": "consistent"|"inconsistent", '
    '"transformation": str (short family name), "reasoning": str (<=60 words), '
    '"confidence": float 0-1}'
)

REQUIRED_OUTPUT_KEYS = (
    "rewritten_statement",
    "intended_label",
    "transformation",
    "reasoning",
    "confidence",
)


class GoldRuleViolation(RuntimeError):
    """Raised when a few-shot candidate is not a golden_train pair."""


@dataclass(frozen=True)
class FewShot:
    """One rendered few-shot calibration example (golden_train only)."""

    pair_id: str
    original: str
    rewritten: str
    verdict: str  # "consistent" | "inconsistent"

    def render(self) -> str:
        return f"ORIGINAL: {self.original}\nREWRITTEN: {self.rewritten}\nVERDICT: {self.verdict}"


@dataclass(frozen=True)
class SourceStatement:
    """One source statement sampled from the representations file."""

    statement_id: str
    content_hash: str
    headless: str


@dataclass
class ProviderCall:
    """Result of one external-LLM subprocess invocation."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    fallback_used: bool = False


@dataclass
class TransformRecord:
    """One pilot record: provider x source statement x direction."""

    provider: str
    index: int
    statement_id: str
    statement_hash: str
    source_statement: str
    direction: str
    prompt_sha256: str
    raw_stdout_path: str
    returncode: int | None
    timed_out: bool
    fallback_used: bool
    parse_ok: bool
    parse_error: str | None = None
    rewritten_statement: str | None = None
    intended_label: str | None = None
    transformation: str | None = None
    reasoning: str | None = None
    confidence: float | None = None
    label_matches_direction: bool | None = None

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------
# Few-shots (GOLD RULE enforced here)
# ---------------------------------------------------------------------------


def load_golden_train_pairs(pairs_path: Path) -> list[dict[str, Any]]:
    """Load ONLY partition=="golden_train" pairs from the canonical golden file.

    Every other partition (dev / final_test / quarantine) is discarded the moment
    its partition field is read; its statement text is never retained.
    """
    kept: list[dict[str, Any]] = []
    with pairs_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row: dict[str, Any] = json.loads(line)
            if row.get("partition") != FEWSHOT_PARTITION:
                continue  # never retain dev/final_test/quarantine content
            kept.append(row)
    for row in kept:
        if row.get("partition") != FEWSHOT_PARTITION:  # defense in depth
            raise GoldRuleViolation(
                f"non-{FEWSHOT_PARTITION} pair leaked into few-shot pool: {row.get('pair_id')}"
            )
    return kept


def build_fewshots(
    pairs_path: Path,
    seed: int,
    k_pos: int = 3,
    k_neg: int = 3,
) -> list[FewShot]:
    """Deterministically sample k_pos consistent + k_neg inconsistent few-shots.

    Eligible pairs: partition=="golden_train" AND label_conflict==False AND
    label_provenance=="expert_human". Sampling is seeded and order-independent
    (pools are sorted by pair_id before sampling).
    """
    pool = load_golden_train_pairs(pairs_path)
    eligible = [
        row
        for row in pool
        if row.get("label_conflict") is False and row.get("label_provenance") == FEWSHOT_PROVENANCE
    ]
    positives = sorted(
        (r for r in eligible if r.get("label") is True), key=lambda r: str(r["pair_id"])
    )
    negatives = sorted(
        (r for r in eligible if r.get("label") is False), key=lambda r: str(r["pair_id"])
    )
    if len(positives) < k_pos or len(negatives) < k_neg:
        raise ValueError(
            f"not enough eligible golden_train pairs: {len(positives)} positive "
            f"(need {k_pos}), {len(negatives)} negative (need {k_neg})"
        )
    rng = random.Random(seed)
    chosen = rng.sample(positives, k_pos) + rng.sample(negatives, k_neg)

    fewshots: list[FewShot] = []
    for row in chosen:
        if row.get("partition") != FEWSHOT_PARTITION:  # GOLD RULE assertion
            raise GoldRuleViolation(
                f"few-shot pair {row.get('pair_id')} has partition "
                f"{row.get('partition')!r}, expected {FEWSHOT_PARTITION!r}"
            )
        fewshots.append(
            FewShot(
                pair_id=str(row["pair_id"]),
                original=str(row["reference_headless"]),
                rewritten=str(row["candidate_headless"]),
                verdict="consistent" if row["label"] is True else "inconsistent",
            )
        )
    return fewshots


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_prompt(
    source_statement: str,
    direction: str,
    fewshots: Sequence[FewShot],
) -> str:
    """Build the full instruction prompt for one rewrite request."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")

    menu: tuple[str, ...]
    if direction == "preserve":
        goal = (
            "rewrite it so the mathematical claim is PROVABLY EQUIVALENT to the original "
            "(a consistency-PRESERVING transformation). The rewrite must state the same "
            "mathematical fact, only expressed differently."
        )
        menu_title = "Choose ONE transformation from this consistency-preserving menu:"
        menu = _PRESERVE_MENU
        label_instruction = 'Your intended_label MUST be "consistent".'
    else:
        goal = (
            "rewrite it so the mathematical claim is SUBTLY DIFFERENT from the original "
            "(a consistency-BREAKING transformation). The rewrite must NOT be equivalent "
            "to the original, but the change must be semantic and non-obvious — not a "
            "mere renaming or reformatting."
        )
        menu_title = "Choose ONE transformation from this consistency-breaking menu:"
        menu = _BREAK_MENU
        label_instruction = 'Your intended_label MUST be "inconsistent".'

    menu_lines = "\n".join(f"- {item}" for item in menu)
    fewshot_blocks = "\n\n".join(shot.render() for shot in fewshots)

    return f"""You are an expert in Lean 4 and mathlib.

TASK: You are given a headless Lean 4 theorem statement (binders and goal, no `theorem`
keyword, no name, no proof). Your job is to {goal}

{menu_title}
{menu_lines}

REQUIREMENTS:
- The rewritten statement must remain a well-typed, headless Lean 4 statement in
  mathlib style (same shape: binders followed by `:` and the goal). It must elaborate
  against `import Mathlib`.
- Keep it self-contained: do not invent constants that do not exist in mathlib.
- Apply exactly one transformation family from the menu (you may name a sub-variant).
- {label_instruction}

CALIBRATION EXAMPLES: The following are real, expert-judged statement pairs showing
what "consistent" (same mathematical claim) and "inconsistent" (different claim) mean.
They are judged examples for calibrating the verdict semantics — NOT demonstrations of
the transformations in the menu.

{fewshot_blocks}

INPUT STATEMENT:
{source_statement}

OUTPUT FORMAT: After any reasoning, the LAST line of your reply must be a single
strict JSON object, exactly this schema, no markdown fence, no trailing text:
{_JSON_SCHEMA_LINE}"""


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _iter_json_candidates(text: str) -> list[dict[str, Any]]:
    """Yield JSON objects decodable at any '{' position, scanning from the end."""
    decoder = json.JSONDecoder()
    # Strip markdown fences so `raw_decode` sees clean JSON tails as well.
    cleaned = text.replace("```json", "\n").replace("```", "\n")
    found: list[dict[str, Any]] = []
    for source in (text, cleaned):
        starts = [i for i, ch in enumerate(source) if ch == "{"]
        for start in reversed(starts):
            try:
                obj, _ = decoder.raw_decode(source[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                found.append(obj)
        if found:
            break
    return found


def parse_llm_output(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    """Extract the last valid transform-JSON object from provider stdout.

    Returns ``(parsed, None)`` on success or ``(None, error)`` on failure.
    Robust to markdown fences and trailing/leading prose; prefers the last
    JSON object that carries the required schema keys.
    """
    candidates = _iter_json_candidates(stdout)
    if not candidates:
        return None, "no JSON object found in stdout"

    schema_hits = [c for c in candidates if all(k in c for k in REQUIRED_OUTPUT_KEYS)]
    if not schema_hits:
        return None, (
            "JSON found but missing required keys; saw keys: "
            + ", ".join(sorted(candidates[0].keys()))
        )
    obj = schema_hits[0]  # candidates are ordered last-first

    if not isinstance(obj["rewritten_statement"], str) or not obj["rewritten_statement"].strip():
        return None, "rewritten_statement missing or not a non-empty string"
    if obj["intended_label"] not in ("consistent", "inconsistent"):
        return None, f"invalid intended_label: {obj['intended_label']!r}"
    if not isinstance(obj["transformation"], str):
        return None, "transformation is not a string"
    if not isinstance(obj["reasoning"], str):
        return None, "reasoning is not a string"
    conf = obj["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, int | float):
        return None, f"confidence is not a number: {conf!r}"
    conf_f = float(conf)
    if not 0.0 <= conf_f <= 1.0:
        return None, f"confidence out of [0,1]: {conf_f}"
    obj["confidence"] = conf_f
    return obj, None


def expected_label(direction: str) -> str:
    return "consistent" if direction == "preserve" else "inconsistent"


# ---------------------------------------------------------------------------
# Source statement sampling
# ---------------------------------------------------------------------------


def load_source_statements(reprs_path: Path) -> list[SourceStatement]:
    """Load headless statements in the [MIN, MAX] char range from a representations file."""
    out: list[SourceStatement] = []
    with reprs_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            headless = row.get("headless")
            if not isinstance(headless, str):
                continue
            if not MIN_STATEMENT_CHARS <= len(headless) <= MAX_STATEMENT_CHARS:
                continue
            out.append(
                SourceStatement(
                    statement_id=str(row.get("representation_id", "")),
                    content_hash=str(row.get("content_hash", "")),
                    headless=headless,
                )
            )
    return out


def sample_source_statements(
    reprs_path: Path,
    seed: int,
    n_per_provider: int,
    providers: Sequence[str] = PROVIDERS,
) -> dict[str, list[SourceStatement]]:
    """Deterministic seeded sample: DIFFERENT statements per provider, no overlap."""
    pool = sorted(load_source_statements(reprs_path), key=lambda s: s.statement_id)
    total = n_per_provider * len(providers)
    if len(pool) < total:
        raise ValueError(
            f"only {len(pool)} statements in range "
            f"[{MIN_STATEMENT_CHARS},{MAX_STATEMENT_CHARS}] chars, need {total}"
        )
    rng = random.Random(seed)
    picked = rng.sample(pool, total)
    return {
        provider: picked[i * n_per_provider : (i + 1) * n_per_provider]
        for i, provider in enumerate(providers)
    }


def direction_for_index(index: int) -> str:
    """Alternate preserve/break per index within a provider (even=preserve)."""
    return DIRECTIONS[index % 2]


# ---------------------------------------------------------------------------
# Provider invocation
# ---------------------------------------------------------------------------


def provider_command(provider: str, prompt: str, claude_model: str | None) -> list[str]:
    if provider == "codex":
        return [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c",
            'model_reasoning_effort="high"',
            "--skip-git-repo-check",
            prompt,
        ]
    if provider == "claude":
        cmd = ["claude", "-p", prompt]
        if claude_model is not None:
            cmd += ["--model", claude_model]
        return cmd
    if provider == "lemex":
        return [
            "lemex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c",
            'model_reasoning_effort="high"',
            "--skip-git-repo-check",
            prompt,
        ]
    raise ValueError(f"unknown provider: {provider!r}")


def run_provider(
    provider: str,
    prompt: str,
    timeout: int = PROVIDER_TIMEOUT_SECONDS,
    cwd: str = "/tmp",
    claude_model: str | None = "claude-opus-5",
) -> ProviderCall:
    """Invoke one provider CLI via subprocess; capture stdout. Never raises on failure.

    For claude, a bad --model id falls back to the CLI's default model once.
    """

    def _run(cmd: list[str]) -> ProviderCall:
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ProviderCall(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return ProviderCall(None, stdout, stderr, timed_out=True)

    call = _run(provider_command(provider, prompt, claude_model))
    if (
        provider == "claude"
        and claude_model is not None
        and not call.timed_out
        and call.returncode not in (0, None)
    ):
        fallback = _run(provider_command(provider, prompt, None))
        fallback.fallback_used = True
        return fallback
    return call


# ---------------------------------------------------------------------------
# Pilot run
# ---------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_rev(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except OSError:
        return "unknown"


def make_record(
    provider: str,
    index: int,
    statement: SourceStatement,
    direction: str,
    prompt: str,
    call: ProviderCall,
    raw_stdout_path: Path,
) -> TransformRecord:
    """Assemble one pilot record from a completed provider call."""
    record = TransformRecord(
        provider=provider,
        index=index,
        statement_id=statement.statement_id,
        statement_hash=statement.content_hash,
        source_statement=statement.headless,
        direction=direction,
        prompt_sha256=_sha256_text(prompt),
        raw_stdout_path=str(raw_stdout_path),
        returncode=call.returncode,
        timed_out=call.timed_out,
        fallback_used=call.fallback_used,
        parse_ok=False,
    )
    if call.timed_out:
        record.parse_error = f"provider timed out after {PROVIDER_TIMEOUT_SECONDS}s"
        return record
    parsed, error = parse_llm_output(call.stdout)
    if parsed is None:
        record.parse_error = error
        return record
    record.parse_ok = True
    record.rewritten_statement = str(parsed["rewritten_statement"])
    record.intended_label = str(parsed["intended_label"])
    record.transformation = str(parsed["transformation"])
    record.reasoning = str(parsed["reasoning"])
    record.confidence = float(parsed["confidence"])
    record.label_matches_direction = record.intended_label == expected_label(direction)
    return record


def provider_stats(records: Sequence[TransformRecord]) -> dict[str, Any]:
    """Aggregate per-provider pilot statistics."""
    parsed = [r for r in records if r.parse_ok]
    label_dist: dict[str, int] = {}
    for r in parsed:
        assert r.intended_label is not None
        label_dist[r.intended_label] = label_dist.get(r.intended_label, 0) + 1
    confidences = [r.confidence for r in parsed if r.confidence is not None]
    deltas = [
        len(r.rewritten_statement) - len(r.source_statement)
        for r in parsed
        if r.rewritten_statement is not None
    ]
    return {
        "n": len(records),
        "parse_ok": len(parsed),
        "timeouts": sum(1 for r in records if r.timed_out),
        "label_matches_direction": sum(1 for r in parsed if r.label_matches_direction),
        "intended_label_distribution": label_dist,
        "mean_confidence": round(float(statistics.mean(confidences)), 4) if confidences else None,
        "mean_rewritten_length_delta": (
            round(float(statistics.mean(deltas)), 1) if deltas else None
        ),
    }


@dataclass
class PilotConfig:
    """Configuration for one pilot run."""

    pairs_path: Path = GOLDEN_PAIRS_PATH
    reprs_path: Path = MATHLIB_REPRESENTATIONS_PATH
    output_root: Path = DEFAULT_OUTPUT_ROOT
    seed: int = 20260828
    n_per_provider: int = 10
    k_pos: int = 3
    k_neg: int = 3
    providers: tuple[str, ...] = PROVIDERS
    claude_model: str | None = "claude-opus-5"
    max_workers: int = 6
    timeout: int = PROVIDER_TIMEOUT_SECONDS
    repo_root: Path = field(default_factory=Path.cwd)


def run_pilot(config: PilotConfig) -> dict[str, Any]:
    """Execute the full pilot: sample, prompt, invoke providers, write outputs."""
    from concurrent.futures import ThreadPoolExecutor

    raw_dir = config.output_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    fewshots = build_fewshots(config.pairs_path, config.seed, config.k_pos, config.k_neg)
    samples = sample_source_statements(
        config.reprs_path, config.seed, config.n_per_provider, config.providers
    )

    jobs: list[tuple[str, int, SourceStatement, str, str]] = []
    for provider in config.providers:
        for index, statement in enumerate(samples[provider]):
            direction = direction_for_index(index)
            prompt = build_prompt(statement.headless, direction, fewshots)
            jobs.append((provider, index, statement, direction, prompt))

    def _execute(job: tuple[str, int, SourceStatement, str, str]) -> TransformRecord:
        provider, index, statement, direction, prompt = job
        call = run_provider(
            provider, prompt, timeout=config.timeout, claude_model=config.claude_model
        )
        stdout_path = raw_dir / f"{provider}_{index:02d}.stdout.txt"
        stdout_path.write_text(call.stdout, encoding="utf-8")
        stderr_path = raw_dir / f"{provider}_{index:02d}.stderr.txt"
        stderr_path.write_text(call.stderr, encoding="utf-8")
        return make_record(provider, index, statement, direction, prompt, call, stdout_path)

    with ThreadPoolExecutor(max_workers=config.max_workers) as pool:
        records = list(pool.map(_execute, jobs))
    records.sort(key=lambda r: (r.provider, r.index))

    records_path = config.output_root / "records.jsonl"
    with records_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record.to_json(), ensure_ascii=False) + "\n")

    manifest: dict[str, Any] = {
        "track": "D-3 LLM transforms pilot",
        "regime": "new (plain run manifest; no attestation)",
        "git_rev": _git_rev(config.repo_root),
        "seed": config.seed,
        "config": {
            "pairs_path": str(config.pairs_path),
            "pairs_sha256": _sha256_file(config.pairs_path),
            "reprs_path": str(config.reprs_path),
            "reprs_sha256": _sha256_file(config.reprs_path),
            "n_per_provider": config.n_per_provider,
            "k_pos": config.k_pos,
            "k_neg": config.k_neg,
            "providers": list(config.providers),
            "claude_model": config.claude_model,
            "timeout_seconds": config.timeout,
            "statement_char_range": [MIN_STATEMENT_CHARS, MAX_STATEMENT_CHARS],
            "direction_policy": "alternating preserve/break per index (even=preserve)",
        },
        "fewshot_pair_ids": [shot.pair_id for shot in fewshots],
        "fewshot_partition": FEWSHOT_PARTITION,
        "records_path": str(records_path),
        "per_provider_stats": {
            provider: provider_stats([r for r in records if r.provider == provider])
            for provider in config.providers
        },
    }
    manifest_path = config.output_root / "pilot_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs-path", type=Path, default=GOLDEN_PAIRS_PATH)
    parser.add_argument("--reprs-path", type=Path, default=MATHLIB_REPRESENTATIONS_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument("--n-per-provider", type=int, default=10)
    parser.add_argument("--providers", nargs="+", default=list(PROVIDERS))
    parser.add_argument("--claude-model", default="claude-opus-5")
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=PROVIDER_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    config = PilotConfig(
        pairs_path=args.pairs_path,
        reprs_path=args.reprs_path,
        output_root=args.output_root,
        seed=args.seed,
        n_per_provider=args.n_per_provider,
        providers=tuple(args.providers),
        claude_model=args.claude_model,
        max_workers=args.max_workers,
        timeout=args.timeout,
    )
    manifest = run_pilot(config)
    print(json.dumps(manifest["per_provider_stats"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
