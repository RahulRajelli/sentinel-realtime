# The First Alarm Is Not the Fault

*Building a test that a cheap rule cannot pass, and what two AI models did with it.*

**All figures measured 2026-08-15.** Every accuracy score below regenerates from
[committed verdict files](https://github.com/RahulRajelli/sentinel-realtime/tree/main/results)
with `scripts/e4_report.py`. The two figures that do not come from those files are the test count,
which comes from `pytest`, and the battery example, which comes from one real flight log.

---

## Six warning lights

Your car's dashboard lights up at once: engine, temperature, oil pressure, battery.

Four warnings, all of them real. But they are not four problems. They are one problem and three
consequences. A mechanic who fixes the light that came on *first* replaces the wrong part and
sends you back out with the actual fault still there.

Noticing that something is wrong is easy. Working out **which** wrong thing caused the others
decides whether the repair works.

## The same problem, on a drone

I build a monitor that watches a drone while it flies and tells the operator what is failing.
The code is at
[github.com/RahulRajelli/sentinel-realtime](https://github.com/RahulRajelli/sentinel-realtime),
Apache-2.0. It ships 118 automated tests that need no simulator and no AI account, which you
can confirm with `pytest -q` after cloning.

A flight log is that dashboard with a few hundred lights and no dashboard. Sensors disagree,
motors run out of headroom and stop being able to correct, the navigation filter that combines
the sensors loses confidence, vibration climbs. Most of it is
downstream of one root cause, and the operator has seconds to act.

The easy half already works. Run `sentinel analyze` on a flight log and it reports settings that
contradict the hardware. On one 2024 log it flagged a low-battery warning set to 10.5 volts on a
22.2 volt pack, a threshold left over from a smaller battery. The alarm could never have fired,
so that aircraft flew with no working low-battery protection. **That log is private, so treat
this as an illustration rather than a claim you can check.** Every other figure below is
reproducible.

Ranking is the hard half, and it is what the rest of this measures.

## The one-second gap

To test ranking I needed a fault where the obvious answer is the wrong answer. So I broke a
drone's compass on purpose in simulation. A compass here is a magnetometer, a sensor that reads
the Earth's magnetic field, and I biased its output by a fixed amount so it pointed slightly
wrong. Then I watched what the monitor said.

```
   t = 8.0s          t = 8.8s                    t = 9.8s
      |                 |                           |
   compass          "navigation filter          "compass is
   broken            is inconsistent"            inconsistent"
      |                 |                           |
      +-----------------+---------------------------+
                        |<------- 1.0 second ------>|
                          THE SYMPTOM      THE CAUSE
                          arrives first    arrives second
```

The compass is what I broke. The compass alarm arrives **second**, one full second after the
alarm it caused.

That second is not luck and not tuning. The compass detector must see the anomaly persist for a
full second before calling it, which is a deliberate guard against false alarms written in the
source as `MIN_ANOMALY_S = 1.0`, in
[compass.py:45](https://github.com/RahulRajelli/ardupilot-log-analyzer/blob/main/src/flightdx/detectors/compass.py).
The detectors live in a sibling repository, `ardupilot-log-analyzer`, which the monitor imports.
The navigation filter has no such guard and fires the moment its threshold is crossed.

**The delay between the cause and its own alarm is a constant in the code.** It will happen every
time, on every flight, for as long as that line exists. Measured across three captured flights,
the gap was 1.0 s every time, with no false alarms before the fault was injected.

## Why the obvious rule fails

There is a free rule that needs no AI:

> **The first alarm after something changed is the cause.**

It costs nothing and on most faults it is simply right. When a motor
fails, the motor alarm fires first. When GPS drops out, the GPS alarm fires first. Any expensive
system has to beat this rule to justify itself, and usually it cannot.

On this fault the free rule scores **0.00**. Not sometimes. It scored zero in more than ten
separate runs, across two different AI models and every tool configuration tested, without a
single exception.

A test the cheap answer passes measures nothing, which is why this one exists.

## A test that can be failed

Four judges were measured, in increasing order of cost:

| judge | what it does |
|---|---|
| **B0** | the free rule above, no AI |
| **B1** | one AI call, no tools |
| **B2** | the same AI asked several times, answers majority-voted, allowed the same running cost as B3 |
| **B3** | an AI agent that can query the flight with tools |

Each flight is recorded once into a single file and fingerprinted so it cannot change unnoticed.
Every judge then runs against that file rather than against the aircraft, which is what makes the
arithmetic affordable.

**One number governs everything below: 3 flights.** Those are the three recordings of the compass
fault. Each is judged under 3 differently worded prompts, giving 9 judgements per run, and the
whole set is repeated 5 times per model. Every figure in the next section is a mean over those 5
repeats. The wider project holds 34 recorded flights and about 600 judgements; this comparison
uses 3 of them, and their verdict files are in
[results/](https://github.com/RahulRajelli/sentinel-realtime/tree/main/results).

Scoring is deliberately strict. Naming a *symptom* scores zero rather than partial credit,
because that confusion is the thing being measured. Every answer must cite evidence that exists
in the flight, so a correct answer pointing at something that never happened also scores zero.
The correct answer is hidden from the judges by construction, and a test asserts it never appears
in anything a judge can see.

## What the AI did

Each cell below is the mean of **five independent runs** over the same three flights, scored per
flight rather than per judgement:

| judge | Gemini-2.5-flash | GPT-5.6-sol |
|---|---|---|
| B0, free rule | 0.00 | 0.00 |
| B1, one call | **0.89** | 0.71 |
| B2, repeated sampling | 0.89 | not run |
| B3, tool agent | 0.67 | **0.96** |

Same code, same flights, same prompts. **On Gemini the agent loses to a single prompt. On GPT it
wins.** The ranking inverts.

Gemini's agent had a legible failure, and it is clearest in a separate experiment. When its tools
were allowed to report *when* each alarm fired, it treated that ordering as causation:

> *"The EKF inconsistency was detected first... this likely caused the compass inconsistency."*
> (EKF is the navigation filter.)

Exactly backwards. In that timestamped setup it made the mistake in 8 of its 9 judgements and
scored **0.11**. Removing the timing from its tools, changing nothing else, moved it to **0.67**
with the mistake appearing once rather than eight times. The 0.67 in the table above is that
improved setup, which is why the two numbers differ.

GPT scored **1.00** in the timestamped setup and never made the mistake once, in any
configuration tested.

So the tempting headline, *"AI agents get worse when you give them more tools"*, is not supported.
One model confused ordering with causation. A stronger model read the same ordering and drew the
correct conclusion.

**A caveat I would rather state than have you find.** This comparison rests on three flights of
one fault. An inverted ranking on that little data could be partly noise. What I can defend is
the mechanism, because the misreading is visible in the model's own words and it disappeared when
the ordering data was removed. What I cannot yet defend is the size of the gap.

## Five things I was wrong about

Over two days I formed five conclusions and withdrew all five.

| I claimed | What killed it |
|---|---|
| "The difference is statistically significant" | I counted 27 samples where there were 9 independent flights. Corrected, the ranges overlap |
| "Removing timestamps raised Gemini's agent to 0.89" | That was one run. Gemini's five-run mean is 0.69. The 0.96 in the table above is a different model, GPT, and a different experiment |
| "AI agents mistake ordering for causation" | True of Gemini, false of GPT |
| "The rest of the gap is a scoring bug in my code" | I fixed the bug. The gap did not move |
| "Remove timestamps from the tools" | Helps the weaker model, slightly hurts the stronger one |

Every one was caught by a check rather than by an argument: an outside model reviewing my work
against a written rubric, running things five times instead of once, testing a second model, and
building comparisons that could have come out either way.

A monitor that quietly reports the wrong root cause is worse
than one that crashes, because you act on it. The same applies to my own conclusions, which is
why those checks run whether or not I expect them to find anything.

## What survived, and what I do not know

**Survived:** the fault works. The free rule scores 0.00 on it, on every model and every
configuration tested. That is what this phase set out to build.

**Not established:** whether anything here about AI judgement holds beyond one fault and three
flights. Two frontier models already disagree about it.

More models would not fix this. A second fault of the same kind would, and building one would
most likely require a new detector rather than a new test, because only two detectors in the
current set carry the built-in delay that makes a cause arrive after its own symptom. That work
is not done.

Until that exists, every AI conclusion above rests on 3 flights, which is stated here rather than
left for a reader to discover.

---

*Reproduce any figure:*

```bash
git clone https://github.com/RahulRajelli/sentinel-realtime
cd sentinel-realtime

# one GPT run: prints the free rule at 0.00 and the tool agent's score
python scripts/e4_report.py --bundles bundles \
  --verdicts results/crossmodel/gpt2_run1.json --only compass_offset

# the timestamped setup, for the 0.11 above
python scripts/e4_report.py --bundles bundles \
  --verdicts results/isolation/iso_timed1.json --only compass_offset
```

Each table cell is the mean of five such files, `run1` through `run5`, in the same folders.
