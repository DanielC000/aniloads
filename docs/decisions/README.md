# Decision records

This folder holds decision/incident narrative extracted out of source comments, per the
comment taxonomy in the owner directive (board card `e6178ac4`): guard/prohibition comments
(≤3 lines) stay inline, API docs/docstrings stay as-is, comments that just restate the code
are deleted, and genuine decision/incident narrative moves here.

## Anchors

A moved comment leaves a short pointer at its original location:

```python
# @decision <id> — <the prohibition or consequence, not a summary>
```

`<id>` is either a board card id (when the original comment cited one) or `sha:<8hex>` — the
short hash of the commit that introduced the comment, per `git blame`. Loom's own tooling
surfaces a record's title and "Do not" section to an agent whenever it reads the anchored
line.

## File naming

One file per id: `<8hex>-<slug>.md` — the bare `<8hex>` (card id or commit sha), never the
`sha:` sigil, which lives only in the anchor comment. The resolver looks up a record by
`<8hex>-*.md`, so a filename carrying the `sha:` prefix would never be found. A second
decision filed under an id that already has a record becomes a new section in that file,
never a second file.

## Record shape

Each record has:
- A short title and the file(s)/function(s) it originally lived in.
- The narrative itself (why, not just what).
- A `## Do not` section stating the prohibition or consequence a future change must respect.
