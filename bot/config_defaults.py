"""Canonical defaults for ani.json's "settings" block.

Single source of truth shared by the bot (bot/anibot.py's loadconfig) and the
dashboard (web/app.py) so a freshly-seeded config and a hand-edited one that's
missing some optional keys behave identically on both sides. Every value here
mirrors what the code already falls back to elsewhere when a key is absent
(see the comment on each key below) -- seeding them changes nothing about how
the bot behaves, it just makes that fallback visible and edit-in-place
instead of failing loudly with "Fehlerhafte ani.json Konfiguration".

Stdlib only -- importable from both bot/ and web/ without pulling in
selenium/myjdapi/etc.
"""

# (value, label) pairs for the hoster select, in animeloads.py's own
# DDOWNLOAD/RAPIDGATOR numbering (bot/animeloads.py: DDOWNLOAD = 0,
# RAPIDGATOR = 1). Duplicated here (rather than imported from animeloads)
# because animeloads.py pulls in selenium at module scope -- this module
# must stay import-safe without it.
HOSTER_CHOICES = ((0, "ddownload"), (1, "rapidgator"))

DEFAULT_SETTINGS = {
    # README's documented example ani.json (see "Anime-Loads Bot Config")
    # uses hoster=1 (Rapidgator).
    "hoster": 1,
    # Firefox is the only browser bundled in the bot image (bot/Dockerfile
    # installs firefox-esr + geckodriver, no Chrome/chromedriver) -- also
    # README's documented default.
    "browserengine": 0,
    # "" lets animeloads._create_driver fall back to the browser's default
    # install location -- also downloadEpisode/downloadBatchCNL's own
    # default parameter value.
    "browserlocation": "",
    # Pushbullet notifications are opt-in.
    "pushbullet_apikey": "",
    # What anibot.startbot()'s own recheck fallback already uses whenever
    # timedelay isn't a positive int (see the "recheck = timedelay if
    # isinstance(timedelay, int) and timedelay > 0 else 600" lines), and what
    # editconfig's own prompt recommends ("Empfohlen: 600 Sekunden").
    "timedelay": 600,
    # Empty -- matches downloadEpisode/downloadBatchCNL's own default
    # parameter. A fresh install has no download backend configured yet;
    # anibot.loadconfig() requires the owner to set this OR myjd_user before
    # the bot can actually download anything.
    "jdhost": "",
    "myjd_user": "",
    # Never seed a secret value.
    "myjd_pw": "",
    "myjd_device": "",
    # The deprecated non-MyJDownloader proxy connection mode is off by
    # default -- matches downloadEpisode/downloadBatchCNL's own default
    # parameter.
    "jd_deprecated": False,
    # Matches downloadEpisode/downloadBatchCNL's own default parameter, so
    # behavior is identical whether the key is present or seeded.
    "jd_deprecatedport": "3128",
}


def default_ani_data():
    """A brand-new ani.json skeleton: full default settings, no watchlist.

    Returns a fresh dict every call -- callers are free to mutate the
    result without affecting DEFAULT_SETTINGS itself.
    """
    return {"settings": dict(DEFAULT_SETTINGS), "anime": []}


def fill_settings_defaults(settings):
    """Return (filled, missing).

    ``filled`` is a shallow copy of ``settings`` with every DEFAULT_SETTINGS
    key it doesn't already define added. ``missing`` lists exactly the key
    names that were absent -- a key present but set to an empty/falsy value
    (e.g. myjd_pw="") is NOT "missing", the owner (or a previous seed) set it
    deliberately and it must not be silently reported as such.
    """
    filled = dict(settings)
    missing = []
    for key, value in DEFAULT_SETTINGS.items():
        if key not in filled:
            filled[key] = value
            missing.append(key)
    return filled, missing
