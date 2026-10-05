# Open questions

Things known to be unresolved, with the experiment that would settle each.
Kept here rather than in issues so the method stays next to the code.

---

## 1. Probe the 95 "absent" commands for more hidden ones

**Status:** open. One confirmed hit so far.

`ABSENT_FROM_7_4_4407` was built by diffing the device's `help` against the
vendor's published reference. That method is **unsound**: `wifi_ssid_set` was in
that set and turned out to be present, just unlisted. So the set means
"not printed by `help`", not "not implemented", and an unknown number of the
remaining 95 are probably hidden too.

**Experiment.** Bare invocation is an existence oracle:

| response | meaning |
|---|---|
| `E: Invalid argument(s)` | present, hidden |
| `Command '<x>' not recognised.` | genuinely absent |

Safe *only* for commands with at least one **required argument** — validation
rejects the call before anything happens.

> **Do not probe nullary commands.** A bare call to `fs_format`, `cc3100_format`,
> `scpu_reset`, `scpu_upgrade` etc. does not fail — it **executes**. Filter the
> candidate list by arity from the vendor reference first, and skip anything
> whose arity is unknown.

Expected payoff: entire families (`touch_*`, `frontlight_*`, `scpu_*`) are listed
as absent on hardware that may well support them. Each hidden command found is a
capability the library currently refuses to use.

---

## 2. Can this firmware represent an SSID containing spaces?

**Status:** unblocked, not yet run.

Originally believed impossible, because the vendor documents `wifi_ssid_set` as
the workaround for spaced SSIDs and it is missing from `help`. It is in fact
present (see above), and it takes the **SSID alone** — no PSK argument — so the
test needs no credentials.

**Experiment.** With a known-good AP whose name contains a space and an
apostrophe, and *without* `flash_save` so a reboot reverts everything:

1. `wifi_conf_get` — record `SSID`, `Security`, `Band`
2. `wifi_ssid_set <name with spaces>` — try bare, `"quoted"`, and `\ ` escaped
3. `wifi_conf_get` — did it round-trip exactly?
4. `wifi_ssid_set <original>` to restore; do **not** `flash_save`

Answers whether the console tokenises on whitespace or takes the rest of the
line verbatim, and whether a quoting convention exists. The result decides
whether `provisioning.py` can honestly claim to configure arbitrary SSIDs.

Requires serial access for recovery, since a bad SSID drops the device off the
network until reverted.
