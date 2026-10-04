# Quitting the redirect-anime driver after a single-result search

`bot/animeloads.py` — `animeloads.search()`, the single-redirect-result (`status_code ==
302`) branch.

`updateInfo()` keeps its driver alive for `downloadEpisode` reuse, but the `anime` object
built here for a single-result search is thrown away right after building the search result,
so its driver must be quit right here — otherwise the Firefox process leaks on every
single-result search.

## Do not

Don't drop the explicit `redir_anime._driver.quit()` call here — `updateInfo()` deliberately
keeps the driver alive for reuse, so without this cleanup every single-result search leaks a
Firefox process.
