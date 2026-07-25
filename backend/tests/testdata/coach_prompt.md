# Coaching brief -- testuser
*(ACPL = average centipawn loss per move, shown in pawns; lower is better. Every figure below is move-weighted across the games in scope.)*

## The student
- Window: 2026-03-27 to 2026-06-07
- Scope: all time controls
- Analyzed: 19 games
- Blitz: 11 games, rating 1455 → 1496 (range 1451-1496)
- Rapid: 6 games, rating 1560 → 1598 (range 1552-1598)
- Bullet: 2 games, rating 1468 → 1483 (range 1468-1483)
- Opposition: avg rating diff -2; 40% (5g) vs stronger, 62% (8g) vs similar, 50% (6g) vs weaker

## How the play breaks down
| Phase | Moves | ACPL | Blunder % |
|---|---|---|---|
| Opening | 176 | 1.30 | 1.7% |
| Middlegame | 54 | 0.32 | 5.6% |
| Endgame | 0 | n/a | n/a |
Overall 1.07 ACPL over 230 moves -- best 58.3% (134), good 29.1% (67), inaccuracy 7.0% (16), mistake 3.0% (7), blunder 2.6% (6); 0.3 blunders/game.

## Trend
| Month | Games | Rating | ACPL | Blunder % |
|---|---|---|---|---|
| 2026-03 | 3 | 1451 | 2.74 | 2.6% |
| 2026-04 | 7 | 1466 | 0.23 | 2.3% |
| 2026-05 | 6 | 1487 | 1.59 | 1.5% |
| 2026-06 | 3 | 1598 | 0.35 | 5.7% |

## How games end
Losses 8: resigned 4, timeout 3, checkmated 1
Draws 2: agreed 1, repetition 1
Wins 9

## Repertoire

### As White (13 games)
#### Systems you chose
| System (first moves) | Games | Score | Opening ACPL | Game ACPL |
|---|---|---|---|---|
| Queen's Pawn Game -- 1.d4 2.Bf4 3.e3 (1.d4 d5 2.Bf4 Nf6 3.e3 e6) | 6 | 58% | 0.14 | 0.20 |
#### What you face as White
| Opponent's line (your reply) | Games | Score | Opening ACPL | Game ACPL |
|---|---|---|---|---|
| Englund Gambit Complex (1.d4 e5 2.dxe5 Nc6 3.Nf3 Qe7) | 5 | 40% | 5.16 | 5.16 |
Long tail: 1 line under 5 games, 2 games total.

### As Black (6 games)
#### Systems you chose
| System (first moves) | Games | Score | Opening ACPL | Game ACPL |
|---|---|---|---|---|
| Pirc Defense -- 1...d6 2...Nf6 3...g6 (1.e4 d6 2.d4 Nf6 3.Nc3 g6) | 6 | 58% | 0.15 | 0.19 |
#### What you face as Black
No line yet reaches the 5-game sample floor.

## Recurring error patterns
| Pattern | Count | % of blunders | Example |
|---|---|---|---|
| Back-rank vulnerability | 2 | 33.3% | 2026-05-14, White's move 8 |
| Walked into a forced mate | 2 | 33.3% | 2026-05-14, White's move 8 |

## Turning points
### 1. 2026-06-04, blitz, as White, Queen's Pawn Game: Accelerated London System -- move 6
Leading up: Nf3 Bd6 Bg3 O-O
You played **6.Bd3** (lost about 1.1 pawns): -0.65 to -1.75. Engine preferred **Bxd6**.
### 2. 2026-06-01, bullet, as White, Ruy Lopez: Open Variation -- move 9
Leading up: Bb3 d5 dxe5 Be6
You played **9.c3** (lost about 3.1 pawns): -0.64 to -3.74. Engine preferred **Bxd5**.
### 3. 2026-05-25, rapid, as Black, Pirc Defense: Austrian Attack -- move 6
Leading up: Bg7 Nf3 O-O Bd3
You played **6...Na6** (lost about 1.1 pawns): +1.05 to +2.15. Engine preferred **Nxe4**.
### 4. 2026-05-14, blitz, as White, Englund Gambit Complex: Englund Gambit -- move 7
Leading up: Bd2 Qxb2 Bc3 Bb4
You played **7.Qd2** (lost about 1.3 pawns): -0.20 to -1.50. Engine preferred **Qxd7+**.
### 5. 2026-04-30, blitz, as White, Englund Gambit Complex: Englund Gambit -- move 6
Leading up: Bf4 Qb4+ Bd2 Qxb2
You played **6.Bc3** (lost about 1.1 pawns): -0.65 to -1.75. Engine preferred **a3**.
### 6. 2026-04-23, blitz, as White, Queen's Pawn Game: Accelerated London System -- move 6
Leading up: Nf3 Bd6 Bg3 O-O
You played **6.Bd3** (lost about 1.1 pawns): -0.65 to -1.75. Engine preferred **Bxd6**.
### 7. 2026-04-12, rapid, as Black, Pirc Defense: Classical Variation -- move 6
Leading up: Bg7 Be2 O-O O-O
You played **6...Bg4** (lost about 1.1 pawns): +1.05 to +2.15. Engine preferred **Nxe4**.
### 8. 2026-03-31, blitz, as White, Englund Gambit Complex: Englund Gambit -- move 7
Leading up: Bd2 Qxb2 Bc3 Bb4
You played **7.Qd2** (lost about 1.3 pawns): -0.20 to -1.50. Engine preferred **Qxd7+**.

## Instructions
Write the coaching brief now, following these rules:
- **Audience and register.** Write for a club player, not a fellow engine: pawns, never centipawns, and lead with the idea -- the threat, the plan, what a line wins -- before any number.
- **Attribution.** An opening is the student's own only where the repertoire lists it under their color in "Systems you chose". Never advise dropping a line from the "What you face" table -- recommend a response to it instead.
- **Citation.** Refer to positions and games by date and move number (e.g. "your 26...Nb6 in the June 14 blitz game"), never by list position or table row.
- **One biggest lever.** Open with the single change most likely to raise this student's results, not a flat list of co-equal weaknesses. Order everything else by impact behind it.
- **Honesty.** If the data does not support a conclusion -- too few games, no sample past the floor -- say so plainly instead of filling the section anyway.
- **Verification.** When the `analyze_position` tool is available, check any concrete line with it before asserting it -- never present an unverified variation as fact.
- **Plan.** Close with a two-week training plan sized to the time controls and volume shown above, not a generic study list.