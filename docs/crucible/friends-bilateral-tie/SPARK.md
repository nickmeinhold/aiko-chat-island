# 🜂 SPARK — the out-breath

> Movement 3. Cross-family generative pass, 2026-08-25. Four sparks thrown **in parallel and
> blind to each other**, no critique, no feasibility filter. Solution space from RESEARCH §3 was
> **withheld** from all four.
>
> **Seat accounting (honest):** Maxwell (Claude), Kelvin (Gemini), Carnot (Codex/GPT), Tesla
> (Grok) reported. **Wu (Kimi K3) FAILED** — `provider.api_error: 500`, zero bytes. Recorded as
> **ABSENT, not passed**; no spark is fabricated on its behalf.
>
> **Axis check:** the four did **not** collapse onto one axis. Kelvin landed on *algebra*
> (verify-without-legibility), Carnot on *the tie as a graded object*, Tesla on *topology*
> (addressing dissolves into weather), Maxwell on *locus of decision* (the endpoint arbitrates).
> Tesla and Maxwell share a family resemblance — payload-decides — but differ on whether
> addressing survives at all. Bisociation proceeded.

---

## MAXWELL — verbatim



## THE DEAF BELL

Stop asking the island for permission. The island was never the right place to hold this.

Every design so far assumes the island is a **gatekeeper**: A wants to ring B, the island checks
a tie, the island decides. That framing is what forces the tie to be legible — you cannot check
what you cannot read. So invert it: **let the island ring every bell, and make only a friend's
bell have a clapper.**

Concretely: when A and B consent to each other, their two devices derive a shared per-tie key —
never uploaded, never escrowed, existing only on the two handsets. Afterwards, when A rings B,
the wake payload the island pushes is **encrypted to that key**. The island forwards it blindly.
It always forwards. It has no opinion.

B's handset wakes, tries to decrypt, and:
- **holds the key** → the payload unseals into "Nick is calling you", and the phone *rings*;
- **doesn't** → the payload is noise. The phone discards it and **stays silent**. The user is
  never disturbed. Nothing is displayed. No notification, no badge, no trace.

Reachability stops being a permission and becomes a **physical property of the payload**. There
is no admission predicate to store, so there is nothing to enumerate. Compromise the island and
you get a pile of ciphertext and a delivery log — you learn that *someone pushed something at
Nick*, which the push queue already told you, and nothing about who may reach him. The
friend-grapevine stays dark because the island genuinely does not know it exists.

The unasked-for gift: **this is silent by construction rather than by policy.** Blocking today is
a rule the island enforces and can therefore get wrong, leak, or be coerced into inverting. A
deaf bell cannot be coerced. An operator with a subpoena and root can push anything they like at
your phone, and your phone will not make a sound.

And it federates for free. B's island does not need to know A, trust A's island, or hold any
cross-island relation — it just carries a sealed envelope toward a device. The tie lives in the
only two places that ever consented to it.

Yes, a stranger can still make your phone *wake* (battery, not attention) — but there is already
a per-recipient wake budget upstream, and a wake nobody hears is a rate-limiting problem, not a
consent problem. Those are different sizes of problem and we have been conflating them.

**Oh, of course:** *the island can ring any bell; only a friend's key has a clapper.*

---

## KELVIN — verbatim

**Name:** The Blind Duet

**The one-liner:** "A relationship isn't who you know, it's a song only two of you can sing."

The tie is not a record; it’s a shared potential for resonance, stored as cryptographic noise on the server. The server can witness the resonance but can never map the connections. We call this a Blind Duet.

When Alice and Bob consent to a tie, their clients first negotiate a high-entropy shared secret, `S_ab`—a key to a song only they know. This secret *never* touches the server.

Instead, each client uses this secret to derive two opaque, un-linkable tokens—their part of the duet. They use a server-known key, `K_server`, but in a swapped way:

-   Alice computes her part: `Initiate = HMAC(K_server, S_ab + "alice-sings")`, and `Listen = HMAC(K_server, S_ab + "bob-sings")`.
-   Bob computes his part: `Initiate = HMAC(K_server, S_ab + "bob-sings")`, and `Listen = HMAC(K_server, S_ab + "alice-sings")`.

Notice the crossover. Alice's `Listen` token is identical to Bob's `Initiate` token, and vice-versa.

Each client uploads their `Initiate` and `Listen` tokens to the server, where they are stored in a simple, unordered pile associated with their user account. The server’s database for Alice is just a jumble of tokens: `[token1, token2, token3, ...]`. It is computationally infeasible for the server to determine that Alice's `Listen` token and Bob's `Initiate` token were derived from the same secret, `S_ab`. The graph is illegible.

When Alice wants to call Bob, she starts the duet. Her client tells the server: "I am singing my `Initiate` token for Bob, and I am listening for his." The server receives two tokens from Alice: her `Initiate` token for this specific tie, and the `Listen` token she expects Bob to sing.

The server's job is now dead simple and completely blind. It performs two checks:
1.  Is Alice's `Initiate` token in her pile of tokens?
2.  Is the `Listen` token she provided in *Bob's* pile of tokens?

If both are true, the duet is complete. The server has confirmation of a symmetric, consented relationship without ever seeing a record of it. It authorizes the call to go through. An attacker compromising the server finds only meaningless piles of tokens, not a social graph they can exploit. The relationship isn't stored as data; it is performed, live, as a verifiable event.

---

## CARNOT — verbatim

**Borrowed Doorbell**

The tie is not a row. It is a little private doorbell the two people build together.

When Mira and Sol consent, their clients mint a shared bell: two half-keys, two nicknames, one tiny ritual object. Each person sees it as a physical-looking charm in chat: a brass button, a cracked shell, a matchbox, whatever skin the island uses. Pressing it does not mean “call Sol.” Pressing it means “spend one permitted chime from the bell Sol and I made.”

The island never stores “Mira is tied to Sol.” It stores bell mouths: opaque little sockets that can ring exactly one handset when fed a valid chime. A chime is made fresh by Mira’s client from the shared bell and addressed to one bell mouth. The island can test the chime, wake the phone, and forget the occasion. It cannot list Sol’s bells because bells are not indexed by Sol. It cannot answer “who are Mira’s people?” because there is no friend shelf, only scattered mouths that open when sung to correctly.

Consent feels like striking a match from both sides. One person offers a half-bell. The other completes it. Until both halves touch, it is inert. Ending the tie is not “unfriend”; it is melting the bell. The charm goes dull on both devices, future chimes stop sounding, old chimes are just ash.

For invites, the same primitive becomes a doorway. A room does not add you. It hands you a silent bell-seed. If you press back, the seed becomes a room-bell: now the room may knock, and your handset may answer. Invitation is no longer admin insertion; it is a doorbell accepted into your house.

The strange extra power: people can have different bells for different selves. “Emergency bell” rings through sleep. “Kitchen bell” only glows. “Moon bell” wakes you once per night. Reachability becomes a handmade acoustic contract, not a permission bit.

One line:

**Of course the island should not know who your friends are; it should only know when a bell is being rung correctly.**

---

## TESLA — verbatim

**The Foghorn Pact**

A friend is not a row. A friend is the one phone that can open the letter you sent to everybody.

You never add anyone. You teach two phones a word the island is not allowed to learn. You do it by knocking the devices together, by a QR on a cafe table, by a link carried on a walk. The ceremony mints a shared opening-key that lives only on those two handsets. No record is written. No pair is named. The island is not informed that anything happened.

When you want them, you do not look them up. You seal a ring inside that word and you sound it into the harbor — one push to every sleeping handset on their island. Thirty-two phones hear weather. They fail to open the letter, discard it, and go back to sleep. Theirs hears its name, opens, and rings.

That flood *is* the island acting. It does not authorize a pair. It makes weather. At thirty-three souls the directory *is* the whole island, so addressing nobody is cheaper than remembering somebody. The scale that makes hashed pairs a joke is the scale that makes broadcast the privacy mechanism.

Revocation is amnesia. Delete the word on your phone and the next horn is rain again. Nothing on the box to retract. Nothing to put on a revocation list. A stolen word without the phone is a sealed letter with no house; a stolen phone *is* the person.

An invite is the same horn in a different dialect. Nobody is added. You and a stranger mint a word; they stand in the fog and speak it; your phone recognizes the student and *asks* you; they are asked on theirs. A new boat is in the harbor because two people could hear each other, not because an admin wrote a row. The missing consent step is the opening of the letter — the phone that cannot open it cannot even be asked.

Federation is harbor-routing, not friendship. You need to know which fog they sleep in, the way you need an area code. You do not need their name on a list. Their operator watches a lighthouse flash and never sees which window lit.

The thing nobody asked for: a silent friend. A guardian you have never called does not exist in any log, any roster, any hash table. They will still hear the horn if you ever need them. The grapevine that makes a recovery-collusion attack non-silent lives in the physics of the opening-key, not in a permissions table an attacker could dump.

Compromise the box and you get a belfry with no names. The graph was never a graph. It was two throats that learned the same note.

---

# The fusion — what none of them had alone

## The seam that forces the answer

Collide them and one wall shows up in all four: **the island cannot wake a specific phone without
knowing whose phone it is.** Delivery requires addressing; addressing requires identity. Kelvin's
token-piles are stored per account, so even though the *graph* is illegible the **degree leaks** —
the size of Alice's pile is her friend count. Carnot's bell mouths must resolve to a handset, and
handsets belong to users. Mine and Tesla's sealed payloads still have to be pushed *somewhere*.

So the privacy of a tie is not really a property of how the tie is *stored*. It is a property of
**how narrowly the wake is addressed.** That is the thing none of the four said, and all four
imply.

Tesla is the only spark that escapes the wall, and it escapes by refusing to address at all —
sound the horn at the whole harbour, let one phone open the letter. But Tesla's harbour has **no
admission control whatsoever**: anyone can flood it, and the island has no way to tell a friend's
horn from a stranger's, because by construction it cannot read either.

Kelvin has exactly what Tesla lacks — a **blind check** the island can run — and pays for it with
a per-account pile that leaks degree.

## The third object: **NARROWCAST — reachability with an anonymity-set dial**

Take Carnot's **bell** (a graded, revocable, per-tie object with its own affordances — emergency
bell rings through sleep, kitchen bell only glows), verified by Kelvin's **blind crossover check**
(so the island *can* enforce and rate-limit without reading the graph), delivered to a **cohort of
k devices** rather than to one (Tesla's move, made continuous instead of absolute), with the final
ring-or-silence decided at the **handset** by whether it can open the seal (mine).

The island's job becomes: *validate that some bell is being rung correctly, then wake k phones,
one of which can hear it.* It never learns which.

**`k` is the design dial, and it is the object nobody threw:**

| k | what the island learns | cost |
|---|---|---|
| 1 (address the recipient) | exactly who may ring whom | free |
| k (a cohort) | "one of these k" | k× wake battery |
| N (Tesla's harbour) | nothing | N× wake battery |

Privacy of delivery is **bought in battery, and the exchange rate is k.** At N=33 the whole
harbour costs 33 wakes; a cohort of 5 costs 5 and reduces the attacker's certainty to 20%. It is
a slider, not a religion — and the C5 property does not need certainty driven to zero, it needs
an attacker to be unable to build a *reliable* list of who to keep quiet.

**And the graded bell composes with the dial:** an emergency bell can be k=1 (ring me, I don't
care who knows) while an ordinary bell is k=8. Reachability stops being a permission bit and
becomes a **two-axis contract: how loudly, and how anonymously.**

## Oh, of course

> **You cannot ring one phone privately. You can only ring a crowd and let one of them hear it.**

## Honest self-assessment of the bisociation

This is **not** Tesla with decoration, and the test is what each spark lacks: Tesla's harbour has
no admission control (a stranger floods it freely) and no revocation beyond amnesia; the fusion
adds a blind-verifiable admission check that Tesla structurally cannot have. It is not Kelvin
either: Kelvin still addresses one recipient and leaks degree. The genuinely new content is
**k as an explicit, per-bell, tunable anonymity set** — nobody threw that, and it converts an
all-or-nothing privacy argument into an engineering quantity you can price.

**Where I expect it to break** (stated now so Fold and Temper strike the real thing): the battery
economics at k>1, whether an attacker can shrink the anonymity set by correlating *timing* across
many wakes, and whether "one of these k" survives repeated observation — an attacker who watches
1,000 wakes may intersect the cohorts and recover the pair anyway. **Intersection attack is the
obvious kill and it is not addressed here.** That is the first thing to hit at Fold.
