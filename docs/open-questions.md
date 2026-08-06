# Open questions

Unresolved items. The first one gates the build; the second is the real bulk of
the work.

## Data boundary

Client-account meetings mean audio and screenshots leave the network. Confirm
what is allowed **before** wiring this to an external API.

An internal monthly review is a different conversation from a client call, and
the answer may differ per meeting type rather than being a single yes/no.

## Knowledge base

The plumbing here is a weekend. Getting past meeting notes, account docs, and
pricing into a store that returns the right chunk is the actual project.

Worth scoping separately from the latency work — they're independent, and the
demo path uses a static table instead.

## Glossary

Feed client names, product names, and internal acronyms to the transcription
model as keyword hints.

Proper nouns are what these models mangle, and they are exactly the vocabulary
this bot needs correct.
