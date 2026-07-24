# ADR-0005 — LF-021 offline fixture and replay foundation

- **Status:** accepted
- **Date:** 2026-07-23
- **Scope:** LF-021 implementation smoke/diagnostic artifacts only

## Context

LF-021 needs provider-call lineage, strict prompt parsing, raw-response
retention, and Lean validation before any live generator family is enabled.
The checked-in provider slots are intentionally unpinned and disabled. Private
`sft_classic` content is prohibited from every external-provider path.

## Decision

Authorize only the following implementation modes before the later Phase-5
provider-selection ADR:

1. a deterministic in-process fixture provider;
2. byte-for-byte replay of an immutable response produced by that fixture;
3. one hand-authored, public, trusted-NL smoke fixture used only to prove the
   pipeline end to end; and
4. fail-closed validation of the disabled production configuration.

These modes perform no network request. Every request binds the provider,
model, revision, prompt template, rendered prompt, decoding parameters,
problem identity, and retry index. Raw responses are written before parsing
and are immutable. Replay validates their canonical bytes and all bindings.

The smoke fixture and its descendants carry `artifact_class=smoke`; they are
ineligible for training, calibration, model selection, prevalence estimates,
and releases. No output receives a semantic label. A noncompiling or
malformed output remains a terminal operational failure and never becomes a
semantic negative.

## Non-decisions

This ADR does not:

- enable any external or local model provider;
- select a generator, proposer, validator, or judge family;
- authorize transmission of private-source content;
- close Gate 5G or Gate 5;
- establish a trusted research problem pool;
- authorize silver/gold promotion; or
- add localization or repair generation.

The later Phase-5 ADR must pin every enabled provider/model revision, public
source permissions, the source-by-provider matrix, prompt/parser versions,
retry policy, at least three collection families, and the four-family
confirmatory held-out design.

## Consequences

Implementation can be tested completely without waiting for credentials or
model availability. Live collection remains impossible under the canonical
checked-in configuration until a later ADR changes the config hash and passes
the corresponding preflight.
