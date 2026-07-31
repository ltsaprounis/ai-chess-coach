# Player profile -- testuser
*(Losses are in pawns per move -- 0.35 means the average move gave up about a third of a pawn; lower is better. Every figure below is move-weighted over the games covered.)*
Covering their games (all time controls): 19 games, 2026-03-27 to 2026-06-07 (all 19 games analyzed).

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
- Best win: beat a 1638 on 2026-06-07, rated 1598 at the time (+40)
- Streaks: their last game was a win; longest runs 2 wins, 2 losses
- By color: White 50% (13g); Black 58% (6g)
- Opposition: avg rating diff -2; 40% (5g) vs stronger, 62% (8g) vs similar, 50% (6g) vs weaker

## How games end
Losses 8: resigned 4, timeout 3, checkmated 1
Draws 2: agreed 1, repetition 1
Wins 9

## Repertoire

### As White
Systems the student chose:
- Queen's Pawn Game (1.d4 2.Bf4 3.e3) -- 6g, 58%
What they face as White:
- Englund Gambit Complex (1.d4 e5 2.dxe5 Nc6 3.Nf3 Qe7) -- 5g, 40%

### As Black
Systems the student chose:
- Pirc Defense (1...d6 2...Nf6 3...g6) -- 6g, 58%

## Recurring error patterns
| Pattern | Count | % of blunders |
|---|---|---|
| Back-rank vulnerability | 2 | 33.3% |
| Walked into a forced mate | 2 | 33.3% |

## Instructions
Write the player's narrative now, following these rules:
- **Length and shape.** Three to five sentences describing this student's tendencies, then a short list of weaknesses -- a handful of bullets, not an essay.
- **Audience.** You are briefing a chess coach about a student they are about to work with -- you are not talking to the student. Write about them in the third person, by name or as "this student"; never address the reader as "you". This text is stored and pasted into other prompts, where the reader is another coach: a narrative that opens "You are a rapid player" tells that coach they are the rapid player.
- **Scope.** The facts above cover one time control, named in the header. Say which one when you characterize the student, and never generalize the figures to their whole game -- a rapid profile is not a description of their bullet play.
- **Two denominators.** Ratings, records and repertoire counts cover every game in scope; average loss, blunder rates and error patterns cover only the analyzed subset, whose size the header states. Never present the analyzed sample as the student's whole history, and if coverage is thin, say the quality read is provisional.
- **Recent form first.** Where the recent-form windows disagree with the all-time figures, lead with the most recent window that has a real sample and say which way it is moving -- how the student plays now matters more than their average over years. Ignore a window whose analyzed count is too small to carry a conclusion.
- **Milestones are evidence, not decoration.** The rating peak with its date, the best win, the streaks, the after-a-loss score, the color split and how games end are all facts about every game in scope. Use the ones that say something -- sitting well below a peak reached long ago, a worse score in the game right after a loss, a lopsided White/Black split, or a large share of losses on the clock are each a coaching problem with a name. Ignore the ones that do not, and never read a split whose sample is a handful of games as a tendency.
- **Register.** Write for a club player's coach, not a fellow engine: pawns, never centipawns, and the idea before the number. Spell the unit out where the number is -- "1.30 pawns a move", never "1.30 ACPL" or any other acronym. Nothing here defines one, and what you write is stored and pasted into prompts that define nothing either, where a reader has no way to tell the figure is not centipawns.
- **Plain prose only.** Sentences and the bullet list, and no markdown headings (`#`, `##`) anywhere -- this text is pasted *inside* another prompt's sections, and a heading of your own would read there as starting a new one.
- **Evidence.** Every claim must tie to a figure stated above -- a rating, an average loss, a blunder rate, a repertoire score, an error-pattern count. Never assert a tendency the facts do not support.
- **No invented lines.** Do not assert a concrete variation, opening trap, or line of play beyond what the facts state -- these are aggregates, not annotated games, and there is no engine here to verify a claimed line.
- **No game citations.** Never reference a specific game, date, or opponent, and never write a link or handle of any kind -- this text is stored and reused inside other prompts, where a game reference could not be resolved into a link or checked.
- **Honesty.** If a section's sample is too thin to support a claim, say so or omit it rather than filling space.