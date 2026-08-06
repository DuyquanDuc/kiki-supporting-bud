# Screen capture handling

Feeds the screen loop described in [architecture.md](architecture.md).

## Capture at button-down, not button-up

Presenters advance slides while you are still asking. The frame you want is the
one that was on screen when you decided to ask.

## Crop to the shared-content region

Have the user drag a box once on first run and reuse it. Meeting layouts do not
move.

## Detail level matters

Big slide headlines survive `low`. Tables and code need `high` and cost real
tokens. Pick per capture rather than globally if the screen loop can tell the
difference.

## Pre-index the deck where possible

Better than OCR: index the deck ahead of time, use the screenshot only to
identify *which slide you are on*, then feed the model the real text of that
slide.

This trades a vision problem for a lookup, and the lookup is both cheaper and
more accurate — especially on the tables and small numbers that OCR mangles.
