# Phase 3 phone approvals: live check (2026-10-23)

`spike/phase3_phone.py` ran against the live gateway with ntfy turned on in the phone settings. It sent
a readiness question to the phone and waited for it to be answered. Then, as a temporary MCP client, it
clicked Send in a WinForms test window (`spike/phonetest.ps1`) and, 10 s after that question closed,
Delete. The person's plan was Allow then Confirm on Send, and Deny on Delete. The phone was an Android
phone with the ntfy app subscribed to the question topic on ntfy.sh; no PC was subscribed.

## Run I, the one that passed ([result.json](result.json))

Times are local; each answer was read from the reply topic.

| time | on the phone | answer |
|---|---|---|
| 19:08:39 | readiness question | Allow |
| 19:08:45 | its confirmation | Confirm |
| 19:08:58 | "click on 'Send'" arrives | |
| 19:09:16 | | Allow; the confirmation arrives |
| 19:09:18 | | Confirm |
| 19:09:29 | "click on 'Delete'" arrives | |
| 19:09:37 | | Deny |

- Send was clicked 20.4 s after the agent asked, with `approved_by: "ntfy"`.
- Delete was refused after 9.1 s with `approval_denied` and `answered_by: "ntfy"`.
- The test window saw only the Send click.

The person was asked to answer every question in the pulled-down notification list.

## Run J, after the titles named the action ([result-j.json](result-j.json))

From b6e6a0b, the titles read "Allow click on 'Send'?" and "Confirm click on 'Send'?" and the Confirm
button reads "Confirm Send". Run J went back to a 1.5 s gap between questions (`--gap 1.5`), as in runs
G and H, and the person was asked to tap wherever they normally would.

| time | on the phone | answer |
|---|---|---|
| 19:26:07 | "Allow click on 'Send'?" arrives | |
| 19:26:13 | | Allow; "Confirm click on 'Send'?" arrives |
| 19:26:17 | | Confirm Send |
| 19:26:19 | "Allow click on 'Delete'?" arrives | |
| 19:26:21 | | Deny |

Send was clicked with `approved_by: "ntfy"`, Delete was refused with `answered_by: "ntfy"`, and the
window saw only Send. Two passing runs don't show the earlier wrong answers are gone, since their
cause is still unknown.

## Earlier runs

In these runs the second question came 1.5 s after the first one closed.

- **Runs A and B** (before the Confirm step): Send was approved from the phone. Delete was approved too:
  the phone posted Allow for Delete, though the person tapped Deny. Both clicks reached the window.
- **Runs C and D** don't count: the person also answered the box on the PC, or the person's own input
  paused the agent.
- **Run E**: Send was answered at the desk. A phone Deny refused Delete.
- **Run G**: the phone's ntfy log (read over adb, ntfy's lines only) shows Deny on Send, though the person
  tapped Allow. A phone Deny refused Delete.
- **Run H** (the first with the Confirm step, 2cb6d3c): the phone sent Deny for Send, then Allow and
  Confirm for Delete, the Confirm 2 s after the confirmation arrived. Delete was clicked in the test
  window. A stray tap is caught by Confirm; two taps on the wrong question are not.

In every run the phone's log named the button it ran, and the reply topic received that button's
answer. ntfy-android gives each button its own intent, and no screenshot was attached, so no download
or open buttons shared the row with Allow and Deny. Why the phone ran a different button, or answered a
different question, from the one the person meant is not known. Run I differs from G and H in two
ways: the questions were 10 s apart, and the answers were given in the notification list.
