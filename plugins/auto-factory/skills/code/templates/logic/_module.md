# Module <name> (<slug>)

**Parent ticket:** <KEY or ->
**Last updated:** <dd.mm.yyyy>

| Feature | File |
| ------- | ---- |

## 1. Module-wide rules
| Rule | Code | Test |
| ---- | ---- | ---- |

## 2. Parameters
| Parameter | Value | Code | Env | Test value |
| --------- | ----- | ---- | --- | ---------- |
<!-- a value nobody has settled is written _Not settled_.
     Env and Test value are for limits that can stop tests or scanners (calls per minute, failed logins before a
     lock, a captcha, messages sent per hour...): Env names the environment variable the code reads it from, Test
     value what the factory's test stack runs with. A limit the code does not read from the environment is
     MISSING (or MISSING: SUGGESTED_NAME), which stops every job until it is fixed. Any other parameter: - in both. -->

## Permissions
| Method | Route | guest | <role> | <role> | Note |
| ------ | ----- | ----- | ------ | ------ | ---- |
<!-- who may call each route of this module: one row per method and route, as the app names it; one column per kind
     of user, named as in the factory's stack/permissions.json; a cell is yes (on any data), no, or own (on the
     caller's own data only). The factory calls every cell on its test stack in every gate. The user confirms the
     table; a new route gets its rows from its ticket, never from its code. -->

## 3. Open points (module-wide)
1. ...

## Change history
| Date | Change | Ticket |
| ---- | ------ | ------ |
