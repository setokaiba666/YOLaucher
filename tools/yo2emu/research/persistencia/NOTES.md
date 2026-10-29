# Persistence in YO2 (client Ver.080912) — trunk, decks, player data, PBIN/NPCBIN

Static analysis of the client (Ghidra) plus recorded sessions. The client itself is not modified.

Confidence: **C** = CONFIRMED (decompiled and re-checked in a second pass: disassembly or a second reader, and size
checked against the protocol table extracted from the client), **P** = PROBABLE (strong deduction, not proven live),
**D** = DOUBTFUL.

Files:
- `proposal.py`: persistence functions and handlers (using `proto.W()`); self-test `python proposal.py` (sizes against
  the protocol table, field-by-field types, round trip).
- `duelpass.py`: DUELPASS codes (0x8101 → 0x8102) and starting amounts (§10).
- Ghidra script: `tools/yo2emu/ghidra/a5_jobs.py` (several jobs per pass: `at`, `xref` following thunks, `scalar`,
  `strings`, `range`, `disasm`, `callargs` = instructions before each call).

## 0. Summary

1. **The client uploads nothing persistent.** *All* calls to the "outgoing message id" function (`68a5f0`, 122 call
   sites, all with an immediate id except the generic duel one `FUN_00692b40`) were extracted: there is no message to
   save a deck, upload the trunk or upload PBIN/NPCBIN. **The server is the only source of truth** and must
   *replicate* what the client does locally (add cards, set bits) so that it sends the same data on the next login.
   **C**
2. **Decks are local only** (`profiles/<name>/deck.ydc`). The server only sees the deck when a duel starts
   (0x6600/0x6601/0x6603). **C**
3. **The CHAMPIONSHIP banner** appears because `reg+0x120` (offset 18 of PSELECTRESULT) ≠ 0: the login scene requests
   0x4915 and waits for flag 0x79, which *no* server message sets → it never closes. In addition, `reg+0x11c`
   (offset 14) with bits `0xc0000000` shows the notice "未取得のリカバリパックがあります" (uncollected Recovery Packs).
   **Both must always be 0.** **C**
4. The rest of PSELECTRESULT/PLAYERDATAUPDATE are profile statistics (profile window, texts 9000-9044) plus a few fields
   the client stores but never reads. Full map in §3.
5. PBIN = 1024 bits defined in `data/common/yo2pbinmap.dat`: avatar parts, titles, prize/deck rights ("GET権利" /
   "GET済み"), per-NPC prizes. NPCBIN = 4 bytes per NPC; byte 3 is the **progress level** that decides which NPC of each
   chain appears on the map.

## 1. Trunk / collection (kaban)

| What | Format | Conf. |
|---|---|---|
| Request | `0x3100 GETTRUNK`: u8 slot 0-3 (FUN_0068f2c0; out of range = not sent). Waits for flag +0x21 | C |
| Start | `0x3101` without data: falls into the `default` of the accounts dispatcher (FUN_0068eca0) = "TRUNKDATASTART": chunk index +0x554 = 0 and clears the temporary buffer +0x555 (0xcb8 B) | C |
| Data | `0x3102 TRUNKDATA`: **exactly 7**: chunks 0-5 of 512 B (at +0x555+i·0x200), chunk 6 of **0xb8 = 184 B** (at +0x1155) (FUN_0068fa90). An 8th chunk would write at +0x1355… and overwrite the player records (+0x124c): **never more than 7** | C |
| End | `0x3103 TRUNKDATAEND` without data: copies the 0xcb8 B to account+0x18ed (the real trunk), sets +0x21 = 2 and logs "Trunk Get! Total Count…" | C |
| Format | 3256 B, **byte i = copies of card id i+1** (u8). Valid ids 1..0xcb8 | C |

Evidence for the `id-1` index: six readers use it the same way (`(id-1)+0x18ed`): shop purchase FUN_006c1f30, DUELPASS
FUN_006bfcc0, SD_GET FUN_006c26d0, SELECTCARDPACK FUN_006bc0f0, PRESENT FUN_006c0070, ITEMGET FUN_006c2b30. All of
them: "if id < 0xcb9 and id ≠ 0 and count ≠ 0xff → count+1". **Per-card limit: 255 (saturating)**; the client does not
cap the trunk at 3. The 3-copies limit *in the deck* was not located (**D**, not needed on the server).

Messages that put cards into the trunk (the client adds them locally and then saves its `kaban.bin` cache with
FUN_00ad7d80; the server must add the same):

| Message (response) | Format | When it adds | Conf. |
|---|---|---|---|
| 0x5212 PURCHASE_SHOP_ITEM_RESULT | s32 + n×(u32 item, u8 type, u32 card, u8, 3×u8) | +1 per entry with type 0 or 1 | C |
| 0x4903 PURCHASE_PACK_OPEN_RESULT (DUELPASS) | s32, u32 (→net+0x540), n×(u16 card, u8) | +1 per card | C |
| 0x5401 SD_GET_RESULT | s32, n×u16 | +1 per card | C |
| 0x4803 SELECTCARDPACKRESULT | s32, n×(u16, u8) | +1 per card | C |
| 0x4905 PRESENT_PACK_OPEN_RESULT | s32, n×(u16, u8) | +1 per card | C |
| 0x5205 ITEMGET_RESULT | u16 n + n×u16 **PBIN bits to set**, u16 m + m×u16 cards | +1 per card; sets bits | C |
| 0x7126 TRADETRUNK (trade) | bytes; "Trade Trunk Get!" | — (out of scope) | D |

Local cache: `profiles/<name>/kaban.bin` (12288 B = 4096 × 3 B; byte 0 = copies, byte 1 = second array at
account+0x25a5, use unknown **D**). The client overwrites it; it is not the source of truth.

## 2. Decks

**There is no deck protocol with the server**: the editor saves locally. **C** (complete list of outgoing messages,
§0.1).

- `profiles/<name>/deck.ydc` (and `deckOffline*.ydc`, `cardpack/<id>.ydc`): **125 × u16 LE = main[80] (0-79) +
  side[15] (80-94) + extra[30] (95-124)**, 0 = empty. Evidence: deckOffline has 50 main (0-49), 2 side (80-81),
  4 extra (95-98); a recorded player deck had 42 main and 3 extra (10,10,10 at 95-97), and in the duel the client sent
  exactly 0x6600 = (42, 0, 3). **C**
- In-memory deck object (FUN_00692840): u32 main, u32 side, u32 extra, then the 125 u16 of the ydc (main at +0xc,
  extra at +0xca = +0xc + 95·2). **C**
- When the duel starts (FUN_00692840, unless spectating): **0x6600** = u32 main, u32 side, u32 extra; **0x6601** =
  u32 main + main×u16; **0x6603** = u32 extra + extra×u16. **The side deck (0x6602) is never sent.** **C**
- `0x4182` ("YG_MSG_SAVE_DECK" according to the log string) is a **server-to-client** message: u32 + 128 B
  (→net+0x74b) + 512 B (→net+0x54a); it is not a deck (sizes do not match) and no user of it was found. **D** Not
  needed.
- `recipe.bin` (local): pairs (u32 PBIN bit, u32 1) of redeemed decks (e.g. 0x302, 0x304, 0x306). The "recipes"
  (`cardpack/100xx.ydc`) are the "コンプ景品" prize of each NPC.
- `b0001.lst` / `f0001.lst`: local lists (probably ignore list / friends). **D**, unrelated to the server.

Server side: the last duel deck is stored (`decks.last_duel`) and a warning is produced if it uses cards the trunk does
not cover (informational only).

## 3. Player record, PSELECTRESULT and PLAYERDATAUPDATE

"Account" object = `*(DAT_012711ac + 0xc68)` (the same network object as the accounts server): +0x1210 result of
0x3111, +0x1248 **DPP**, +0x124c **4 player records of 0x1a8 B** (one per slot), +0x18ec selected slot, +0x18ed trunk
(0xcb8 B), +0x25a5 second per-card array, +0x3264 PBIN object (PBIN at +0x3268, NPCBIN at +0x32e9). Constructor:
FUN_0068e040. **C**

`FUN_006a5bc0(slot)` = `ECX + 0x30 + slot·0x1a8`, and in both handlers ECX = account+0x121c (disassembly 0x6b7f20 /
0x6bc28a) → **the "iVar2" of PSELECT is the same record** as that of PLAYERDATAUPDATE. **C**

Labels: the profile window (FUN_00a6e7a0 creates the labels with texts 9008, 9006, 9000, 9003, 9010, 9011, 9002, 9009,
9012, 9005, 9015, 9016, 9017…9044 in that order; FUN_00a72e40 draws the values in the same order: name,
title(+0x112), +0xa4, +0x124, +0xb2, +0xba, +0xa8, win %, collection %, +0xac, cards, types, +0xc4, +0xd0, +0xc8…).
1:1 pairing → **P** (high). Fields marked "no reads" are copied by the client but no reader was found (xref of the
getter, direct accesses account+0x124c+off and both handlers): their value does not matter → 0.

### Field-by-field maps (generated from the specifications in proposal.py; C = CONFIRMED, P = PROBABLE, D = DOUBTFUL)

#### PSELECTRESULT 0x4101 (FUN_006b7ef0) — 133 B

| Off | Type | Field (state.json) | Destination in the client | Use / meaning | Confidence |
|---|---|---|---|---|---|
| 0 | s32 | `result` | net+0x7e8 | 0 = OK; !=0 -> the rest is not read | C |
| 4 | u32 | `player_id` | reg+0xc | player id (CAvatarNetSupport); 0 = none | C |
| 8 | u32 | `dpp` | account+0x1248 | DUELPASS points | C |
| 12 | u16 | `unk_116` | reg+0x116 | no reads in the client | C destination / D use |
| 14 | u32 | `recovery_flags` | reg+0x11c | bits 0xc0000000 = "you have uncollected Recovery Packs" (MessageBox + 0x4901). Keep 0 | C |
| 18 | u32 | `champ_pending` | reg+0x120 | !=0 -> CHAMPIONSHIP prize banner + 0x4915. El motor de eventos solo lo pone con un claim real y el handler responde 0x4803, que libera la escena | C |
| 22 | u32 | `tp` | reg+0x124 | Tournament Points / TP (9003) | C destination / P label |
| 26 | u8 | `lv` | reg+0xa4 | LV / Level (text 9000/9001) | C destination / P label |
| 27 | u32 | `score` | reg+0xa8 | Score (9002); s32 in PLAYERLIST | C destination / P label |
| 31 | u32 | `mp` | reg+0xac | Miles = MP (9005): shop balance | C |
| 35 | u16 | `unk_b0` | reg+0xb0 | no reads in the client | C destination / D use |
| 37 | u16 | `wins` | reg+0xb2 | Wins (9010); win% = b2/(b2+ba) | C destination / P label |
| 39 | u16 | `unk_b4` | reg+0xb4 | no reads in the client | C destination / D use |
| 41 | u16 | `unk_b6` | reg+0xb6 | no reads in the client | C destination / D use |
| 43 | u16 | `unk_b8` | reg+0xb8 | no reads in the client (sent in PLAYERLIST) | C destination / D use |
| 45 | u16 | `losses` | reg+0xba | Losses (9011) | C destination / P label |
| 47 | u16 | `match_wins` | reg+0xbc | No. of matches won (9026) | C destination / P label |
| 49 | u16 | `match_losses` | reg+0xbe | No. of matches lost (9027) | C destination / P label |
| 51 | u16 | `single_wins` | reg+0xc0 | No. of "single" won (9028) | C destination / P label |
| 53 | u16 | `single_losses` | reg+0xc2 | No. of "single" lost (9029) | C destination / P label |
| 55 | u16 | `duels` | reg+0xc4 | Total no. of duels (9017); average LP = d8/c4 | C destination / P label |
| 57 | u16 | `duels_won` | reg+0xc8 | No. of duels won (9019) | C destination / P label |
| 59 | u16 | `duels_lost` | reg+0xca | No. of duels lost (9020) | C destination / P label |
| 61 | u16 | `duels_drawn` | reg+0xcc | No. of duels drawn (9021) | C destination / P label |
| 63 | u16 | `duels_void` | reg+0xce | No. of unfinished games (9022) | C destination / P label |
| 65 | u32 | `turns` | reg+0xd0 | Total no. of turns (9018) | C destination / P label |
| 69 | u16 | `surrender_me` | reg+0xd4 | times I surrendered (9023) | C destination / P label |
| 71 | u16 | `surrender_opp` | reg+0xd6 | times the opponent surrendered (9024) | C destination / P label |
| 73 | u32 | `lp_total` | reg+0xd8 | sum of final LP; average = d8/c4 (9025) | C destination / P label |
| 77 | u32 | `side_swaps` | reg+0xdc | cards swapped with the Side Deck (9030) | C destination / P label |
| 81 | u16 | `tribute1` | reg+0xe0 | summons with 1 tribute (9031) | C destination / P label |
| 83 | u16 | `tribute2` | reg+0xe2 | with 2 tributes (9032) | C destination / P label |
| 85 | u16 | `tribute3` | reg+0xe4 | with 3 tributes (9033) | C destination / P label |
| 87 | u16 | `fusions` | reg+0xe6 | fusion summons (9034) | C destination / P label |
| 89 | u16 | `ss_grave` | reg+0xe8 | special summons from the graveyard (9035) | C destination / P label |
| 91 | u16 | `ss_total` | reg+0xea | special summons (9036) | C destination / P label |
| 93 | u32 | `trap_me` | reg+0xec | traps activated by me (9037) | C destination / P label |
| 97 | u32 | `trap_opp` | reg+0xf0 | opponent's traps (9038) | C destination / P label |
| 101 | u32 | `spell_me` | reg+0xf4 | spells activated by me (9039) | C destination / P label |
| 105 | u32 | `spell_opp` | reg+0xf8 | opponent's spells (9040) | C destination / P label |
| 109 | u32 | `effect_me` | reg+0xfc | my monster effects (9041) | C destination / P label |
| 113 | u32 | `effect_opp` | reg+0x100 | opponent's effects (9042) | C destination / P label |
| 117 | u16 | `comeback_wins` | reg+0x104 | comeback wins (9043) | C destination / P label |
| 119 | u16 | `comeback_losses` | reg+0x106 | losses after a comeback (9044) | C destination / P label |
| 121 | u16 | `unk_108` | reg+0x108 | no reads in the client | C destination / D use |
| 123 | s32 | `unk_10c` | reg+0x10c | s32, no reads in the client | C destination / D use |
| 127 | u16 | `unk_110` | reg+0x110 | no reads in the client | C destination / D use |
| 129 | u16 | `title` | reg+0x112 | displayed title (称号): FUN_0068fc50(id) -> text; changed by 0x4174 | C |
| 131 | u16 | `unk_c6` | reg+0xc6 | no reads in the client | C destination / D use |

Total: **133 B**.

#### PLAYERDATAUPDATE 0x4140 (FUN_006bc270) — 125 B

| Off | Type | Field (state.json) | Destination in the client | Use / meaning | Confidence |
|---|---|---|---|---|---|
| 0 | u8 | `lv` | reg+0xa4 | LV / Level (text 9000/9001) | C destination / P label |
| 1 | u32 | `score` | reg+0xa8 | Score (9002); s32 in PLAYERLIST | C destination / P label |
| 5 | u32 | `mp` | reg+0xac | Miles = MP (9005): shop balance | C |
| 9 | u16 | `unk_b0` | reg+0xb0 | no reads in the client | C destination / D use |
| 11 | u16 | `wins` | reg+0xb2 | Wins (9010); win% = b2/(b2+ba) | C destination / P label |
| 13 | u16 | `unk_b4` | reg+0xb4 | no reads in the client | C destination / D use |
| 15 | u16 | `unk_b6` | reg+0xb6 | no reads in the client | C destination / D use |
| 17 | u16 | `unk_b8` | reg+0xb8 | no reads in the client (sent in PLAYERLIST) | C destination / D use |
| 19 | u32 | `dpp` | account+0x1248 | DUELPASS points | C |
| 23 | u16 | `losses` | reg+0xba | Losses (9011) | C destination / P label |
| 25 | u16 | `match_wins` | reg+0xbc | No. of matches won (9026) | C destination / P label |
| 27 | u16 | `match_losses` | reg+0xbe | No. of matches lost (9027) | C destination / P label |
| 29 | u16 | `single_wins` | reg+0xc0 | No. of "single" won (9028) | C destination / P label |
| 31 | u16 | `single_losses` | reg+0xc2 | No. of "single" lost (9029) | C destination / P label |
| 33 | u16 | `duels` | reg+0xc4 | Total no. of duels (9017); average LP = d8/c4 | C destination / P label |
| 35 | u16 | `duels_won` | reg+0xc8 | No. of duels won (9019) | C destination / P label |
| 37 | u16 | `duels_lost` | reg+0xca | No. of duels lost (9020) | C destination / P label |
| 39 | u16 | `duels_drawn` | reg+0xcc | No. of duels drawn (9021) | C destination / P label |
| 41 | u16 | `duels_void` | reg+0xce | No. of unfinished games (9022) | C destination / P label |
| 43 | u32 | `turns` | reg+0xd0 | Total no. of turns (9018) | C destination / P label |
| 47 | u16 | `surrender_me` | reg+0xd4 | times I surrendered (9023) | C destination / P label |
| 49 | u16 | `surrender_opp` | reg+0xd6 | times the opponent surrendered (9024) | C destination / P label |
| 51 | u32 | `lp_total` | reg+0xd8 | sum of final LP; average = d8/c4 (9025) | C destination / P label |
| 55 | u32 | `side_swaps` | reg+0xdc | cards swapped with the Side Deck (9030) | C destination / P label |
| 59 | u16 | `tribute1` | reg+0xe0 | summons with 1 tribute (9031) | C destination / P label |
| 61 | u16 | `tribute2` | reg+0xe2 | with 2 tributes (9032) | C destination / P label |
| 63 | u16 | `tribute3` | reg+0xe4 | with 3 tributes (9033) | C destination / P label |
| 65 | u16 | `fusions` | reg+0xe6 | fusion summons (9034) | C destination / P label |
| 67 | u16 | `ss_grave` | reg+0xe8 | special summons from the graveyard (9035) | C destination / P label |
| 69 | u16 | `ss_total` | reg+0xea | special summons (9036) | C destination / P label |
| 71 | u32 | `trap_me` | reg+0xec | traps activated by me (9037) | C destination / P label |
| 75 | u32 | `trap_opp` | reg+0xf0 | opponent's traps (9038) | C destination / P label |
| 79 | u32 | `spell_me` | reg+0xf4 | spells activated by me (9039) | C destination / P label |
| 83 | u32 | `spell_opp` | reg+0xf8 | opponent's spells (9040) | C destination / P label |
| 87 | u32 | `effect_me` | reg+0xfc | my monster effects (9041) | C destination / P label |
| 91 | u32 | `effect_opp` | reg+0x100 | opponent's effects (9042) | C destination / P label |
| 95 | u16 | `comeback_wins` | reg+0x104 | comeback wins (9043) | C destination / P label |
| 97 | u16 | `comeback_losses` | reg+0x106 | losses after a comeback (9044) | C destination / P label |
| 99 | u16 | `unk_108` | reg+0x108 | no reads in the client | C destination / D use |
| 101 | s32 | `unk_10c` | reg+0x10c | s32, no reads in the client | C destination / D use |
| 105 | u16 | `unk_110` | reg+0x110 | no reads in the client | C destination / D use |
| 107 | u16 | `title` | reg+0x112 | displayed title (称号): FUN_0068fc50(id) -> text; changed by 0x4174 | C |
| 109 | u16 | `unk_116` | reg+0x116 | no reads in the client | C destination / D use |
| 111 | u32 | `tp` | reg+0x124 | Tournament Points / TP (9003) | C destination / P label |
| 115 | u32 | `npc_index` | FUN_00ad6d80(idx,…) if !=0 | NPC id; 0 = do not touch NPCBIN | C |
| 119 | u8 | `npc_b0` | account+0x32e9+4·idx | NPCBIN[npc] byte 0 | C destination / P use |
| 120 | u8 | `npc_b1` | … +1 | byte 1 | C destination / P use |
| 121 | u8 | `npc_b2` | … +2 | byte 2 | C destination / P use |
| 122 | u8 | `npc_b3` | … +3 | byte 3 (progress level) | C |
| 123 | u16 | `unk_c6` | reg+0xc6 | no reads in the client | C destination / D use |

Total: **125 B**.

#### PLAYERLIST 0x3012 entry (FUN_0068f520) — 76 B (after an s32 0; max. 4)

| Off | Type | Field (state.json) | Destination in the client | Use / meaning | Confidence |
|---|---|---|---|---|---|
| 0 | u8 | `slot` | index (<4) | slot 0-3 | C |
| 1 | bytes16 | `name` | reg+0x10 | name | C |
| 17 | u8 | `lv` | reg+0xa4 | LV / Level (text 9000/9001) | C destination / P label |
| 18 | s32 | `score` | reg+0xa8 | Score (9002); s32 in PLAYERLIST | C destination / P label |
| 22 | u32 | `tp` | reg+0x124 | Tournament Points / TP (9003) | C destination / P label |
| 26 | u32 | `tp_rank` | reg+0x128 | position in the TP ranking (1-1000 = ranked); only in PLAYERLIST | C |
| 30 | u16 | `title` | reg+0x112 | displayed title (称号): FUN_0068fc50(id) -> text; changed by 0x4174 | C |
| 32 | u32 | `mp` | reg+0xac | Miles = MP (9005): shop balance | C |
| 36 | u16 | `unk_b0` | reg+0xb0 | no reads in the client | C destination / D use |
| 38 | u16 | `wins` | reg+0xb2 | Wins (9010); win% = b2/(b2+ba) | C destination / P label |
| 40 | u16 | `losses` | reg+0xba | Losses (9011) | C destination / P label |
| 42 | u16 | `unk_b8` | reg+0xb8 | no reads in the client (sent in PLAYERLIST) | C destination / D use |
| 44 | bytes16 | `avatar_a` | reg+0x134 (+4) | 16 B of parts | C destination / P (Domino/Academy) |
| 60 | bytes16 | `avatar_b` | reg+0x148 (+4) | 16 B of parts | C destination / P (Domino/Academy) |

Total: **76 B**.

Notes on the tables:
- PSELECTRESULT and PLAYERDATAUPDATE write the record of the **selected slot** (account+0x18ec). PSELECT also sets
  `reg+0x0c` (id), `+0x11c` and `+0x120`; PDU does not touch them but adds the NPCBIN update.
- `score` is read as u32 in PSELECT/PDU and as s32 in PLAYERLIST: same field.
- `mp` (Miles, +0xac) is the shop balance (CONFIRMED by the server's shop) and the DUELPASS adds 50 to it locally
  (see §4).
- `tp_rank` (+0x128) only arrives via PLAYERLIST; 1-1000 = the player is in the TP ranking (used by the login scene,
  case 0x13/0x16, together with PBIN bits 0x240/0x242 "TP100/TP500 GET権利"). Computed but OFF by default: see §11.
- PDU: `npc_index` is not range-checked by the client (FUN_00ad6d80): **only 1..127**.

## 4. PBIN and NPCBIN

Request: `0x3114 GETPBIN` (u8 slot; FUN_0068f340) on the accounts server (5731). Response **in this order**: `0x3114`
with 128 B (FUN_0068fb10 → FUN_00ad68a0 copies to pbin+4) and `0x4234 NPCBIN` with 512 B (FUN_0068fb90 → FUN_00ad68d0
copies to pbin+0x85 and **sets flag +0x22 = 2**, which is the one GETPBIN waits for). Without 0x4234 the login keeps
waiting. **C**

### PBIN (128 B = 1024 bits)
- Bit n = `byte[n >> 3] & (1 << (n & 7))` (FUN_00ad6a90 reads, FUN_00ad6900 sets/clears). **C**
- Meaning: `data/common/yo2pbinmap.dat` (2008 client data, Shift-JIS, one line per bit: `bit, bit%256, default, type,
  subtype, text_id, name, series`; loaded by FUN_00ad53d0, max. 0x400 rows). **C**
  - type 1/2 = avatar parts for boy/girl (subtype = part: 1 front hair, 2 face accessory, 3 nose, 4 eyes, 5 mouth,
    6 clothes, 7 back hair, 8 disk, 9 accessory); series 1 common, 2 Domino, 3 Academy. The "default" column = 1 on
    55 bits (boy 0-184, girl 256-440), the same ones the avatar editor sets in its temporary PBIN (FUN_008b07d0). →
    **Initial PBIN = those 55 bits** (**P**: with everything at 0 the wardrobe is empty, according to FUN_00ad5ab0,
    which only lists parts with their bit set).
  - type 10 = titles (称号 001-112, subtype 7600+; text 9100+), bits 157-255 and 409-511.
  - type 4 = special prizes (TP100/TP500 576-579, survey, finalist 582/583, champions 584-588).
  - type 5 = deck rights: "GET権利" (right) / "GET済み" (already collected) in pairs: 768/769 E-HERO EVOLUTION,
    **770/771, 772/773, 774/775 BeginnersDeck001-003**, 776-781 MiddleDeck, 782-791 Startup Present
    30/40/50/60/10, 792-809 decks 08-16.
  - types 6/8/9 = per-NPC prizes: for each NPCID_xxx, "途中景品 GET権利/GET済み" (intermediate prize) and
    "コンプ景品 GET権利/GET済み (レシピ: …)" (completion prize = recipe `cardpack/100xx.ydc`). NPC 1-50 at
    816+4·(n-1); 51-98 at 544-767. `proposal.npc_prize_bits()` extracts them from the file.
  - type 7 = "挑戦権" (right to challenge) Kaiba 592, Asuka 593, dark Marik 594.
- Rules the client applies (the server must mimic them):
  - **DUELPASS** (0x4903 with result 0, FUN_006bfcc0): DPP += 150, MP of slot 0 += 50 (writes account+0x12f8 =
    record 0 + 0xac), and for each BeginnersDeck with neither the right nor "済み" it sets the right
    (0x302/0x304/0x306). Same in DPAUTH/USBDPAUTH (FUN_006c01b0/FUN_006c1710). **C**
  - **Deck redemption** (login scene CCardPackCheckScene FUN_00850d90, case 0x10): sends `0x5400 SD_GET_REQUEST` =
    **u16 deck id + u16 right bit** (recorded: `1f 41 03 02` = deck 8001, bit 0x302), **clears the right bit** before
    sending and, if `0x5401` comes with result 0, **sets bit+1 (済み)**. **C** (disassembly 0x8523f2-0x852488)
  - **ITEMGET_RESULT** sets the bits it lists. **C**
  - The login scene also checks 0x240/0x242/0x246/0x310-0x316 (TP and Startup Present prizes) and title bits
    0xb2/0xc3/0xc4/0x1f3. With those bits at 0 it requests nothing. **C**

### NPCBIN (512 B = 128 × 4 B)
- Entry i = bytes `[4i .. 4i+3]` = b0 b1 b2 b3 (FUN_00ad6d10 reads it, FUN_00ad6d80 writes it). Index = NPC id (1-98).
  **C**
- **b3 = progress level with the NPC.** **C** for the use:
  - Map spawn (FUN_009ff2d0): an NPC with a "previous NPC" (`+0x40` of the NPCINFOLIST record) only appears if
    `NPCBIN[previous].b3 >= threshold of the previous (+0x34)`; and an NPC disappears when
    `NPCBIN[itself].b3 >= its threshold (+0x34)` and its successor exists. This is how NPC chains advance.
  - When talking to an NPC (FUN_0089f790): if `b3 == 0`, it sends `0x5204 ITEMGET` (type 4, id) and sets b3 = 1
    locally (FUN_006cf000); otherwise it goes to dialog 5.
- b0/b1/b2: statistics against that NPC, which the NPC window sums over the 3 levels of the famous characters (51-53,
  54-56… FUN_008c3c30): b0 → +0x454, b2 → +0x45c, b1 → +0x458 of the window. From texts 9046-9049 ("Friendship level",
  "Wins %d", "Losses %d", "Draws %d") the proposal is **b0 = wins, b1 = draws, b2 = losses** → **P** (the drawing code
  was not found).
- The server updates it with the NPC field of PLAYERDATAUPDATE (u32 id + 4 u8 in order b0..b3). **C**

## 5. `state.json` schema

`players` and `wallet` are kept as they were. `profiles` and `active_slot` are added; `proposal.profile()` creates the
profile on first use and **migrates `wallet`** to slot 0.

```json
{
 "players": {"0": "Player1"},
 "wallet": {"mp": 9800, "dpp": 1000},
 "active_slot": 0,
 "profiles": {
  "0": {
   "name": "Player1",
   "rec": {"lv": 1, "score": 0, "mp": 9800, "dpp": 1000, "tp": 0, "tp_rank": 0, "title": 0,
           "wins": 0, "losses": 0, "duels": 0, "duels_won": 0, "duels_lost": 0, "duels_drawn": 0, "duels_void": 0,
           "turns": 0, "lp_total": 0, "...": "remaining REC keys (proposal.py), all integers",
           "recovery_flags": 0, "champ_pending": 0, "unk_b0": 0, "unk_116": 0},
   "trunk": {"1": 3, "2": 3, "10": 3},
   "pbin": [0, 1, 2, 3, 4, 5, 32, 48, "... set bits (sorted list)", 771],
   "npcbin": {"51": [0, 0, 0, 1]},
   "avatar": {"a": "<32 hex: Domino parts>", "b": "<32 hex: Academy parts>"},
   "decks": {"last_duel": {"main": [1, 1, 1, 2], "extra": [10, 10, 10], "side_count": 0, "missing": {}}},
   "claims": [["sd", 8001, 770]]
  }
 }
}
```

- `trunk`: sparse, `id → copies` (1..255). Initial trunk (`YO2_TRUNK_START`): `deck` (the cards of the player's local
  `deck.ydc`, so their deck is never invalid), `all3` (3 copies of everything; the default, see
  `persistencia.TRUNK_START` in `yo2srv/BANDERAS.md`) or `empty`.
- `pbin`: list of set bits (readable, easy to edit by hand). Initial = the 55 default bits.
- `npcbin`: only NPCs that were touched. `avatar`: filled by 0x4170 (u32 style 0/1 + 16 B).
- `champ_pending` and `recovery_flags` are forced to 0 on serialisation whatever the JSON says.

## 6. Handler map

`H = proposal.make_handlers(load_state, save_state, shop_purchase, ...)` (wired in `yo2srv/arranque.py`):

| Message | Handler |
|---|---|
| 0x3010 (5731) | `H["h_playerlist"](c, data)` (PLAYERLIST with LV, MP, wins… from the profile) |
| 0x3100 (5731) | `H["h_gettrunk"](c, data)` (trunk of the profile; stores `active_slot`) |
| 0x3114 (5731) | `H["h_getpbin"](c, data)` |
| 0x3110 (5731) | `H["h_dp_saldo"](c, data)` (DUELPASS passes of the account, §12) |
| 0x4100 (lobby) | `H["h_pselect"](c, data)` (2nd byte = slot) |
| wallet / player data update | `H["wallet"]`, `H["set_wallet"]`, `H["player_data_update"]` (balance per profile) |
| 0x5210, after charging MP | `H["h_shop_bought"](c, cards)` **with the same list** that goes into 0x5212 (`shop_purchase` is random: compute it once) |
| 0x4902 | `H["h_pack_open"](c, data)` (adds cards, DPP+150, MP+50, Beginners rights; §12) |
| 0x8101 | `H["h_dpauth"](c, data)` (DUELPASS codes, §10) |
| 0x5400 | `H["h_sd_get"](c, data)` (uses the 2nd u16 = right bit) |
| 0x5204 | `H["h_itemget"](c, data)` |
| 0x4174 / 0x4170 | `H["h_title"](c, data)` / `H["h_avatar"](c, data)` |
| 0x6600/0x6601/0x6603 (duel port) | `H["h_duel_deck"](c, msg_id, data)` |
| end of a duel against an NPC | `c.send(0x4140, proposal.on_npc_duel_end(p, npc, result, ...))` + `save_state` |

Self-test: `python research/persistencia/proposal.py`.

## 7. Adversarial verification (second pass)

| Claim | How it was re-checked | Result |
|---|---|---|
| Order and types of PSELECTRESULT/PDU | Re-decompiled, disassembly of the entry (ECX = account+0x121c), `proposal.selftest()` compares type by type with the protocol table (49 and 50 fields) | C |
| Sizes 133/125/76/128/3256 | self-test against the protocol table | C |
| Trunk index = id-1, saturation 255 | 6 different handlers, same pattern | C |
| Exactly 7 chunks | FUN_0068fa90 + dispatcher (index +0x554, `== 6` → 0xb8) and a recorded session (6×512 + 184 accepted) | C |
| NPCBIN closes GETPBIN | FUN_0068fb90 sets +0x22 = 2; GETPBIN waits for 0x22 | C |
| CHAMPIONSHIP prize | case 0xb of FUN_00850d90 (+0x120) → case 0xc → 0x4915. El cliente no recibe otro mensaje que ponga +0x79=2; el servidor contesta 0x4803, la bandera que la escena sí espera | C |
| Recovery Packs notice | disassembly 0x850f07-0x850f24 + string at 0xcee214 | C |
| No outgoing deck/trunk/PBIN messages | `callargs` over all calls to 68a5f0: 122 call sites, immediate ids, closed list | C |
| ydc format main80/side15/extra30 | 4 files + deck object (extra at +0xca) + recorded 0x6600 (42,0,3) | C |
| SD_GET u16 deck + u16 bit | disassembly of case 0x10 of the login scene + recorded `1f 41 03 02` + recipe.bin | C |
| Statistics labels | label creation order (FUN_00a6e7a0) = drawing order (FUN_00a72e40) | P |
| Meaning of NPCBIN b0/b1/b2 | only the output order of FUN_008c3c30 + texts | P |
| "No reads" fields | xref of the getter (22 functions), direct accesses account+0x124c+off (false positives discarded: card ids of the duel engine), reader of 0x4160 | D (use) |

## 8. Suggested live checks

1. Login: `0x3102` ×7 with zeros except the cards of the initial trunk; `0x3012` with the profile's LV and MP. In the
   deck editor the trunk shows only those cards.
2. Shop: buy a 200 MP pack → `profiles.<slot>.trunk` grows by 5 and `rec.mp` drops by 200. **Quit the game and log in
   again**: the 5 cards are still in the trunk.
3. DUELPASS (0x4902): `rec.dpp` +150, `rec.mp` +50, `pbin` gains 770/772/774. On the next login the login scene must
   request 0x5400 ×3 (decks 8001-8003); afterwards `pbin` has 771/773/775 and they are **not** requested again.
4. Profile (statistics window): set `rec.wins = 7, rec.losses = 3, rec.lv = 5, rec.tp = 99` by hand in the state and
   log in again: 70 % wins, LV 5, TP 99 → confirms the P labels.
5. NPCBIN: set `npcbin."51" = [2,1,3,0]` and look at the window of NPC 51 (wins/losses/draws) → confirms b0/b1/b2. With
   a high `b3` on an NPC that has a successor, the successor must appear on the map.
6. Title: choose a title in the profile → `rec.title` changes and persists.

## 9. Open questions

- End of duel: no client result message was identified in this scope (0x6004 = 16 B, 0x6013 = u8, 0x6700/0x6702/0x6703
  without data; 0x6613 DUELSCORE 9×u32 is sent by the server). The server decides the outcome;
  `on_npc_duel_end()` takes the result.
- Per-NPC prizes (bits 途中/コンプ), Startup Present and TP: the format is known, the logic of when to grant them is
  not (in the original game the server decided it).
- Purchased avatar accessories (shop types 2-5): they probably set their PBIN bit.
- `0x3111 DUELPASSPURCHASERESULT` goes to account+0x1210: it is the number of unopened DUELPASS passes of the account
  (§12).

## 10. DUELPASS codes (0x8101 → 0x8102) and starting amounts

Module `duelpass.py` (registered as `sys.modules["yo2_duelpass"]` so all users share it). The server handles 0x8101
with `h_dpauth` (`YO2_DPAUTH=0` leaves it to the automatic replies).

| What | How the original works | Conf. |
|---|---|---|
| Request | "DUELPASS Points" with `account+0x1210` < 1 → code window (4055). Button: `FUN_00a86470` → `FUN_006ce5c0`: 0x8101 = the typed text in **16 B** (`FUN_0068aaf0(buf, text, 0x10)`) | C |
| Response | 0x8102 (`FUN_006c01b0`) = s32, u16 card, u8 (unused), u8 DPP, u16 deck; if the s32 ≠ 0 it reads nothing useful and adds 1 to `+0x544` | C |
| Window | 0xa85758: **only checks s32 == 0**. ≠ 0 → "Incorrect DUELPASS code" (4056, push at 0xa85af6). Text 4057 ("has already been used") exists in yo2text but **this exe does not use it** (no 0xfd9 anywhere in the code) | C |
| Lockout | `FUN_006ce5c0`: with `+0x544` > 10 it sends nothing and sets −2000 itself (11 failures per session) | C |
| Effects with 0 | card +1 if id ≤ 0xcb7; deck = pack from packinfo (`FUN_006c3610`): cards ×1 and bit `pack+0x68` + 1 ("済み"); 8999 special (×3, DPP = 1000, bit 0x246); DPP += u8; MP of **slot 0** += 10 if the u8 is 15/20/30; rights 0x302/0x304/0x306 if missing | C |
| `pack+0x68` | = 7th line of the block in `packinfo_s.txt` (7001→768, 8001→770, 8004→776…). 8001↔0x302 is visible in a recorded 0x5400 redemption | C (8001) / P (rest) |
| Value of each error | D (the client does not distinguish them). Here 1 = does not exist, 2 = already redeemed, 3 = revoked | D |
| Case | Compared in upper case (generated codes are A-Z/2-9, 12 characters) | P |

- Without `admin_duelpass.json` every code is incorrect (s32 1). (Answering 0x8102 with zeros would be a "success"
  with nothing, while the client would still grant itself the beginner rights locally without the server knowing.)
- Server = client: `duelpass.apply` replicates the effects (decks only those in packinfo with a right: 7001-7005,
  8001-8006; 8999 is not used). The code is marked "redeemed" (account, slot, character, date) before the items are
  given and goes back to "free" if saving fails. The log shows only the first 4 characters.
- Loose packs and arbitrary MP do **not** fit in 0x8102: a pack id used as "deck" would give its whole content and set
  bit 1 (`pack+0x68` = 0).
- **0x8101 arrives ENCRYPTED** (C, live): the 16 B of the code use Blowfish ECB with the same key as the 0x3003 login
  (`research/cuentas/cifrado.py`). `duelpass.norm` decrypts them if they are not text; plain text (`fake_client`) is
  still accepted.

Starting amounts: `duelpass.install_start(PP, dir)` wraps `blank_profile`/`seed_trunk`. With `admin_regalos.json`
(`{"mp", "dpp", "baul": all3|deck|empty}`) profiles created afterwards use those amounts; without the file, exactly
`START_MP`, `START_DPP` and `YO2_TRUNK_START`. In the original: 10 free DP at the start and 1 DP per duel (§13).

## 11. Shop: "New" marker, recipe of a purchased deck, no prize packs, tp_rank

| What | How | Flag | Conf. |
|---|---|---|---|
| 0x5212 "new" | u8 after the card = 1 only if the profile's trunk did not have it before the purchase; the 2nd copy in the same cart = 0 (like the lobby's 0x4803, `h_pack_open`'s 0x4903 and the client itself in 0x8102, `FUN_006c01b0`). Clothing: always 1. Without persistence: 1 | no flag | C (destination of the u8 and client rule) |
| Catalogue | `shop_catalog` skips the type-2 packs of `packinfo_s.txt` (9001 "TP3", 9002 "Startup Present"): prizes, not packs. The shop windows only filter by the ITEM type (`FUN_00af53f0`: type 1), so the filter must be server-side | `SHOP_SELL_PRIZE_PACKS` = False | C (data) |
| Recipe | buying a type-1 pack with a bit (7th line of packinfo: 7001→768, 8001→770…) sets `pbin` bit + 1 ("GET済み") in the profile. The editor only lists type 1/4 with that bit (`FUN_00983c80`); the client sets it by itself in SD_GET and 0x8102, but NOT on purchase. The client sees it on the next GETPBIN (0x3114: when re-entering with the character) | `SHOP_DECK_RECIPE` = True | C (client) / P (that the original did it on purchase) |
| Recipe notice | additionally 0x5205 with the bit (visible without re-entering, but shows "You have won a new item!") | `SHOP_DECK_ITEMGET` = False | D |
| tp_rank | position among ALL saved characters (root + `accounts`) by `rec.tp` descending, tie-break `rec.wins` descending, full tie = same position; TP 0 or > 1000 → 0 | `TP_RANK` (`YO2_TP_RANK=1`) = False | C (field) / P (order) / D (when) |

Why tp_rank is off. The client reads it in two places:
- `yo2TpWindow.cpp` (`FUN_009884e0`): with 1..1000 it draws 5289 "You are now Number %d in the TP Ranking!"; with 0 it
  draws no position (if PBIN 0x240, 5288 "You have passed the 100 TP Points mark!").
- Login scene (`FUN_00850d90` cases 0x13-0x16): with 1..1000 it opens the TP prize **on every login**: 0x8301
  REQTPPRESENTINFO → 0x8302 (card) → 0x8303 REQTPCARDGET → 0x8304; at the end it sets its copy to 0. With the lobby's
  TPPRESENT off, 0x8302 comes from the automatic replies with card 0. Whether the original always sent the position or
  only with a pending prize is unknown (D). Turn it on together with the TP prize: `PP.TP_RANK = True` or
  `YO2_TP_RANK=1`.

## 12. DUELPASS passes per account

In the original the DUELPASS was a pass that was bought: finite. Answering 0x3110 with a fixed balance, or re-arming
0x3111 = 1 after every 0x4903, would allow unlimited redemptions. Here a pass is an in-game item granted by the
operator.

| What | How the original works | Conf. |
|---|---|---|
| Request | 0x3110 = "GetDuelPassPurchase" (`FUN_0068f3c0`, string "Request_GetDuelPassPurchase"), through the **accounts** port in the login scene | C |
| Response | 0x3111 s32 → `account+0x1210`: object of the account (`DAT_012711ac+0xc68`), shared by the 4 slots → the balance belongs to the **account** | C |
| Click | "DUELPASS Points" (`FUN_00a67270`): `+0x1210` < 1 → code window (0x8101, §10); otherwise it sets it to 0 **locally** and opens the pack window (button → 0x4902) | C |
| 0x4903 ≠ 0 | `FUN_006bfcc0` reads the list the same way, but with s32 ≠ 0 it adds no DPP, MP or rights. The window (0xa88391: result 0x48 = `+0x900`) plays 0x10000003 and shows a box with "OK" (text 4002, `CWindowModelDuelpass150::m_okDlg`) instead of the pack | C (path) / D (exact text and error value: 1 is sent) |

Current behaviour (`research/persistencia/proposal.py`):
- `state["dp_pases"]` of the account (an account key: at the root of the state for the host account, in
  `accounts[key]` for the others; the SQLite storage keeps it as is). Missing key = 0. Max. `DP_PASS_MAX` (9999).
- 0x3110 → `PH["h_dp_saldo"]`: 0x3111 = balance. Without persistence the server answers 0.
- 0x4902 → `h_pack_open`: with balance ≥ 1, the usual pack (5 cards, DPP +150, MP +50 to slot 0, BeginnersDeck rights
  only if the player has neither the right nor "GET済み"), subtracts 1 and sends 0x3111 = new balance through the
  session's 5731 connection. With 0: 0x4903 = s32 1, u32 0, no cards, nothing changes, and 0x3111 = 0.
  `YO2_DUELPASS_REARM=0` removes the trailing 0x3111 (the client would see the balance on the next login). Without
  persistence the server answers s32 1.
- Passes are granted with an admin tool (not included); if the account is connected, 0x3111 is pushed immediately.
- Accounts start with 0 passes.

## 13. DP cost of a duel

DP = DUELPASS points (`account+0x1248`; the client treats it as the balance of the selected character: it arrives in
PSELECTRESULT 0x4101 and PLAYERDATAUPDATE 0x4140, field `dpp`, and is not in the character list 0x3012). Sources:
decompiled client, client texts, and period web pages found via a search engine (Yugipedia "Duel Pass", Konami Wiki
"Yu-Gi-Oh! Online").

### Rules

| Question | Answer | Source | Conf. |
|---|---|---|---|
| How much does a duel cost? | **1 DP per duel**, NPC and PvP | web: "it costs 1 DP for 1 duel"; OF-CPU guide (NPC: "costs 1 DP and gives 1 card") | C |
| And a match (best of 3)? | **Per duel played**: 2 DP if it ends 2-0, 3 if it goes to 3. To start a match the client requires 3 DP (in a single-duel room, 1) | client: `FUN_00863ec0`/`FUN_00863750`, 4 sites `+0x1248 < ((type != 3) ? 3 : 1)` (C); text 7180 "You need a **maximum** of x DUELPASS points" (tournament); recollection of former players (2 DP if you won 2-0) fits. No source states it outright | P |
| Do both pay in PvP? | Yes, each their own DP | web: "1 DP for 1 duel" (each player duels); the server also checked the opponent: text 4557 "Your opponent does not have enough DUELPASS points" (−614, `FUN_00884b70`) | P |
| On surrender, disconnection or draw? | Same cost | NPC: OF-CPU ("also if you surrender or disconnect", C). PvP and draw: same by analogy | C (NPC) / P (PvP) |
| Charged at the start or at the end? | Unknown. Here at the end, in the final 0x4140 (the same message that already carried the result) | — | D |
| Does the client subtract it? | **No.** The only writers of `+0x1248` are PSELECTRESULT (`FUN_006b7ef0`), 0x4140 (`FUN_006bc270`), DUELPASS 150 (+150) and 0x8102 (+u8). The server sets it with the `dpp` field of 0x4140 | decompiled client | C |
| Where does the player see it? | DUELPASS window: "DUELPASS Points" %d (text 4052, `FUN_00a84010`/`FUN_00a894c0`) | decompiled client | C |
| With 0 DP? | In **player rooms** the client stops by itself (create room, challenge a room; and when returning from a duel it deletes its room if it cannot afford it): state 0x4a with −612 = 4556 "You do not have enough DUELPASS points.". When **challenging an NPC it does NOT check** DP (`0x869410`: with dstyle 0, which is the NPC, it exits through `FUN_0089ad30` before checking; the NPC 0x4288 arrives with dstyle 0 in all recorded sessions) → the server must stop it. The −612 in 0x428a is drawn the same as its own (`FUN_0086bc60` state 0xd: `+0x94` = s32 of 0x428a → state 0x4a) | decompiled client + recorded sessions | C (client) / P (that Konami did it this way) |
| 10 free DP per account or per character? | The web says "given for free to every **account** when they are first created". But the client stores the balance per character (PSELECTRESULT rewrites it on selection) and DUELPASS 150 asks "Do you want to credit the points to **this Player**?" (3090/5046) | web (amount) / client | C (10) / D (account or character). Implemented per account: §14 |
| Cards per duel? | PvP (web, via search engine): single duel → **1 card** to each player from a pack of their choice; match (2 or 3 duels) → **3 cards** each; win or lose. NPC: 1 card (OF-CPU, implemented with `NPC_RULES_2008["card"]`). PvP: §14 | web (Konami Wiki/Yugipedia via search engine, page not directly readable) | P |
| Other versions? | DUEL ACCELERATOR (2010) changed the economy ("Duelist Premium" subscription). Nothing found about a different per-match cost | — | D |

### What the server does (on by default)

| Where | What | Turn off |
|---|---|---|
| `proposal.py` `START_DPP` | **New** character: 10 DP. `LEGACY_DPP` = 1000 only for a saved profile without `dpp`: **existing characters do not change** | `YO2_DP_INICIO=<n>` (`admin_regalos.json` still takes precedence if it exists) |
| `proposal.py` `dp_of`/`dp_need`/`dp_charge` | balance, threshold equal to the client's (1 single / 3 match), charge without going below 0 | `YO2_DP_COSTE=0` (or `PP.DUEL_DP_COST = 0` and `DFM.PS.DUEL_DP_COST = 0`) |
| NPC (`npcreglas`) | −1 DP at the end (win, loss, surrender, disconnection) in the final 0x4140; same cost as PvP | `NPC_RULES_2008["dp"] = False` |
| `dp_npc_gate` | 0x4288 to an NPC without DP → 0x428a s32 −612 and nothing else (no 0x43a0, no "dueling" for the others: runs before `pvp_hub`) | `YO2_DP_NPC_BLOQUEO=0` / `PP.DP_NPC_BLOCK = False` |
| `research/lobby` `_challenge` | 0x4300 to a room: challenger without DP → 0x4301 −612; host of the room without DP → −614 (threshold according to the room's dstyle) | `YO2_DP_SALA_CHECK=0` / `PP.DP_ROOM_CHECK = False` |
| `pvp_match` `PvpMatch._finish` | −1 DP to **each** player at the end (win, loss, draw, surrender, disconnection); sent in their 0x4140. No charge if the duel never started or if duelcore crashes (WHY_ABORT: our fault, D) | `YO2_DP_COSTE=0` |

`NPC_DP_COST` (`YO2_NPC_DP_COST`) stays at 0: do not turn it on (it would charge twice).

### Tests
- `python fake_client.py dpcoste`: new character 10 DP, an existing one keeps its balance, PvP with surrender −1 to both
  and their 0x4140 carries it, NPC with surrender −1 and the 0x4140 carries it.
- `python fake_client.py dpcero` (starts with `YO2_DP_INICIO=0`): NPC without DP → 0x428a −612 without 0x43a0 or
  "dueling"; challenging the room of someone without DP → −614; challenging without DP → −612.

### Open / undecided
1. **Charge at the start** instead of at the end (D). With "at the end" there is no loophole: disconnecting also costs.
2. Live check with the real client: a character with 0 DP talks to an NPC and presses "Duel" → "You do not have enough
   DUELPASS points." must appear and the player must still be able to walk. If it gets stuck:
   `PP.DP_NPC_BLOCK = False`.
3. Tournaments: text 7181 "You do not have enough DUELPASS Points to enter this Tournament" implies a check at
   registration (not implemented); tournament duels go through PvpMatch and already charge 1 DP to each player.

## 14. Card after a PvP duel and 10 DP once per ACCOUNT

### What is confirmed

| Question | Answer | Source | Conf. |
|---|---|---|---|
| How many cards does a PvP duel give? | Single duel: **1 to each player**, win or lose; match (2-3 duels): **3** | web (Konami Wiki/Yugipedia via search engine, §13) | P |
| Who chooses the pack and how? | The player. After "Next" in `CResultScene` (`FUN_008c4230` → scene 0x19, the same path for NPC and PvP; in GUNSLINGER rooms from `CDuelGunsStarTask`), `CCardPackCheckScene` (`FUN_008444c0`) opens `CWindowPackSelect` if the 0x4800 list (`lobby+0x1204`, 0x6c B per pack) is not empty: a carousel with **all** packs in the list (`FUN_00845750`/`FUN_00845810`: the selected one at x = 0x19c, the others at ±300 px). On selection (state 1 → 2) it sends **one** 0x4802 SELECTCARDPACK with the pack (`FUN_006ccdc0`), waits for 0x4803 (`+0x3a == 2`, state 3) and goes to scene 0x18 (`CCardGetResultScene`) with the cards it receives. One single choice per duel end | decompiled client | C |
| Has it been seen working? | Yes with an NPC: recorded 0x4800 `00 04` → 0x4802 `00 04` → 0x4803 with 1 card. With several packs in the list it has never been observed (0x4901 does not appear in any recorded session) | recorded session | C (1 pack) / P (several) |
| And with an empty list? | It continues without the picker; only in rooms of type 7/9/0xa/0xf/0x10/0x11/0x14 (GUNSLINGER, TRIAL, ANNIVERSARY, EVOLUTION, LAST BATTLE) or with `reg+0x11c & 0xc0000000` (Recovery Packs, §0.3) does it request 0x4910 REQGUNSLINGCARD → 0x4803. With packs in the list that path is not used | decompiled client | C |
| 0x4904/0x4905 PRESENT_PACK_OPEN | Sender `FUN_006ce570` has no callers: dead code (`research/lobby` `NO_APLICA`). Not involved | decompiled client | C |
| Which packs did Konami offer? | Unknown. Here 1-10 (the 0x4901 ones and those of the NPC card, `npcreglas.CARD_PACKS`) | — | D |
| What would the 3 match cards look like? | A single 0x4802 per duel end, so "3 cards" = one 0x4803 with 3 cards from the chosen pack (the client draws whatever arrives) | decompiled client (single choice) | P |
| 10 starting DP | "given for free to every **account** when they are first created"; the balance is still per character (+0x1248 of each record) | web (§13) / client | C (amount) / P (per account: design choice) |

### What the server does (on by default)

| Where | What | Turn off |
|---|---|---|
| `persistencia` `PVP_CARD_*`, `pvp_card_entry/offer/take/grant` | Pending card in the profile (`pvp_card_due`: `[{packs, n, t}]`, max. 5). `pvp_card_take(p, pack)` removes the most recent one that offered that pack and returns n (1 per duel; 3 prepared for the match) | `YO2_PVP_CARTA=0` (or `PP.PVP_CARD_ON = False` and `DFM.PS.PVP_CARD_ON = False`) |
| `pvp_match` `PvpMatch._finish` → `_card` | To **each** player at the end (win, loss, draw, surrender or disconnection), in the same save as the DP charge. Connected: stays pending and their 0x4800 carries the packs. Without a duel or lobby connection at the end (the one who dropped): n cards from a random pack **straight into the trunk**, without 0x4800. Nothing if the duel did not start (`_abort`) or if our engine crashes (`WHY_ABORT`), same as the DP | same |
| `_h_selectpack_pvp` + `pvp_card_install()` | In front of the usual 0x4802: with a pending PvP card for that pack, 0x4803 with n cards of the pack (`LP._cards`) stored in the trunk (`LP._cards_result`, with a real "New" marker). Otherwise the previous behaviour (NPC card from npcreglas or `h_selectpack`). Pending cards are delivered even if the feature is turned off later | — |
| `YO2_PVP_SOBRES=a,b-c` | Packs offered (default 1-10, max. 64) | — |
| `persistencia` `profile()` → `dp_start_once` | When the profile of a character is created: if the account has already received its starting DP (mark `dp_inicio`), has another profile or has `profiles_borrados`, it starts with **0**; otherwise with `START_DPP` (or the `admin_regalos.json` amount) and the account is marked. A profile created in a slot WITHOUT a character (no name: e.g. a 0x4140 before creating it) stays at 0 with `dp_inicio_pend` and is decided once it has a name. Existing characters do not change | `YO2_DP_INICIO_CUENTA=0` (or `PP.DP_START_ONCE = False` and `DFM.PS.DP_START_ONCE = False`): per character |
| account keys | + `dp_inicio`: the mark belongs to the ACCOUNT (root for the host account, `accounts[key]` for the rest, like `dp_pases`). `storage/` keeps it as is, like `dp_pases` | — |

Existing accounts: none has the mark, but every account that has (or had) characters counts as "already granted": its
next character starts with 0.

### Tests
- `python fake_client.py pvpcarta`: 1st character of an account 10 DP, the 2nd 0, delete and create again 0; PvP with
  surrender → both get 0x4140 and then 0x4800 with the 10 packs, 1 pending in each profile; each sends 0x4802 with a
  pack → 0x4803 with ONE card, trunk +1, nothing pending; a 2nd 0x4802 no longer gives the PvP card; PvP with a
  disconnection midway → the one who stays gets 0x4800 with packs, the one who dropped gets +1 card straight into the
  trunk and no 0x4800.
- `python fake_client.py pvpcarta0`: with `YO2_PVP_CARTA=0`, empty 0x4800 and nothing pending.
- Persistence self-test (`python research/persistencia/proposal.py`).

### Open / undecided
1. **Live check** of the picker with 10 packs (with an NPC it has only been seen with 1). If the carousel looks wrong:
   `PP.PVP_CARD_PACKS = (n,)` and `DFM.PS.PVP_CARD_PACKS = (n,)` at runtime, or `YO2_PVP_SOBRES=n` at startup.
2. **Which packs** to offer (D): currently 1-10. The shop ones (1-45)? Rotate them?
3. If the player closes the game with the picker open, the card stays pending (max. 5) and is not delivered by itself:
   the next 0x4802 with that pack takes it. Give it straight to the trunk on the next login? (not 1:1, D).
4. An NPC card and a PvP card pending at the same time with the same pack: the PvP one wins (both are 1 card).
5. Best-of-3 match: `pvp_card_offer(p, match=True)` (3 cards) exists; see `pvp.PVP_MATCH` in `yo2srv/BANDERAS.md`.
6. Unrelated, noticed while doing this: a 0x4802 without a pending card still gives 5 cards from the pack the client
   requests (`lobby.h_selectpack`, intended for 0x4901 Recovery Packs), and 0x4910 in GUNSLINGER rooms gives 5
   invented cards.
