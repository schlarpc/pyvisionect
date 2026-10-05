'''What is known about ``encryption_key_set`` / ``encryption_mode_set``, and how.

**No setter in this module has ever been executed.**  Everything below comes from
three read-only sources: the device's own ``help`` text and
``encryption_config_get`` reply, the vendor's TCLV descriptor table, and a
disassembly of the vendor's gateway.  Where those run out, it says so.

The question this answers
-------------------------

The gateway's AES-128-CBC "Secure" mode cannot be turned on in a self-hosted
deployment, because the per-connection key is minted by an **escrow service** at
``$VISIONECT_SERVER_EA`` which has no compiled-in default and is not part of the
shipped image.  The obvious hope is that since the sign exposes
``encryption_key_set`` over USB, you could set the key yourself on both ends and
have encryption without the vendor's cloud.

**That hope does not survive the evidence.**  See :data:`VERDICT`.  The short
version: the key the gateway encrypts with is 16 bytes of ``crypto/rand`` minted
fresh per activation, and the gateway never transmits it -- so a key stored in
the device's flash cannot be it.  TCLV 131 is a long-term device secret used to
resolve the escrow blob, and reproducing that resolution means reading firmware
that ships encrypted.

The usable alternative is **TLS**, which is a different mechanism entirely and is
not gated on any of this.  See :data:`TLS_ALTERNATIVE`.

Mode values -- solid
--------------------

``encryption_mode_set <mode>`` writes TCLV **131**'s companion, TCLV **130**,
whose description in the vendor's own table reads verbatim:

    ``Outbound encryption: 0=Disabled, 1=Enabled``

So the argument is a **boolean, 0 or 1**, and :data:`MODES` says so.  It is *not*
the wire protocol's ``SecurityType``, which takes 0, 2 and 3 -- a confusion worth
heading off, because both are called a "mode" in different places:

====  ===================================  ===========================
mode  ``ProtocolHeader.Security``           what it is
====  ===================================  ===========================
0     0                                    plaintext
1     2 (ordinary) / 3 (security packet)   AES-128-CBC, session key
\\-    1                                    **not a socket value** -- the same
                                           20-byte header is reused as a
                                           firmware file container, where 1
                                           means "encrypted with the firmware
                                           key"
====  ===================================  ===========================

The mapping ``mode 1 -> Security 2`` is itself an inference: the gateway picks 2
whenever it holds a key and 0 otherwise, with nothing in between, and the device
side could not be read.

Key format -- not solid
-----------------------

The primitive is **AES-128**, so 16 bytes of key material.  That part is firm:
``securityManager.IssueKey`` does ``key := make([]byte, 16);
crypto/rand.Read(key)`` and asserts the length.  How those 16 bytes are
*spelled* on a CLI line is not known, and :data:`KEY_FORMAT_CANDIDATES` keeps
all three readings rather than picking one and pretending.

What constrains it:

* The CLI is whitespace-delimited and single-line, so the key is one token with
  no spaces.
* ``encryption_config_get`` echoes the key back **inside single quotes**
  (``Key: ''`` when unset), so the device stores and renders it as a printable
  string, not as raw bytes.
* Every other key and IV in this system is a **16-character printable ASCII
  literal** used directly as the 16 key bytes -- ``F@%gtb7;xLmXV$9a``,
  ``J<lyAM$*B@<G{`9i``, ``thisbeemulatorke``, ``N3ls0#!Dba0f8*B>``.  That is the
  house style, and it is the single strongest hint available.
* The only place the system serialises a *generated* key is the escrow POST,
  which uses ``base64.StdEncoding`` -- 24 characters for 16 bytes.

So "16 printable characters, used verbatim" is the best-supported reading and
base64 is second, but neither was tested and a length check in the firmware could
rule either out instantly.  The firmware images are AES-encrypted under a key
that lives only on Visionect's servers, so the CLI handler cannot be read.

How to settle it, if you ever want to
-------------------------------------

Cheaply and reversibly, on hardware you are willing to inconvenience:

1. ``encryption_config_get`` -- record the current state (it is
   ``Disabled`` / ``''`` on a factory sign).
2. ``encryption_key_set <candidate>`` with a 16-character ASCII string.
3. ``encryption_config_get`` again.  **This is the whole experiment**: the
   command echoes the stored key, so if the device accepted and stored 16
   characters verbatim you can see it, and if it hashed, truncated, rejected or
   re-encoded them you can see that too.
4. Do **not** run ``flash_save``.  Power-cycle, and the sign is exactly as it
   was.

That sequence never enables encryption -- it only writes TCLV 131, leaving TCLV
130 at 0 -- so the link stays plaintext and the sign stays on its server
throughout.  It was not run here because the sign in question is in service and
the question is not worth even a small risk to it.  **Mode 1 is the dangerous
one**: enabling it points the device at a key exchange its server cannot
complete, and the recovery path (``IPV4_SERVER_IP``) is USB-only.
'''

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "AES_KEY_BYTES",
    "KEY_FORMAT_CANDIDATES",
    "MODES",
    "TCLV_ENCRYPTION_KEY",
    "TCLV_ENCRYPTION_MODE",
    "TCLV_TLS_MODE",
    "TLS_ALTERNATIVE",
    "VERDICT",
    "KeyFormatCandidate",
]

TCLV_ENCRYPTION_MODE = 130
"""``encryption_mode_set`` -> *"Outbound encryption: 0=Disabled, 1=Enabled"*."""

TCLV_ENCRYPTION_KEY = 131
"""``encryption_key_set`` -> *"Encryption key"*. No further description exists."""

TCLV_TLS_MODE = 145
"""*"TLS mode: 0=disabled, 1=TLS 1.3"*. A different mechanism -- see :data:`TLS_ALTERNATIVE`."""

AES_KEY_BYTES = 16
"""AES-128. Firm: the gateway mints exactly 16 ``crypto/rand`` bytes and asserts it."""

MODES: dict[int, str] = {0: "Disabled", 1: "Enabled"}
"""Valid ``encryption_mode_set`` arguments, from the vendor's TCLV 130 description.

Verbatim from the descriptor table, so this one is not a guess.  Note it is a
boolean and **not** the wire ``SecurityType`` (0/2/3).
"""


@dataclass(frozen=True, slots=True)
class KeyFormatCandidate:
    """One possible spelling of the 16-byte key on a CLI line.

    Attributes:
        name: short label.
        length: how many characters the CLI token would be.
        example: a syntactically valid example. **Not a working key** -- there is
            no such thing without the escrow service.
        support: the evidence for this reading.
        tested: always False. Nothing in this module was executed.
    """

    name: str
    length: int
    example: str
    support: str
    tested: bool = False


KEY_FORMAT_CANDIDATES: tuple[KeyFormatCandidate, ...] = (
    KeyFormatCandidate(
        name="16 printable ASCII characters, used verbatim",
        length=16,
        example="F@%gtb7;xLmXV$9a",
        support=(
            "Best supported. Every key and IV elsewhere in this system is a "
            "16-character printable ASCII literal used directly as the 16 key "
            "bytes (the two traffic IVs, the emulator key, the DeviceID hash "
            "key). encryption_config_get echoing the key inside single quotes "
            "fits a stored string, and 16 characters is exactly AES-128."
        ),
    ),
    KeyFormatCandidate(
        name="base64 of 16 raw bytes",
        length=24,
        example="AAECAwQFBgcICQoLDA0ODw==",
        support=(
            "Second. The one place the system serialises a generated key is the "
            "escrow POST, which uses base64.StdEncoding on exactly these 16 "
            "bytes. If the CLI mirrors the wire format, this is it. Against: the "
            "'=' padding is an odd thing to type, and base64 is unusual in an "
            "embedded CLI."
        ),
    ),
    KeyFormatCandidate(
        name="32 hex characters",
        length=32,
        example="464025677462373b784c6d5856243961",
        support=(
            "Third, on general priors rather than evidence: hex is the usual way "
            "to type a key at an embedded prompt. Nothing in this system "
            "actually does it, which is why it is last."
        ),
    ),
)
"""The three readings of the key format, with their evidence. **None was tested.**

Ordered by how well the system's own conventions support them.  The experiment
in the module docstring distinguishes all three in one command, because
``encryption_config_get`` echoes whatever was stored.
"""

VERDICT = '''A USB-set key does not buy you self-hosted link encryption.

The reasoning, in the order the evidence falls:

1. The gateway encrypts with a key it mints itself, per activation:
   ``securityManager.IssueKey`` does ``key := make([]byte, 16);
   crypto/rand.Read(key)``. It is random and fresh every time.
2. The gateway never sends that key to the device. ``activateSecurity`` sends a
   ``packet.Raw`` containing the **escrow service's response bytes verbatim**,
   which the gateway itself treats as opaque, and only then installs the key on
   its own codec.
3. So the device must derive or unwrap the session key from that blob, using
   something it already holds -- which is what TCLV 131 is for. TCLV 131 is a
   long-term device secret, not the session key.
4. To issue a blob the device will accept, you would have to reproduce the
   transform the device applies to it. That code is in the device firmware, and
   the firmware images ship AES-encrypted under a key held only on Visionect's
   servers.
5. Therefore setting TCLV 131 over USB is necessary-but-not-sufficient, and the
   missing piece is not something USB access provides.

A corollary worth knowing: with ``VISIONECT_SERVER_EA`` unset the escrow POST
fails, ``IssueKey`` errors, and ``SetSecurity`` fails -- so a self-hosted gateway
with ``Options["Secure"] = "true"`` does not fall back to plaintext, it fails the
connection. Leave ``Secure`` alone.

What this does *not* say: it does not say the mode values are unknown (they are
0 and 1, from the vendor's own table), and it does not say encryption of any kind
is impossible. See TLS_ALTERNATIVE.
'''
"""Why the USB key is not the route to encryption. Reasoned, not tested."""

TLS_ALTERNATIVE = '''Use TLS instead. It is the mechanism that actually works.

TCLV **145** is *"TLS mode: 0=disabled, 1=TLS 1.3"*, and three things make it the
better target:

* **It is network-writable.** 145 is not one of the eight ``canWrite: false``
  ids, so a server can set it over the link it already has -- no USB, no cable,
  no bootstrap.
* **The gateway's TLS is opportunistic on the same port.** A plain-TCP device and
  a TLS device share port 11113; the server offers TLS 1.3 with P-521/384/256 and
  ordinary system trust. So a reimplementation can wrap its listener in TLS and
  serve both.
* **There is no certificate pinning on the device link**, so your own
  certificate works. (That is also the security weakness, but it is what makes
  self-hosting possible.)

Note firmware 7.4.4407 has **no CLI command for TCLV 145** -- nothing matching
``tls_*`` appears in ``help``. It is a network-only parameter, which is the
opposite of the problem this module exists to work around.

Untested here: this library speaks plaintext and nothing has driven TCLV 145 on
hardware. The parameter's description and its absence from the read-only set are
both read out of the vendor's own tables.
'''
"""The mechanism to use instead of chasing the AES key."""
