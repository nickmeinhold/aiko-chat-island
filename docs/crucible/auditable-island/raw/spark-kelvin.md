### Receipt Weaving

This model provides auditability by chaining every island action into a public, unbreakable history, without relying on consensus or third-party observers.

An island's operator mints a large, finite set of single-use cryptographic "routing tickets." These tickets are derived from a secret key and a counter, forming a hash chain. To send a message, a client "spends" a ticket.

For every ticket spent, the island MUST publish a signed "receipt" to a public, append-only log it hosts. A receipt contains a hash of the ticket, the encrypted message payload, and the next-hop island's public key. Crucially, the island's routing logic is woven into this chain: the cryptographic seed for generating the *next* block of tickets for another client is derived from the hash of the *last* receipt issued.

This creates an immutable, verifiable sequence of operations. Any fork in the chain or missing receipt is a permanent, detectable scar. An operator cannot retroactively hide a malicious action (like dropping a message for which a ticket was spent) without breaking the chain for all subsequent operations. If they refuse to issue a receipt for a submitted ticket, the sending client has proof of non-service.

When a message travels from Island A to Island B, an auditor (which can be any user) can asynchronously verify the journey. They look for the sending receipt in Island A's public log and a corresponding receiving receipt in Island B's log. A missing receiving receipt proves Island A is a black hole, burning its reputation. A missing sending receipt proves Island A is censoring its own user.

The operator cannot equivocate. They can either behave according to the protocol and generate the public, chained proof of their actions, or visibly break the chain, creating cryptographic evidence of their own misbehavior. The system makes the island's own state its jailor.

OH OF COURSE: The island can't lie about what it did, because the proof of its last action is the key to its next action.
