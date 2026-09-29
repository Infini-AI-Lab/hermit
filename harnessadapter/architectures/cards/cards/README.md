# Cards

One file per card, named by its id (`C001.md`). See `_template.md` for the
format and `../cards_core/schema.py` for the rules. A card is served only when
it carries an `admitted:` field, which the controller writes after the card
passed verification. Learners propose cards through the host; they do not write
files here.
