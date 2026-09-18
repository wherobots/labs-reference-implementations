# Workflow Schematic — Wherobots Jobs from AWS Step Functions

Hand-off spec for rendering the process-flow diagram. Everything an image
generator (or designer) needs is in this file; no other context required.

## What the diagram depicts

An AWS Step Functions pipeline submits a Spark job to Wherobots Cloud and
pauses on a one-time task token. The job itself reports the result over HTTPS
(the callback pattern). Three outcome paths exist:

1. **Happy path (primary, visually dominant):** job posts a success callback →
   the paused state wakes → NextStep runs.
2. **Soft failure:** the job's `except` block posts a failure callback carrying
   the Python traceback → pipeline ends at JobFailed.
3. **Hard death (OOM/kill):** the job dies without sending anything; its
   60-second heartbeats stop; a 180-second heartbeat timeout fires → FindRun
   recovers the run ID from the job name → a reusable poller asks Wherobots
   for the true status → NextStep (if COMPLETED) or JobFailed (if FAILED).

Core message: the happy path has no polling, and the fallback machinery only
exists for deaths the job cannot report itself.

## Layout (two horizontal swimlanes, landscape ~16:9)

```
┌─ LANE 1: "YOUR AWS ACCOUNT · STEP FUNCTIONS + API GATEWAY" (top, ~60% height)
│   Row A (top):    [PreviousStep] → [SubmitAndAwaitCallback]        [NextStep]
│   Row B (middle):                    [API Gateway → relay λ]       [JobFailed]
│   Row C (bottom):                [FindRun] → [PollUntilComplete]
└─ LANE 2: "WHEROBOTS CLOUD" (bottom, ~25% height)
        [Wherobots job]   (sits under SubmitAndAwaitCallback)
```

NextStep and JobFailed are stacked at the far right (NextStep above JobFailed).
Keep a clear vertical corridor between PollUntilComplete and the right-side
boxes for return arrows.

## Nodes

| id | Label | Sublabel | Color role |
|----|-------|----------|------------|
| prev | PreviousStep | — | neutral gray |
| submit | SubmitAndAwaitCallback | waitForTaskToken · heartbeat 180s | primary teal, the hero box |
| next | NextStep | receives the callback output | success green |
| failed | JobFailed | traceback as cause | failure red |
| relay | API Gateway → relay λ | SendTaskSuccess / Failure / Heartbeat | neutral, teal-adjacent |
| findrun | FindRun | run_id by job name | fallback amber |
| poller | PollUntilComplete | reusable poller · every 30s | fallback amber |
| job | Wherobots job | Apache Sedona · managed Spark · try/except + heartbeat 60s | outlined, lives in lane 2 |

## Edges

| # | From → To | Style | Label |
|---|-----------|-------|-------|
| 1 | prev → submit | neutral, solid | — |
| 2 | submit → next | **teal, bold, straight across the top** | "success callback" |
| 3 | submit → job | neutral, solid, straight down | "submit run with the task token" |
| 4 | job → relay | teal, solid, up | "POST {token, result}" |
| 5 | relay → submit | teal, solid, short elbow up into submit's bottom | "wakes the paused state" |
| 6 | submit → failed | red, solid, routed under Row A across to the right | "failure callback (traceback as cause)" |
| 7 | submit → findrun | amber, DASHED, down-left then right | "heartbeats stopped (OOM) · HeartbeatSeconds fires" |
| 8 | findrun → poller | amber, dashed, short | — |
| 9 | poller → next | amber, dashed, up the right corridor | "COMPLETED" |
| 10 | poller → failed | red, dashed, up the right corridor | "FAILED" |

Arrowheads on every edge, at the destination end only.

## Hard constraints

- **No line may pass through or touch any text.** Labels sit in open space
  beside their edges, never straddling them.
- Where two edges must cross (edge 6 crosses edge 5's vertical; edge 9 crosses
  edge 6's horizontal), draw a small hop/bridge arc on the crossing edge — no
  ambiguous four-way junctions.
- Edges never pass through boxes. Route around, using the corridors between
  boxes.
- The teal happy path (edges 2, 4, 5) reads as the dominant visual flow.
  The amber fallback reads clearly secondary (dashed). Red is used only for
  the two failure edges and the JobFailed box.

## Legend (bottom strip)

- teal solid — callback path (happy)
- amber dashed — fallback (hard death)
- red — failure
- caption: "the single-use task token is the capability · no polling on the happy path"

## Palette A — editorial (the "typical" version)

Light warm-gray ground `#F4F6F7`, white lanes with dashed `#D6DEE1` borders,
ink `#101A1E`, muted `#6B7C84`. Teal `#0E5B69` (soft fill `#D7E8EA`), green
`#1C7A55` (`#CFE7DC`), amber `#A96A12` (`#F2E3C8`), red `#AC3128` (`#F3D8D5`).
Monospace labels (IBM Plex Mono), condensed sans title (IBM Plex Sans
Condensed). Thin boxes, 2px borders, barely-rounded corners. Title:
"Wherobots Jobs from AWS Step Functions". Feel: technical editorial report,
calm, precise.

## Palette B — 8-bit arcade (the retro version)

Night ground `#1A1C2C` with sparse 4px pixel stars. Panels: dark fills with
4px hard borders, zero corner radius: blue `#41A6F6`, green `#38B764`, orange
`#EF7D57`, red `#FF004D`, yellow `#FFCD75`, light `#94B0C2`, star `#F4F4F4`.
Thick 6px square-capped lines; arrowheads are solid pixel triangles; dashed =
brick-like dashes. All text uppercase pixel/mono font. Renames for flavor
(keep meaning): NextStep → "STAGE CLEAR", JobFailed → "GAME OVER", poller →
"POLL LOOP / REUSABLE BOSS", lanes → "WORLD 1: YOUR AWS ACCOUNT" / "WORLD 2:
WHEROBOTS CLOUD". Add three pixel hearts on the Wherobots job (its lives =
heartbeats). Title: "WHEROBOTS × STEP FUNCTIONS", subtitle "THE CALLBACK
QUEST · INSERT TOKEN TO CONTINUE". Legend caption ends "PRESS START".

## One-paragraph prompt (paste-ready, adapt palette per version)

> A clean landscape architecture diagram with two horizontal swimlanes:
> "YOUR AWS ACCOUNT · STEP FUNCTIONS + API GATEWAY" on top and "WHEROBOTS
> CLOUD" below. Top lane, upper row: box "PreviousStep" with an arrow into a
> prominent teal box "SubmitAndAwaitCallback (waitForTaskToken · heartbeat
> 180s)", and far right a green box "NextStep". A bold teal arrow labeled
> "success callback" runs from SubmitAndAwaitCallback straight to NextStep.
> Below the right side, a red box "JobFailed (traceback as cause)" receives a
> red arrow from SubmitAndAwaitCallback labeled "failure callback (traceback
> as cause)". Middle of the top lane: a box "API Gateway → relay λ
> (SendTaskSuccess / Failure / Heartbeat)". Lower row of the top lane: two
> amber boxes "FindRun (run_id by job name)" and "PollUntilComplete (reusable
> poller · every 30s)" connected left to right by a dashed amber arrow; a
> dashed amber arrow from SubmitAndAwaitCallback labeled "heartbeats stopped
> (OOM)" leads into FindRun, and dashed arrows leave PollUntilComplete up to
> NextStep (labeled COMPLETED) and to JobFailed (labeled FAILED). Bottom
> lane: an outlined box "Wherobots job (Apache Sedona · managed Spark ·
> try/except + heartbeat 60s)" receives a straight arrow down from
> SubmitAndAwaitCallback labeled "submit run with the task token", sends a
> teal arrow up labeled "POST {token, result}" into the relay box, and the
> relay sends a short teal arrow labeled "wakes the paused state" back into
> SubmitAndAwaitCallback. No line touches any text; crossings use small hop
> arcs; legend at the bottom: teal solid = callback path (happy), amber
> dashed = fallback (hard death), red = failure.
