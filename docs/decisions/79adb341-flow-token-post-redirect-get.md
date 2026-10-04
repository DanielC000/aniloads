# Add-flow steps stash their rendered HTML behind a flow token

`web/app.py` — the add-flow token store consulted around `/?flow=<token>`.

Add-flow steps (search results, release picker, TVDB step) are rendered by a POST.
Post/Redirect/Get: the POST stashes the rendered step here and redirects to
`/?flow=<token>`, so reloading re-renders the stashed step instead of re-submitting the form.
Read-only on GET; bounded and short-lived, in memory only.

## Do not

Don't render an add-flow step directly from a POST response — redirect to `/?flow=<token>`
instead (Post/Redirect/Get), or reloading the page re-submits the form. Keep the store
read-only on GET, bounded, and in-memory only (it isn't meant to survive a restart).
