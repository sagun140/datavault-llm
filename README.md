# datavault-llm

An experiment in answering questions **without a generative model that can invent things**.

Text goes into a [Data Vault 2.0](https://en.wikipedia.org/wiki/Data_vault_modeling) store
(hubs, links, satellites). Questions are answered by retrieving satellite rows and
rearranging their literal strings. Every answer carries the revision it came from, and
anything the vault doesn't hold gets a refusal instead of a guess.

```
$ dvl ask who founded anthropic
Anthropic founders: Dario Amodei; Daniela Amodei; Jared Kaplan; Jack Clark;
Chris Olah; Ben Mann; Sam McCandlish; Tom Brown. [1]

Sources (vault rows):
  [1] Anthropic / founders = Dario Amodei; Daniela Amodei; ...
      rev 1376293555 @ 2026-09-23T07:28:32Z  ·  enwiki:Anthropic@1376293555

$ dvl ask what is anthropic's stock price
No fact in the vault answers that, so I will not answer.
Closest slots the vault does hold: Anthropic/website, Anthropic/name, Anthropic/industry
```

## What is actually claimed

This is **not a trained model**, and training is not what would make it truthful.
Hallucination is a property of free-form generation, not of weights — so the guarantee
here comes from three gates in the pipeline, each of which is a test in `tests/`:

| Gate | Where | What it prevents |
|---|---|---|
| **Provenance gate** | `vault.load_revision` | A fact enters the vault only if its `source_span` appears **verbatim** in the source document. Extractor output that cites nothing is dropped and counted. |
| **Retrieval gate** | `query.ask` | A row is only surfaced on *predicate-side* evidence. Below threshold the system refuses and names the slots it does hold. |
| **Grounding gate** | `verify.assert_grounded` | Every content token in the answer must appear in a cited row. Unsupported tokens raise instead of printing. Only a fixed function-word list may be added by the composer. |

The third gate is the interesting one: it validates *whatever wrote the answer*. The
composer here is deterministic, so it passes trivially — but the same check is what
would let an LLM write the prose later without being able to smuggle in a fact.

It also distinguishes **"I don't have that"** from **"here are related facts"**. Asking for
a CTO the vault has never seen returns founders and key people *explicitly labelled as
not an answer*, rather than letting adjacent truths pose as one.

## The sentence creator: text -> vault -> the same text

A sentence is stored as two constructs, and rebuilt from them:

```
hub_pattern    the frame, its entity mentions replaced by slots
hub_entity     each filler, one hub per distinct entity
link_slot      which hub fills which position, and how it was written there
```

The pattern is **derived from the input, never authored**. Wikipedia already marks its
entities with `[[links]]` and bold markup, so the split between frame and filler is given
by the source rather than guessed at:

```
$ dvl reconstruct Anthropic -v

pattern: {0} (stylized as {1}) is an American {2} (AI) {3} headquartered in {4}, California.
slots  : ['Anthropic, PBC', 'ANTHROP\C', 'artificial intelligence',
          'public benefit corporation', 'San Francisco']
rebuilt: Anthropic, PBC (stylized as ANTHROP\C) is an American artificial intelligence
         (AI) public benefit corporation headquartered in San Francisco, California.

157/157 sentences rebuilt exactly (100.0%)
```

`construct()` reads `hub_pattern` and `link_slot` only -- never
`sat_utterance.source_span` -- so matching that column is a verification, not a copy.
A sentence is admitted to the vault **only if `construct(decompose(s)) == s` exactly**, so
the vault can regenerate every sentence it holds, character for character. What it cannot
rebuild, it refuses to store.

Two things this forced into the open:

- **A mention is not an entity.** `artificial intelligence` and `Artificial intelligence`
  are one hub but two surface forms, so the surface belongs on `link_slot` (the mention),
  not on the hub (the identity). Getting this wrong cost 3 of 158 sentences.
- **Utterance identity must survive edits.** Keying on (document, pattern) collided when
  one document used a frame twice; it now includes the occurrence index, which stays
  stable when unrelated sentences change.

## Sentences are walked out of the graph

A link is already a proposition: `hub(subject) -> predicate -> satellite(value)` maps
onto subject -> verb -> object. So sentences are not looked up and formatted, they are
*realized from the vault topology*. Several links out of one hub fuse into one clause:

```
$ dvl describe Anthropic
Anthropic is an American artificial intelligence (AI) public benefit corporation
headquartered in San Francisco, California. [1] Its type is Public benefit corporation
and Private. [2] It was founded on 2021-01-26 by Dario Amodei, Daniela Amodei, Jared
Kaplan, Jack Clark, Chris Olah, Ben Mann, Sam McCandlish, and Tom Brown. [4] It is
located in 500 Howard Street and San Francisco, California, U.S. and its industry is
Artificial intelligence. [6] Its products are Claude, Claude Code, Claude Cowork, and
Bun. [7] ...
```

`founded` + `founders` are two links on one hub; the founding *frame* fuses them into a
single clause with two objects. Lists are coordinated (`;` -> `, and`), and the subject
is pronominalized after first mention. All of it is closed-class machinery.

### Why nothing can be invented

The claim rests on one invariant, enforced over every template by
`tests/test_grammar.py`:

> Every word in a clause template is either **(a)** a closed-class function word in
> `verify.SCAFFOLD` — determiners, copulas, prepositions, conjunctions, pronouns — or
> **(b)** a morphological variant of the predicate's own business key.

Content words therefore have exactly two doors into a sentence:
`hub_entity.business_key` and `sat_statement.value`. Adding a template that smuggles in a
content word fails the build — which is how the `birth_date` template was caught trying
to use "born", a word derivable from no vault key.

Whatever a template does contribute under (b) is reported with the citation as that
predicate's `lexicalization`, so every word that did not come from a hub or a satellite
is auditable.

## Why Data Vault, specifically

Because the requirement was "if the info updates, start tracking that too" — and that is
what a type-2 satellite *is*. Values live in satellites, not in links, so a changed value
end-dates one row and opens another. Updates become queryable events instead of silent
overwrites:

```
$ dvl history
Anthropic / founders
  was : Daniela Amodei; Dario Amodei                    (rev 1156815581, from 2023-05-24)
  now : Dario Amodei; Daniela Amodei; Jared Kaplan; ...  (rev 1376293555, since 2026-09-23)
Anthropic / products
  was : Claude                                          (rev 1156815581, from 2023-05-24)
  now : Claude; Claude Code; Claude Cowork; Bun          (rev 1376293555, since 2026-09-23)
```

Loads are idempotent (same content twice = one satellite row) and order-safe (an older
revision loaded late never overwrites newer history).

## Run it

Python 3.10+, no dependencies.

```bash
git clone https://github.com/sagun140/datavault-llm && cd datavault-llm

# load two points in time, so there is history to track
python3 -m dvl.cli ingest "Anthropic" --asof 2023-06-01T00:00:00Z --asof 2026-09-25T00:00:00Z

python3 -m dvl.cli ask who founded anthropic
python3 -m dvl.cli ask what changed about anthropic
python3 -m dvl.cli history
python3 -m dvl.cli stats

python3 -m pytest tests/ -q
```

`ingest` works on any English Wikipedia page; extraction covers the infobox and the lead
definition sentence.

## Schema

```
hub_entity      entity_hk, business_key                     -- Anthropic, Dario Amodei, ...
hub_predicate   predicate_hk, business_key                  -- founded, num_employees, ...
link_statement  statement_hk -> (subject_hk, predicate_hk)  -- the claim slot
sat_statement   value, source_span, revision_id,            -- type-2 history of the slot
                load_ts, load_end_ts, hash_diff
sat_entity      title, url, revision_id, ...                -- document-level metadata
```

One deliberate deviation from textbook Data Vault: the object of a statement lives in the
satellite rather than in the link. A pure link-per-(subject, predicate, object) triple
would make every value change a *new link*, which loses exactly the change history this
experiment is about. The linked entity is still hubbed and referenced from the satellite,
so the graph is intact.

## Honest limitations

- **39% of sentences round-trip vacuously.** 60 of 155 contain no wikilink, so they
  become a zero-slot pattern: the whole sentence stored as one frozen string. Those are
  memorized, not decomposed, and the vault gains no structure from them. Decomposition
  depends entirely on the source's own markup; unlinked prose yields nothing.
- **Facts are still infobox + lead sentence only.** Sentence storage now covers the whole
  page, but the fact extractor that `ask` queries does not, so most body prose is
  reconstructable without being answerable.
- **Retrieval is lexical**, with a hand-written synonym table. It has no notion of
  paraphrase; an unmapped question word simply fails to retrieve, and it refuses. That
  fails safe, but it fails often.
- **The realizer is template-driven, not learned.** Clause patterns are hand-authored per
  predicate (17 of them). A predicate with no template produces no sentence rather than a
  guess, so coverage of the grammar bounds coverage of the answers.
  An LLM composer behind `assert_grounded` remains the intended next step for fluency.
- **Fidelity is only as good as the source.** The vault guarantees a claim traces to a
  Wikipedia revision. It does not make Wikipedia correct.
- **`object_entity_hk` is unreliable.** It records the *first* wikilink in a value, which
  is often not the value's referent: `revenue` links to `US$`, `key_people` to
  `Chief executive officer` rather than to Dario Amodei. The answer path reads `value`
  and never this column, so answers are unaffected — but the graph edges are wrong, and
  they pollute `hub_entity` with non-entities. The real fix is a multi-valued
  `link_reference` table rather than one arbitrary link per statement.
- **Multi-value fields are single strings** (`"Dario Amodei; Daniela Amodei"`), not
  separate facts, so you cannot yet ask about one founder.

## License

MIT
