# 1EX MongoDB — Poora Table Structure (ex99_local)

> **Database:** `ex99_local`  
> **Connection:** `mongodb://localhost:27017`  
> **Total collections:** 35  
> **Project:** 1ex99 / centerpanel.1ex99.in local clone

---

## Quick Overview — Kaunsa Table Kya Rakhta Hai

| # | Collection | Hindi mein samjho | Docs (approx) |
|---|------------|-------------------|---------------|
| 1 | `users` | Saari login users — owner se client tak | 11 |
| 2 | `auth_sessions` | Login ke baad JWT token | 2 |
| 3 | `user_activities` | Login/logout/bet activity log | 16 |
| 4 | `domains` | Website settings (1ex99.in banner, theme) | 1 |
| 5 | `matches` | Sports matches + odds markets | 7 |
| 6 | `sports_catalog` | Sport list (Cricket, Football…) | 2 |
| 7 | `sports_bets` | User ki sports bets | 2 |
| 8 | `positions` | Market mein user ka exposure/position | 1 |
| 9 | `casino_games` | Casino games list (Teenpatti, etc.) | 35 |
| 10 | `casino_bets` | Casino par lagayi gayi bets | 2 |
| 11 | `casino_rounds` | Har round ka result | 1 |
| 12 | `day_wise_casino` | Din-wise casino P/L report | 2 |
| 13 | `matka_events` | Matka draw events | 2 |
| 14 | `matka_bets` | Matka par lagayi bets | 1 |
| 15 | `ledger_entries` | Credit/debit ledger rows | 2 |
| 16 | `user_ledger` | User ka full ledger summary | 1 |
| 17 | `statements` | Account statement (date range) | 1 |
| 18 | `profit_loss` | User profit/loss summary | 1 |
| 19 | `reports` | Cached report data | 4 |
| 20 | `bpexch_accounts` | Bpexch balance + statement | 1 |
| 21 | `decision_logs` | Match result declare logs | 1 |
| 22 | `center_projects` | Center panel projects | 1 |
| 23 | `center_domain_ips` | Domain → IP mapping | 1 |
| 24 | `center_events` | Sport types + market count | 4 |
| 25 | `center_series` | Series list (IPL, etc.) | 2 |
| 26 | `center_custom_series` | Custom series create | 1 |
| 27 | `center_racing_events` | Horse/greyhound racing | 1 |
| 28 | `center_manual_fancy` | Manual fancy sessions | 1 |
| 29 | `center_manual_bookmaker` | Manual bookmaker odds | 1 |
| 30 | `center_squad_templates` | Team squad templates | 1 |
| 31 | `center_betfair_results` | Betfair result declare | 1 |
| 32 | `center_manual_scores` | Manual live score | 1 |
| 33 | `center_fancy_categories` | Fancy category list | 2 |
| 34 | `center_fancy_audit` | Fancy change audit log | 1 |
| 35 | `center_master_settings` | Global settings (bet delay, etc.) | 1 |

---

## User Hierarchy (users table)

```
owner (9)
 └── subowner (8)
      └── superadmin (7)
           └── admin (6)
                └── subadmin (5)
                     └── master (4)
                          └── superagent (3)
                               └── agent (2)
                                    └── client (1)
```

Har user ka `parentId` uske upar wale user ki `userId` hoti hai.

---

## 1. USERS & AUTH

### `users` — Main user table

| Field | Type | Description |
|-------|------|-------------|
| `_id` | ObjectId | MongoDB auto ID |
| `userId` | string | Unique user ID (e.g. uid-owner) |
| `username` | string | Login username (unique) |
| `password` | string | Plain/hashed password |
| `name` | string | Display name |
| `mobile` | string | Phone number |
| `userType` | enum | owner \| subowner \| superadmin \| admin \| subadmin \| master \| superagent \| agent \| client |
| `userPriority` | number | 1–9 (9 = owner, 1 = client) |
| `parentId` | string \| null | Upar wale user ki userId |
| `creatorId` | string | Jisne create kiya |
| `coins` | number | Current balance/chips |
| `creditLimit` | number | Max credit limit |
| `exposure` | number | Open bet exposure |
| `profitLoss` | number | Total P/L |
| `casinoStatus` | boolean | Casino allowed? |
| `matkaStatus` | boolean | Matka allowed? |
| `betStatus` | boolean | Sports bet allowed? |
| `matchStatus` | boolean | Match bet allowed? |
| `intCasinoStatus` | boolean | International casino |
| `matchShare` | number | Match commission % |
| `casinoShare` | number | Casino commission % |
| `matchCommission` | number | Match comm rate |
| `sessionCommission` | number | Session/fancy comm |
| `casinoCommission` | number | Casino comm rate |
| `betChipsData` | object | Chip buttons {100:100, 500:500…} |
| `referralCode` | string | Referral code |
| `isPasswordChanged` | boolean | Password changed flag |
| `status` | number | 1=active, 0=inactive |
| `isDeleted` | boolean | Soft delete |
| `createdAt` | date | Created time |
| `updatedAt` | date | Last update |

**Indexes:** username (unique), userId (unique), userType, parentId

**APIs:** user/login, user/create, user/userList, centerPanel/userLogin, centerPanel/createCustomer…

---

### `auth_sessions` — Login tokens

| Field | Type | Description |
|-------|------|-------------|
| `token` | string | JWT / session token |
| `userId` | string | → users.userId |
| `username` | string | Login username |
| `expiresAt` | date | Token expiry |
| `createdAt` | date | Login time |

**Indexes:** token, userId

---

### `user_activities` — Activity log

| Field | Type | Description |
|-------|------|-------------|
| `userId` | string | → users.userId |
| `activityType` | enum | login \| logout \| bet \| update \| transfer |
| `ip` | string | User IP |
| `device` | string | Browser/device info |
| `payload` | object | Extra data (panel name, etc.) |
| `createdAt` | date | Activity time |

**Indexes:** userId + createdAt, activityType

---

## 2. DOMAIN & WEBSITE

### `domains` — Site settings per domain

| Field | Type | Description |
|-------|------|-------------|
| `domainName` | string | e.g. 1ex99.in (unique) |
| `domainUrl` | string | Domain URL |
| `title` | string | Site title |
| `userNotification` | string | Admin notification text |
| `clientNotification` | string | Client-facing notification |
| `themeSetting` | object | Colors, theme config |
| `sportsSetting` | object | Sports config |
| `socialMedia` | object | Social links |
| `banner` | array | Banner images [{name, priority, image}] |
| `account` | object | Bank account settings |
| `upi` | object | UPI (paytm, googlePay, phonePay…) |
| `signUpBonusSetting` | object | Signup bonus config |
| `minimumWithdrawAmount` | number | Min withdraw |
| `maximumWithdrawAmount` | number | Max withdraw |
| `isSignUpOtp` | boolean | OTP on signup |
| `status` | boolean | Domain active? |

**Indexes:** domainName (unique)

---

## 3. SPORTS BETTING

### `sports_catalog` — Sport types

| Field | Type | Description |
|-------|------|-------------|
| `sportId` | number | 4=Cricket, 1=Soccer… (unique) |
| `sportName` | string | Sport name |
| `status` | boolean | Active? |

---

### `matches` — Match + market data

| Field | Type | Description |
|-------|------|-------------|
| `marketId` | string | Main market ID (e.g. 1.245690241) |
| `eventId` | string | Event ID |
| `sportId` | number | Sport type |
| `seriesId` | number | Series/tournament ID |
| `seriesName` | string | e.g. Indian Premier League |
| `matchName` | string | Match display name |
| `matchType` | string | T20, ODI, etc. |
| `matchDate` | string | Match datetime |
| `sportName` | string | Sport label |
| `sportType` | string | Cricket, Football… |
| `status` | string | INPLAY, UPCOMING, CLOSED |
| `isMatchOdds` | boolean | Match odds enabled |
| `isFancy` | boolean | Fancy/session enabled |
| `isBookmaker` | boolean | Bookmaker enabled |
| `isToss` | boolean | Toss market |
| `isTieOdds` | boolean | Tie market |
| `isCompletedOdds` | boolean | Completed match market |
| `isTv` | boolean | Live TV |
| `isScore` | boolean | Live score |
| `betPerm` | boolean | Betting allowed |
| `betDelayTime` | number | Bet delay seconds |
| `betDelaySetting` | object | {matchOddsBetDelay, bookMakerBetDelay…} |
| `maxMinCoins` | object | Min/max bet limits per market type |
| `marketList` | array | Sub-markets [{marketId, marketType, selectionIdData[]}] |
| `teamData` | string (JSON) | Team/runner list |
| `cacheUrl` | string | Odds cache API URL |
| `socketUrl` | string | Live socket URL |
| `scoreIframe` | string | Score widget URL |
| `tvUrl` | string | Live TV URL |
| `wonTeamName` | string \| null | Declared winner |
| `createdAt` | date | Created |
| `updatedAt` | date | Updated |

**Indexes:** marketId, eventId

**marketList[] sub-fields:**

| Sub-field | Type | Description |
|-----------|------|-------------|
| `marketId` | string | Sub-market ID |
| `marketType` | string | Match Odds, Tied Match, Completed Match |
| `selectionIdData` | array | Runners [{selectionId, runnerName, handicap}] |
| `status` | string | INPLAY etc. |
| `playStatus` | boolean | Betting on? |

---

### `sports_bets` — Placed sports bets

| Field | Type | Description |
|-------|------|-------------|
| `betId` | string | Unique bet ID |
| `userId` | string | → users.userId |
| `marketId` | string | → matches.marketId |
| `eventId` | string | Event ID |
| `selectionId` | number | Runner selection |
| `runnerName` | string | Team/player name |
| `stake` | number | Bet amount |
| `odds` | number | Odds rate |
| `betType` | string | B=Back, K=?, L=Lay, N=? |
| `betFor` | string | match, session, toss… |
| `oddsType` | string | match, fancy… |
| `marketType` | string | Match Odds, Bookmaker… |
| `profitLoss` | number | P/L after settle |
| `status` | enum | open \| settled \| void |
| `createdAt` | date | Bet time |

**Indexes:** userId, marketId, createdAt

---

### `positions` — Live exposure per market

| Field | Type | Description |
|-------|------|-------------|
| `userId` | string | → users.userId |
| `marketId` | string | → matches.marketId |
| `selectionId` | number | Runner |
| `runnerName` | string | Runner name |
| `position` | number | Net position amount |
| `exposure` | number | Risk/exposure (negative = loss risk) |

**Indexes:** userId + marketId

---

## 4. CASINO

### `casino_games` — Game catalog

| Field | Type | Description |
|-------|------|-------------|
| `eventId` | number | Game ID e.g. 3030 (unique) |
| `name` | string | 20-20 Teenpatti |
| `shortName` | string | teen20 |
| `minStake` | number | Min bet |
| `maxStake` | number | Max bet |
| `betStatus` | boolean | Betting open? |
| `cashinoStatus` | boolean | Game active? |
| `isDisable` | boolean | Disabled? |
| `isVirtual` | boolean | Virtual game? |
| `socketURL` | string | Live data socket |
| `cacheURL` | string | Cached odds API |
| `videoUrl1` | string | Stream URL 1 |
| `videoUrl2` | string | Stream URL 2 (1ex99.in) |
| `videoUrl3` | string | Stream URL 3 |
| `fetchData` | string | socket / api |
| `setting` | object | {oddsDifference, errorMessage} |
| `createdAt` | date | Created |

**Indexes:** eventId (unique)

---

### `casino_bets` — Casino bets

| Field | Type | Description |
|-------|------|-------------|
| `betId` | string | Unique bet ID |
| `userId` | string | → users.userId |
| `eventId` | number | → casino_games.eventId |
| `roundId` | string | Round ID |
| `stake` | number | Bet amount |
| `selection` | string | Player A, Banker… |
| `profitLoss` | number | P/L |
| `gameType` | enum | diamond \| aviator |
| `status` | string | open, settled… |
| `createdAt` | date | Bet time |

**Indexes:** userId, eventId

---

### `casino_rounds` — Round results

| Field | Type | Description |
|-------|------|-------------|
| `eventId` | number | → casino_games.eventId |
| `roundId` | string | Round ID |
| `result` | object | {winner, cards[]} |
| `createdAt` | date | Result time |

**Indexes:** eventId + roundId

---

### `day_wise_casino` — Daily casino report rows

| Field | Type | Description |
|-------|------|-------------|
| `_id.date` | string | Date key |
| `eventId` | number | Game ID |
| `eventName` | string | Game name + date |
| `userNetProfitLoss` | number | Agent net P/L |
| `userOddsComm` | number | Commission |
| `clientOddsAmount` | number | Client odds amount |
| `clientNetAmount` | number | Client net |
| `createdAt` | date | Report time |

---

## 5. MATKA

### `matka_events` — Matka draws

| Field | Type | Description |
|-------|------|-------------|
| `matkaEventId` | string | Unique event ID (unique) |
| `name` | string | Kalyan, Milan Day… |
| `openTime` | date | Open time |
| `closeTime` | date | Close time |
| `result` | string | Declared result |
| `status` | string | open, closed, declared |

---

### `matka_bets` — Matka bets

| Field | Type | Description |
|-------|------|-------------|
| `betId` | string | Bet ID |
| `userId` | string | → users.userId |
| `matkaEventId` | string | → matka_events |
| `number` | string | Bet number e.g. 123 |
| `stake` | number | Amount |
| `profitLoss` | number | P/L |
| `status` | string | open, settled |
| `createdAt` | date | Bet time |

---

## 6. FINANCE / LEDGER

### `ledger_entries` — Har credit/debit row

| Field | Type | Description |
|-------|------|-------------|
| `ledgerId` | string | Entry ID |
| `userId` | string | → users.userId |
| `type` | enum | credit \| debit |
| `amount` | number | Amount |
| `description` | string | Reason text |
| `category` | enum | sport \| casino \| matka \| cash |
| `balanceAfter` | number | Balance after entry |
| `createdAt` | date | Entry time |

**Indexes:** userId + createdAt

---

### `user_ledger` — Ledger summary (API response cache)

| Field | Type | Description |
|-------|------|-------------|
| `userId` | string | → users.userId |
| `totalCoins` | number | Total balance |
| `creditAmount` | number | Total credit |
| `debitAmount` | number | Total debit |
| `calAmount` | number | Calculated amount |
| `sportLedger` | number | Sports P/L |
| `diamondCasinoLedger` | number | Diamond casino P/L |
| `intCasinoLedger` | number | Int casino P/L |
| `matkaLedger` | number | Matka P/L |
| `cashLedger` | number | Cash P/L |
| `ledgerData` | array | Detailed ledger rows |

---

### `statements` — Account statement

| Field | Type | Description |
|-------|------|-------------|
| `statementId` | string | Statement ID |
| `userId` | string | → users.userId |
| `startDate` | date | From date |
| `endDate` | date | To date |
| `rows` | array | [{date, description, credit, debit}] |
| `totalCredit` | number | Sum credit |
| `totalDebit` | number | Sum debit |

---

### `profit_loss` — User P/L summary

| Field | Type | Description |
|-------|------|-------------|
| `userId` | string | → users.userId |
| `profitLoss` | number | Net P/L |
| `exposure` | number | Current exposure |
| `payload` | object | Extra report data |

---

### `bpexch_accounts` — Bpexch wallet

| Field | Type | Description |
|-------|------|-------------|
| `userId` | string | → users.userId |
| `balance` | number | Current balance |
| `statement` | array | [{date, type, amount}] |

---

## 7. REPORTS (cached)

### `reports` — Generic report cache

| Field | Type | Description |
|-------|------|-------------|
| `reportType` | string | e.g. casino/dayWiseCasinoReport |
| `userId` | string | → users.userId |
| `marketId` | string \| null | Optional market |
| `payload` | object | Full report JSON |
| `createdAt` | date | Cached time |

**Indexes:** reportType + userId

---

## 8. CENTER PANEL (Admin backend)

### `center_projects`

| Field | Type | Description |
|-------|------|-------------|
| `projectId` | string | Project ID |
| `name` | string | Project name |
| `projectName` | string | Display name |
| `domainUrl` | string | 1ex99.in |
| `status` | number/boolean | Active? |

---

### `center_domain_ips`

| Field | Type | Description |
|-------|------|-------------|
| `domainUrl` | string | Domain |
| `ip` | string | Server IP |
| `userId` | string | Assigned user |

---

### `center_events`

| Field | Type | Description |
|-------|------|-------------|
| `eventType.id` | string | Sport ID |
| `eventType.name` | string | Cricket, Soccer… |
| `marketCount` | number | Active markets count |

---

### `center_series`

| Field | Type | Description |
|-------|------|-------------|
| `seriesId` | string | Series ID |
| `seriesName` | string | IPL, ICC WTC… |
| `sportId` | number | Sport type |
| `sportName` | string | Sport label |
| `marketCount` | number | Markets in series |

---

### `center_custom_series`

| Field | Type | Description |
|-------|------|-------------|
| `seriesId` | string | Custom series ID |
| `sportId` | number | Sport |
| `seriesName` | string | Name |
| `status` | string | active/inactive |
| `source` | string | latiyal/manual |
| `createdAt` | date | Created |

---

### `center_racing_events`

| Field | Type | Description |
|-------|------|-------------|
| `eventId` | string | Race event ID |
| `competitionId` | string | Competition |
| `eventName` | string | Race name |
| `venue` | string | Location |
| `startTime` | date | Start |
| `status` | string | scheduled, finished |

---

### `center_manual_fancy`

| Field | Type | Description |
|-------|------|-------------|
| `fancyId` | string | Fancy ID |
| `marketId` | string | Market |
| `sessionName` | string | 10 Over Runs |
| `runsYes` | number | Yes runs line |
| `runsNo` | number | No runs line |
| `oddsYes` | number | Yes odds |
| `oddsNo` | number | No odds |
| `status` | string | active |

---

### `center_manual_bookmaker`

| Field | Type | Description |
|-------|------|-------------|
| `bookmakerId` | string | BM ID |
| `marketId` | string | Market |
| `runnerName` | string | Team name |
| `back` | number | Back odds |
| `lay` | number | Lay odds |
| `status` | string | active |

---

### `center_squad_templates`

| Field | Type | Description |
|-------|------|-------------|
| `templateId` | string | Template ID |
| `sportId` | number | Sport |
| `name` | string | Template name |
| `players` | array | Player names list |

---

### `center_betfair_results`

| Field | Type | Description |
|-------|------|-------------|
| `marketId` | string | Market |
| `eventId` | string | Event |
| `winnerSelectionId` | number | Winning runner |
| `resultStatus` | string | declared |
| `declaredAt` | date | Declare time |

---

### `center_manual_scores`

| Field | Type | Description |
|-------|------|-------------|
| `eventId` | string | Event |
| `marketId` | string | Market |
| `score` | object | {team1, team2, overs} |
| `updatedAt` | date | Last update |

---

### `center_fancy_categories`

| Field | Type | Description |
|-------|------|-------------|
| `categoryId` | string | Category ID |
| `name` | string | Normal Fancy |
| `status` | boolean | Active? |

---

### `center_fancy_audit`

| Field | Type | Description |
|-------|------|-------------|
| `fancyId` | string | Fancy ID |
| `marketId` | string | Market |
| `action` | string | create, update, delete |
| `userId` | string | Admin user |
| `payload` | object | Change details |
| `createdAt` | date | Log time |

---

### `center_master_settings`

| Field | Type | Description |
|-------|------|-------------|
| `settingKey` | string | e.g. betDelayGlobal (unique) |
| `value` | mixed | Setting value |
| `description` | string | What it does |
| `updatedAt` | date | Last change |

---

### `decision_logs` — Result declare history

| Field | Type | Description |
|-------|------|-------------|
| `logId` | string | Log ID |
| `marketId` | string | Market |
| `eventId` | string | Event |
| `action` | string | declare_result |
| `payload` | object | Result details |
| `createdAt` | date | Log time |

---

## Relationships — Kaun Kis Se Judta Hai

```
users.userId
  ├── auth_sessions.userId
  ├── user_activities.userId
  ├── sports_bets.userId
  ├── casino_bets.userId
  ├── matka_bets.userId
  ├── positions.userId
  ├── ledger_entries.userId
  ├── user_ledger.userId
  ├── statements.userId
  ├── profit_loss.userId
  ├── reports.userId
  └── bpexch_accounts.userId

matches.marketId
  ├── sports_bets.marketId
  ├── positions.marketId
  ├── center_manual_fancy.marketId
  ├── center_manual_bookmaker.marketId
  └── decision_logs.marketId

casino_games.eventId
  ├── casino_bets.eventId
  ├── casino_rounds.eventId
  └── day_wise_casino.eventId

matka_events.matkaEventId
  └── matka_bets.matkaEventId

domains.domainName
  └── center_projects.domainUrl (same domain)
```

---

## Useful MongoDB Commands

```bash
# Saari databases dekho
mongosh --eval "db.adminCommand('listDatabases')"

# Is database ke collections
mongosh ex99_local --eval "db.getCollectionNames()"

# Kisi table ka sample doc
mongosh ex99_local --eval "db.users.findOne()"

# Count
mongosh ex99_local --eval "db.users.countDocuments()"
```

---

## File Location

- **Yeh document:** `1exscrape/mongodb/1EX_DATABASE_STRUCTURE.md`
- **Machine-readable schema:** `1exscrape/mongodb/collections_schema.json`
- **Per-table JSON:** `1exscrape/mongodb/tables/*.json`
- **Seed data:** `1exscrape/mongodb/seed/*.json`

---

*Generated from live `ex99_local` database + collections_schema.json*
