# Player profile -- testuser
*(Losses are in pawns per move -- 0.35 means the average move gave up about a third of a pawn; lower is better. Every figure below is move-weighted over the games covered.)*
Covering their games (all time controls) at their current level: 19 games, 2026-03-27 to 2026-06-07 (all 19 games analyzed).

## Ratings
| Time class | Score | Rating | Peak |
|---|---|---|---|
| Blitz | 50% (11g) | 1455 → 1496 (range 1451-1496) | 1496 on 2026-06-04 |
| Rapid | 58% (6g) | 1560 → 1598 (range 1552-1598) | 1598 on 2026-06-07 |
| Bullet | 50% (2g) | 1468 → 1483 (range 1468-1483) | 1483 on 2026-06-01 |

## Recent form
*(Windows are nested and end at the most recent game, so each wider row contains the narrower ones.)*
| Window | Games | Score | Rating | Avg loss | Blunder % | Analyzed |
|---|---|---|---|---|---|---|
| last 30 days | 8 | 44% (8g) | 1598 | 1.34 | 3.4% | 8 |
| whole span | 19 | 53% (19g) | 1598 | 1.07 | 2.6% | 19 |

## Quality
| Phase | Moves | Avg loss | Blunder % |
|---|---|---|---|
| Opening | 176 | 1.30 | 1.7% |
| Middlegame | 54 | 0.32 | 5.6% |
| Endgame | 0 | n/a | n/a |
Overall 1.07 pawns lost per move over 230 moves -- best 58.3% (134), good 29.1% (67), inaccuracy 7.0% (16), mistake 3.0% (7), blunder 2.6% (6); 0.3 blunders/game.

## Trend
| Month | Games | Rating | Avg loss | Blunder % |
|---|---|---|---|---|
| 2026-03 | 3 | 1451 | 2.74 | 2.6% |
| 2026-04 | 7 | 1466 | 0.23 | 2.3% |
| 2026-05 | 6 | 1487 | 1.59 | 1.5% |
| 2026-06 | 3 | 1598 | 0.35 | 5.7% |

## Milestones and tendencies
*(All over every game in scope, analyzed or not -- none of these needs an engine.)*
- Biggest upset: beat someone 120 points higher, a 1607 on 2026-05-29 while rated 1487
- Streaks: their last game was a win; longest runs 2 wins, 2 losses
- Opposition: avg rating diff -2; 40% (5g) vs stronger, 62% (8g) vs similar, 50% (6g) vs weaker

## Splits
*(Each compares two groups of the student's own games. A split marked "within noise" is a difference this many games cannot distinguish from chance -- it is not a tendency and must not be reported as one.)*
- By color: 50% (13g) as White, against 58% (6g) as Black -- within noise, not a tendency
- Queen's Pawn Game as White: 58% (6g) in this system, against 43% (7g) their other games as White -- within noise, not a tendency

## How games end
Losses 8: resigned 4, timeout 3, checkmated 1
Draws 2: agreed 1, repetition 1
Wins 9

## Repertoire

### As White (11 of 13 games in these lines)
Systems the student chose:
- Queen's Pawn Game (1.d4 2.Bf4 3.e3) -- 6g, 58%, 0.14 pawns/move out of the opening
What they face as White:
- Englund Gambit Complex (1.d4 e5 2.dxe5 Nc6 3.Nf3 Qe7) -- 5g, 40%, 5.16 pawns/move out of the opening

### As Black (6 of 6 games in these lines)
Systems the student chose:
- Pirc Defense (1...d6 2...Nf6 3...g6) -- 6g, 58%, 0.15 pawns/move out of the opening

## Recurring error patterns
| Pattern | Count | % of blunders |
|---|---|---|
| Back-rank vulnerability | 2 | 33.3% |
| Walked into a forced mate | 2 | 33.3% |

## Instructions
Write a short profile of this student for the coach who works with them next. It gets pasted into other sessions as context when that coach explains a move or answers a question, so write what would actually change the advice.

**Dense, not polished. Around 200 words.** This is context another prompt pastes in, not an essay -- it is read for what it says, never for how it reads. Every sentence must carry a fact or a consequence the coach would act on. Cut transitions, cut any sentence whose only job is to introduce the next one, and never explain the significance of a figure you have just given: the reader is a coach and can see it. If a sentence could be deleted without losing information, delete it.

The facts above are everything you have -- there are no tools on this run, so write only what they support and say so where they run out.

Five rules, all because this text is stored and reused elsewhere:
- Third person, about the student, to a coach. Never address the reader as "you" -- they are the coach, not the player.
- No game citations, dates, opponents, links or handles. They resolve to nothing where this lands.
- No markdown headings (`#`, `##`). This text is pasted *inside* another prompt's sections, where a heading of your own reads as starting a new one.
- An observation from reading individual games is an example, never a tendency. Say how many games you looked at, and do not turn it into a trait -- collapsing in three sampled endings is three endings, not a temperament.
- Spell every unit out -- "1.30 pawns a move", never "1.30 ACPL" or any other acronym. Nothing here defines one, and neither do the prompts this lands in.

A comparison marked "within noise" is not a tendency. Do not name it as a weakness, do not call it "worth watching", and do not soften it into a passing mention -- the honest statement is that the data cannot tell, and the sentence is better spent on something it can.