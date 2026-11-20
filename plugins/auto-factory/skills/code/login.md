# Working out how the factory logs in

You find how a test account logs in to the project's app, so that the security scanner (ZAP) and every agent can log
in the same saved way. Your prompt gives the fx command, the factory repository, the kind or kinds to work out
(`web` and/or `api`), why the saved way failed (if it did), and either a job (its app runs for you to try on) or
`init` (no job yet).

The two kinds are always kept apart, each with its own logic:

- **web**: how a person logs in through the pages: the login form, its fields, a CSRF token if the form has one, and
  the session cookie the app sets. fx sends the result as the `Cookie` header.
- **api**: how a client of the API proves who it is: a token from a login endpoint, an API key issued by a command
  or a seeder, or another scheme the API reads (`Authorization: Bearer …`, an `X-Api-Key` header…). fx sends it as
  the header you name (default `Authorization`).

## What to read

1. The knowledge's description first (`knowledge/<repo>/environment.md`, sections "Đăng nhập giao diện" and
   "Đăng nhập API" or their equivalent in the knowledge's language), and why the saved way failed: often one thing
   changed (a field name, the account, a command).
2. The code: the login routes and controllers, the auth middleware and guards, how the API authenticates, the
   seeders or fixtures that create test accounts and their roles, and any command that issues tokens or keys. With a
   job, read its branch too: its own change may be what changed login.

## Rules

- **Test accounts of the test stack only**, from its seed data. Never a real person's account, never production.
- **Never write a password, token or key into the command.** The command reads it when it runs: from the seed file,
  from the stack's environment, or by issuing a fresh key on the stack. The command runs in the app's folder with the
  app's environment, and prints the header's value as its **last line**.
- **The account:** list the roles you found. Use the one with the most access, unless the user said otherwise, and
  say which pages or routes only another role can open (the user decides on a scan as that role).
- **A check page** for each kind: a page or endpoint that needs exactly this login, answers 2xx with it and 401, 403
  or a redirect to the login page without it. fx calls it before and after every scan.
- **Keeping the session:** if a form the scanner will submit can end the session (logout, change password, delete
  account), say so: fx notices a lost login after the scan and stops, and the project may need its test stack to
  keep the scanner's session.

## Try and save

- **With a job:** start its app (`fx app up <job>`), then try each way until its check answers 2xx:
  `fx login <job> --kind web|api --command '<command>' --header <header> --check <path> --role <role>`.
  Save it with `--save job` when the job's own change is what changed login (it then stays with the job until its
  pull request is merged), otherwise with `--save factory`. Stop the app when you are done (`fx app down <job>`).
- **At init:** no app runs yet. Save with `--untested --save factory`; `fx selftest` proves it right after.
- Then write the description in `knowledge/<repo>/environment.md`, one section per kind: how the login works, the
  account and its role, the check page, what the command does, and what only another role can open. Write it in
  the knowledge's language. Never copy a password, token or key into it.

## Answer

One line per kind and nothing else:

```
web: saved (factory|job), role <role>, check <path>
api: saved (factory|job), role <role>, check <path>
web: ask: <what you need from the user, in plain words>
```

When fx said most answers were 429 (or the app locked the account), the login works and a limit of the app stopped
the scan: answer `limits: <what refused, where>` for that kind, and the orchestrator works the limits out
(/auto-factory:code limits).

Answer `ask` when the code does not tell you how to log in (an external identity provider, a one-time code by email
or phone, a captcha): the user decides.
