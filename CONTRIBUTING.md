# Contributing

Ordinary contributions are welcome and need no ceremony: bug fixes, detector coverage, platform
support, docs, typos. Open a PR.

What follows is the short list of rules that are **not** style preferences. Each one exists
because breaking it produced a retracted claim in this project, and a PR that breaks one will be
asked to change regardless of how good the code is. They are here because they are invisible from
the diff.

---

## The seven

### 1. Never lower a threshold to make a scenario pass

This is the one that matters most. If a fault scenario does not fire, the fix is **not** to lower
the detector threshold until it does. That does not produce a hard test case, it redefines the
fault until you win — and afterwards nobody, including you, can tell the difference between that
and honest work.

Three faults in this repo are recorded as **unreachable**, with the measurements that ruled them
out, rather than tuned into existence. That is the expected outcome when a fault cannot be built.

The same applies to *adding* a persistence gate in order to create an ambiguous pair. Justify the
gate as false-alarm suppression, write down why, and commit that reasoning **before** checking
whether it discriminates. If you check first, you have tuned the experiment into existence.

### 2. Name the model in anything you publish

Every table must say which model produced it. Measured here: `gemini-2.5-flash` and `gpt-5.6-sol`
rank the same judges in **opposite orders** on identical data. A finding attributed to "an LLM
agent" is therefore not merely vague — it may be false.

### 3. Never quote a single run

Five repeats, report the mean and the spread. Measured spread is 0.11, one judgement in nine, and
three of this project's six retractions came from quoting a single run as though it were a mean.

`B0` is the sole exception, because it is deterministic code with no model in it and returns
exactly the same number every time.

### 4. Quote the interval at the independent unit

Statistics are per **bundle** (`ci_bundle`), never per judgement. 3 flights x 3 prompt variants is
9 judgements but only 3 independent flights; treating them as 9 inflates n threefold. That exact
error was made here and the significance claim built on it was retracted.

### 5. Never edit code while an experiment is running

A mid-run edit once crashed a sweep and left five runs straddling a scoring change. The whole set
was discarded. Finish the run, or start a new one.

### 6. Changing bundle identity is a three-part change

Changing `_identity_payload` or `_TIMING_FIELDS` must, **in the same commit**:

1. bump `SCHEMA_VERSION`,
2. add a migrator, and
3. widen `RunBundle.resolvable_identities()`.

Miss (1) and every existing bundle reports itself as tampered with. Miss (3) and every bundle
loads perfectly while all 60 committed verdict files silently stop resolving — `bundle_id` is a
foreign key, and migrating a table without migrating its references breaks the join. That happened
on 2026-08-15 and no published figure regenerated for a day while CI stayed green.

### 7. Console output stays ASCII

Windows `cp1252` renders anything else as `?`. Use `python -u` for backgrounded runs or the log
stays empty and looks hung.

---

## What CI enforces

```bash
pytest -q     # 218 passed, 2 skipped
```

Two things worth knowing about that number:

* **`tests/test_published_figures.py` is a reproducibility gate.** It re-scores every committed
  verdict file and asserts the published figures still regenerate, that `B0 = 0.00` in every arm,
  and that `B0` fails *in the constructed way* (naming a symptom on every bundle). It is ~30 s of
  the ~35 s runtime. If it fails, the repository's central claim — that its numbers can be checked
  — is currently false. That is a different kind of red from a unit test, and CI runs it as its
  own named step for that reason.
* If you change a published figure legitimately, update `PUBLISHED` in that file **in the same
  commit** as the write-ups. The coupling is deliberate: it should not be possible to quietly move
  a number on a page without a test objecting.

## Adding a detector

The bar is a detector whose silence is *distinguishable*. `coverage.py` reports "nobody was
listening" and `health.py` reports "the monitor fell behind", so a new detector should declare its
preconditions rather than simply returning nothing.

A detector carrying a **genuine persistence gate** is the single highest-value contribution
available, because it is the route to a second discriminating fault — see
[docs/METHOD.md](docs/METHOD.md). Read rule 1 before you build one.

## Scope

* **ArduPilot only.** PX4 is unsupported by design: different dialect, log format and parameter
  names. It would run and find nothing, which is worse than refusing.
* **No LLM anywhere near flight control.** Verified by test: no model client is imported in
  `runner.py`, `capture.py` or `gate.py`.
* **No RAG, no vector DB.** There is no corpus.

## Reporting a security issue

See [SECURITY.md](SECURITY.md).
