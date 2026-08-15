# Building a baseline that is wrong by construction

*The transferable part of this project. Nothing here is about drones.*

Most evaluations compare an expensive method against a baseline that is weak by accident. The
baseline loses, the expensive method is declared better, and nobody can say whether it earned its
cost or just beat a straw man. The usual fixes -- a stronger baseline, more test cases, a better
metric -- all leave the same hole, because the baseline's weakness is still incidental. It could
have gone the other way on a different sample.

This is the alternative. **Construct a case where the cheap method is wrong by CONSTRUCTION, not
by measurement.** Then "the expensive method beat the cheap one" stops being a sampling result and
becomes a structural one.

---

## The four steps

### 1. Write the free heuristic down as one sentence

Not a paragraph, not an implementation -- one sentence, in the form a skeptic would say it. If it
cannot be written in one sentence, it is not the cheap alternative your method has to beat; keep
looking for the one that can.

In this project:

> The first alarm after something changed is the cause.

That rule costs nothing, needs no model, and on most faults is simply right. Any system with a
running cost has to beat it to justify existing, and most published systems are never tested
against anything this cheap.

### 2. Find a STRUCTURAL asymmetry that makes it wrong

Not a hard case. Not an adversarial input. A property of the system that guarantees the heuristic
is wrong every time, that you can point to in source.

Here, two detectors disagree about latency for a principled reason. The compass detector waits for
the anomaly to persist (`MIN_ANOMALY_S = 1.0`) so it does not cry wolf. The navigation-filter
detector fires the moment its threshold is crossed. So when the compass breaks, the SYMPTOM is
advised at 8.8 s and the CAUSE at 9.8 s.

The careful engineering choice is exactly what puts the true cause last in the queue. That is the
asymmetry, and because it is a constant in the source rather than a property of one recording, it
holds on every run.

### 3. Verify the baseline scores zero, and that it cannot be tuned out

Two separate checks, and the second is the one people skip.

* The baseline scores 0.00. Here it does, in 10+ runs across two model families and every
  configuration tested, with no exceptions.
* The baseline **fails in the constructed way**. It is not enough that it scores badly; it must
  fail *because* of the asymmetry. Here, the baseline names the symptom as root on every bundle --
  so if that ever stops being true while the score stays 0.00, the scenario changed and the test
  is no longer measuring what it claims.

Both are pinned by tests, and both run on every commit. A construction argument that is not
enforced decays into a story about how things used to work.

### 4. Refuse the shortcut

**Never adjust the system to make the baseline fail.** Lowering a threshold, loosening a gate, or
tuning a parameter until the cheap method breaks does not produce a hard case -- it redefines the
task until you win. It is the single easiest way to get a publishable-looking result that means
nothing, and it is almost indistinguishable from honest work in the writeup.

The discipline that makes step 2 safe: **decide the structural property on its own merits, and
write down why, BEFORE checking whether it discriminates.** A persistence gate must be justifiable
purely as false-alarm suppression. If you add the gate and then check, you have tuned the
experiment into existence and cannot tell the difference afterwards.

In this project, three candidate faults are recorded as unreachable rather than tuned into
existence, with the measurements that ruled them out.

---

## What you get

A test where a positive result is informative *and a negative result is too*. If your expensive
method also scores near zero, you have learned something real -- the task is genuinely hard, not
that your baseline was weak.

You also get a cheap, permanent tripwire. The baseline is deterministic, so it should return
exactly the same number forever. When it does not, something about the task changed, and you find
out at that moment rather than at review.

## What you do NOT get

**Generality.** One construction gives you one mechanism. This project's own experience is the
cautionary case: the fault works robustly, and every conclusion drawn *through* it about model
behaviour turned out to be a property of a single model rather than of the method being tested.
Two frontier models rank the same judges in opposite orders on the same data.

A construction proves your baseline is beatable-in-principle on this mechanism. It says nothing
about the next mechanism, and a second construction is worth far more than more runs of the first
one.

## The reporting rules that go with it

These are not style preferences; each exists because breaking it produced a retraction here.

| Rule | Why |
|---|---|
| Name the model in every table | Two models ranked the judges in opposite orders on identical data. An unnamed finding may simply be false. |
| Never quote a single run | Measured spread is 0.11, one judgement in nine. Three of six retractions came from single runs. |
| Quote the interval at the INDEPENDENT unit | 3 items x 3 prompt variants is 9 judgements but 3 independent samples. Treating them as 9 inflates n threefold; this exact error was made and retracted. |
| Record what you could not produce | A fault that cannot be built is a finding. Deleting the attempt turns an honest limit into an invisible one. |
| Publish the verdict files, not just the numbers | A number nobody can regenerate is a claim. See the note below. |

That last one has teeth. On 2026-08-15 a schema migration silently broke the join between the
committed verdicts and their source data, and for a day not one published figure regenerated --
while the test suite stayed green, because nothing read those files. Publishing the artifacts is
necessary and not sufficient; something has to check that they still reproduce.

---

*Worked instance: [WHITEPAPER.md](../WHITEPAPER.md). Enforcement:
[tests/test_published_figures.py](../tests/test_published_figures.py).*
