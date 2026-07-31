# Coaching brief -- testuser
*(Losses are in pawns per move -- 0.35 means the average move gave up about a third of a pawn; lower is better. Every figure below is move-weighted across the games in scope.)*

## The student
- Requested: 2026-01-01 to 2026-07-27
- Window: 2026-03-27 to 2026-06-07
- Coverage: 19 of 30 games in scope are analyzed
- Note: the other 11 games in scope are not engine-analyzed. Ratings, records, milestones, how games end and the repertoire's game counts cover every game in scope; average loss, blunder rates, error patterns and turning points cover the analyzed ones only.
- Scope: all time controls
- Analyzed: 19 games
- Blitz: 11 games, rating 1455 → 1496 (range 1451-1496; peak 1496 on 2026-06-04)
- Rapid: 6 games, rating 1560 → 1598 (range 1552-1598; peak 1598 on 2026-06-07)
- Bullet: 2 games, rating 1468 → 1483 (range 1468-1483; peak 1483 on 2026-06-01)
- Opposition: avg rating diff -2; 40% (5g) vs stronger, 62% (8g) vs similar, 50% (6g) vs weaker

## Recent form
*(Windows are nested and end at the most recent game, so each wider row contains the narrower ones.)*
| Window | Games | Score | Rating | Avg loss | Blunder % | Analyzed |
|---|---|---|---|---|---|---|
| last 30 days | 8 | 44% (8g) | 1598 | 1.34 | 3.4% | 8 |
| whole span | 19 | 53% (19g) | 1598 | 1.07 | 2.6% | 19 |

## How the play breaks down
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

## Milestones
*(Over every game in scope, analyzed or not -- none of these needs an engine.)*
- Best win: beat hikaru (1638) on 2026-06-07, rated 1598 at the time (+40)
- Streaks: last game was a win; longest runs 2 wins, 2 losses
- By color: White 50% (13g); Black 58% (6g)

## How games end
Losses 8: resigned 4, timeout 3, checkmated 1
Draws 2: agreed 1, repetition 1
Wins 9

## Repertoire

### As White (13 games)
#### Systems the student chose
| System (first moves) | Games | Score | Opening avg loss | Game avg loss |
|---|---|---|---|---|
| Queen's Pawn Game -- 1.d4 2.Bf4 3.e3 (1.d4 d5 2.Bf4 Nf6 3.e3 e6) | 6 | 58% | 0.14 | 0.20 |
#### What they face as White
| Opponent's line (their reply) | Games | Score | Opening avg loss | Game avg loss |
|---|---|---|---|---|
| Englund Gambit Complex (1.d4 e5 2.dxe5 Nc6 3.Nf3 Qe7) | 5 | 40% | 5.16 | 5.16 |
Long tail: 1 line under 5 games, 2 games total.

### As Black (6 games)
#### Systems the student chose
| System (first moves) | Games | Score | Opening avg loss | Game avg loss |
|---|---|---|---|---|
| Pirc Defense -- 1...d6 2...Nf6 3...g6 (1.e4 d6 2.d4 Nf6 3.Nc3 g6) | 6 | 58% | 0.15 | 0.19 |
#### What they face as Black
No line yet reaches the 5-game sample floor.

## Recurring error patterns
| Pattern | Count | % of blunders | Example |
|---|---|---|---|
| Back-rank vulnerability | 2 | 33.3% | 2026-05-14 vs hikaru, White's move 8 (cite [g9]) |
| Walked into a forced mate | 2 | 33.3% | 2026-05-14 vs hikaru, White's move 8 (cite [g9]) |

## Turning points
### 1. 2026-06-04, blitz, as White vs hikaru, Queen's Pawn Game: Accelerated London System -- move 6 -- cite [g1]
Leading up: Nf3 Bd6 Bg3 O-O
FEN: `rnbq1rk1/ppp2ppp/3bpn2/3p4/3P4/4PNB1/PPP2PPP/RN1QKB1R w KQ - 4 6`
Played **6.Bd3** (lost about 1.1 pawns): -0.65 to -1.75. Engine preferred **Bxd6**.
### 2. 2026-06-01, bullet, as White vs hikaru, Ruy Lopez: Open Variation -- move 9 -- cite [g2]
Leading up: Bb3 d5 dxe5 Be6
FEN: `r2qkb1r/2p2ppp/p1n1b3/1p1pP3/4n3/1B3N2/PPP2PPP/RNBQ1RK1 w kq - 1 9`
Played **9.c3** (lost about 3.1 pawns): -0.64 to -3.74. Engine preferred **Bxd5**.
### 3. 2026-05-25, rapid, as Black vs hikaru, Pirc Defense: Austrian Attack -- move 6 -- cite [g3]
Leading up: Bg7 Nf3 O-O Bd3
FEN: `rnbq1rk1/ppp1ppbp/3p1np1/8/3PPP2/2NB1N2/PPP3PP/R1BQK2R b KQ - 4 6`
Played **6...Na6** (lost about 1.1 pawns): +1.05 to +2.15. Engine preferred **Nxe4**.
### 4. 2026-05-14, blitz, as White vs hikaru, Englund Gambit Complex: Englund Gambit -- move 7 -- cite [g4]
Leading up: Bd2 Qxb2 Bc3 Bb4
FEN: `r1b1k1nr/pppp1ppp/2n5/4P3/1b6/2B2N2/PqP1PPPP/RN1QKB1R w KQkq - 2 7`
Played **7.Qd2** (lost about 1.3 pawns): -0.20 to -1.50. Engine preferred **Qxd7+**.
### 5. 2026-04-30, blitz, as White vs hikaru, Englund Gambit Complex: Englund Gambit -- move 6 -- cite [g5]
Leading up: Bf4 Qb4+ Bd2 Qxb2
FEN: `r1b1kbnr/pppp1ppp/2n5/4P3/8/5N2/PqPBPPPP/RN1QKB1R w KQkq - 0 6`
Played **6.Bc3** (lost about 1.1 pawns): -0.65 to -1.75. Engine preferred **a3**.
### 6. 2026-04-23, blitz, as White vs hikaru, Queen's Pawn Game: Accelerated London System -- move 6 -- cite [g6]
Leading up: Nf3 Bd6 Bg3 O-O
FEN: `rnbq1rk1/ppp2ppp/3bpn2/3p4/3P4/4PNB1/PPP2PPP/RN1QKB1R w KQ - 4 6`
Played **6.Bd3** (lost about 1.1 pawns): -0.65 to -1.75. Engine preferred **Bxd6**.
### 7. 2026-04-12, rapid, as Black vs hikaru, Pirc Defense: Classical Variation -- move 6 -- cite [g7]
Leading up: Bg7 Be2 O-O O-O
FEN: `rnbq1rk1/ppp1ppbp/3p1np1/8/3PP3/2N2N2/PPP1BPPP/R1BQ1RK1 b - - 5 6`
Played **6...Bg4** (lost about 1.1 pawns): +1.05 to +2.15. Engine preferred **Nxe4**.
### 8. 2026-03-31, blitz, as White vs hikaru, Englund Gambit Complex: Englund Gambit -- move 7 -- cite [g8]
Leading up: Bd2 Qxb2 Bc3 Bb4
FEN: `r1b1k1nr/pppp1ppp/2n5/4P3/1b6/2B2N2/PqP1PPPP/RN1QKB1R w KQkq - 2 7`
Played **7.Qd2** (lost about 1.3 pawns): -0.20 to -1.50. Engine preferred **Qxd7+**.

## Instructions
Write the coaching brief now, following these rules:
- **Audience and register.** Write the brief *to* the student, in the second person ("you lose most of these on the clock"): the data above describes them in the third person, but they are the one reading what you write. Write for a club player, not a fellow engine: pawns, never centipawns, and lead with the idea -- the threat, the plan, what a line wins -- before any number.
- **Attribution.** An opening is the student's own only where the repertoire lists it under their color in "Systems the student chose". Never advise dropping a line from the "What they face" table -- recommend a response to it instead.
- **Citation.** Game first, move second: name the game by opponent and date at its first citation, then give the move in notation as the link, e.g. "In your game against marko77 on June 14, [26...Nb6][g3] ...", written through the entry's `cite` handle. Never a raw URL, never an invented handle, never a list position or table row. Later references to an already-cited game may shorten (e.g. "that marko77 game"). The opening name appears only as coaching content, never as the identifier; state the time class only when the report mixes time controls (the student section's scope line says which) and omit it otherwise.
- **One biggest lever.** Open with the single change most likely to raise this student's results, not a flat list of co-equal weaknesses. Order everything else by impact behind it.
- **Recent form first.** Where the recent-form windows disagree with the whole-span figures, lead with the most recent window that has a real analyzed sample and say which way it is moving -- how they play now matters more than their average over years. Ignore a window whose analyzed count is too small to carry a conclusion, and prefer these windows to a single month's row, which swings on one bad game.
- **Milestones are evidence.** The dated rating peak, the best win, the streaks, the score in the game right after a loss, the White/Black split and how games end are facts about every game in scope, not just the analyzed ones. Use the ones that say something -- sitting well below a peak reached long ago, a worse score right after a loss, a lopsided color split, a large share of losses on the clock each name a problem worth a paragraph -- and ignore the rest. Never read a split resting on a handful of games as a tendency: each carries its own game count, so check it first.
- **Honesty.** If the data does not support a conclusion -- too few games, no sample past the floor -- say so plainly instead of filling the section anyway.
- **Verification.** When the `analyze_position` tool is available: for each turning point the brief features, run the tool on that entry's FEN and state the refutation -- what the played move loses to, not just the better move's name -- and check any other concrete line before asserting it. Never present an unverified variation as fact.
- **Plan.** Close with a two-week training plan sized to the time controls and volume shown above, not a generic study list.