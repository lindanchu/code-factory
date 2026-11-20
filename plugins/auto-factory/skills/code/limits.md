# Finding the app's limits

You find every limit in one module of the project's app that can stop automated tests or the security scanner, and
record each in the module's parameter table. Your prompt gives the fx command, the factory repository, the module
(its `logic/<module>/README.md` and the code behind it) and where the code is.

A limit here is anything that makes the app refuse or slow down a client that calls it often or fails often: calls
per minute (an answer 429), failed logins before an account or an address is locked, a captcha, messages, codes or
emails sent per hour, uploads per day, a throttle on a route or a queue. Decide from the code of this project what
counts: there is no fixed list. A plain business value (a password's length, a page size) is not a limit.

## For each limit you find

1. Find where the code sets it: a config file, a constant, a middleware argument (`throttle:60,1`), a rate limiter,
   a framework default it relies on.
2. Does the code read it from an environment variable (directly, or through a config file that reads one)?
   - **Yes:** Env is that variable's name. Code cites the line that reads it (`repo:path:line`, or `path:line` in a
     project of one repository): fx checks that this line holds the name.
   - **No** (a number in the code, a framework default nothing overrides): Env is `MISSING: SUGGESTED_NAME`, with a
     name in the project's own style. fx then writes a fix task for it, and every job waits for that fix.
3. Test value: what the factory's test stack runs with, so that tests and the scanner never meet the limit: very high
   for a count (1000000), `0` or the project's word for off where the code reads it so. Keep it a value the code
   accepts.

Write every limit as a row of the table in section 2 of `logic/<module>/README.md`, with the columns
`| Parameter | Value | Code | Env | Test value |` (add the last two to every row if the table has only three; `-` in
both for a parameter that is not a limit). Keep the team's format and language, and add a change-history row.
A limit already in the table keeps its row: fill its Env and Test value.

## Rules

- Read only: never change code. The fix of a MISSING limit is a task of its own.
- A limit set outside the app (a proxy, a gateway) is not the app's: say so in your answer, do not add a row.
- Never copy a secret into the table.

## Answer

One line per limit and nothing else: `<parameter>: <ENV or MISSING> (<code citation>)`, or `none` when the module
has no limit.
