import logging
import subprocess, sys, json, time, os, re, random
from logging.handlers import TimedRotatingFileHandler

from getpass import getpass

from datetime import datetime, date, timedelta

LOG_DIR = os.environ.get("LOG_DIR", "/config/logs")
LOG_FILE = os.path.join(LOG_DIR, "anibot.log")
LOGLEVEL = getattr(logging, os.environ.get("LOGLEVEL", "INFO").upper(), logging.INFO)

# Days before a skip_until date to start scraping anyway. The bot defers
# scraping a "Continuing" series until skip_until (the TVDB-predicted airdate),
# but anime-loads.org sometimes publishes an episode early. Once today is within
# this many days of skip_until, the bot scrapes anyway to catch the early
# release. 0 disables (strict skip_until honoring). See should_scrape_despite_skip().
EARLY_SCRAPE_DAYS = int(os.environ.get("EARLY_SCRAPE_DAYS", "1"))

_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(logging.Formatter("%(message)s"))
_handlers = [_stdout_handler]

_file_log_error = None
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        os.chmod(LOG_DIR, 0o777)
    except OSError:
        pass
    _file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=14, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _handlers.append(_file_handler)
    try:
        os.chmod(LOG_FILE, 0o666)
    except OSError:
        pass
except OSError as e:
    _file_log_error = e

logging.basicConfig(level=LOGLEVEL, handlers=_handlers)
_log = logging.getLogger("anibot")
if _file_log_error:
    _log.warning("File logging disabled: %s", _file_log_error)

try:
    from pushbullet import Pushbullet
except ImportError:
    Pushbullet = None

from tvdb import TVDBClient

import animeloads as animeloads_module

from animeloads import animeloads, ALLinkExtractionException

arglen = len(sys.argv)

import myjdapi

pb = ""

botfile = "config/ani.json"
botfolder = "config/"

def is_docker():
  if not os.path.isfile("/proc/" + str(os.getpid()) + "/cgroup"): return False
  with open("/proc/" + str(os.getpid()) + "/cgroup") as f:
    for line in f:
      if re.match(r"\d+:[\w=]+:/docker(-[ce]e)?/\w+", line):
        return True
    return False

def log(message, pushbullet):
    try:
        pushbullet.push_note("anibot", message)
    except:
        pass
    _log.info(message)

def compare(inputstring, validlist):
    for v in validlist:
        if(v.lower() in inputstring.lower()):
            return True
    return False

def printException(e):
    exc_type, exc_obj, exc_tb = sys.exc_info()
    fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
    _log.error("Error: %s %s %s", exc_type, fname, exc_tb.tb_lineno)

def should_scrape_despite_skip(skip_until_str, today, window_days):
    """Decide whether to scrape now even though a skip_until date is set.

    The bot writes skip_until = <TVDB-predicted airdate> for a "Continuing"
    series and skips scraping until then. But anime-loads.org sometimes
    publishes an episode before its TVDB airdate, which the strict skip would
    miss. So once `today` is within `window_days` of skip_until — i.e. it's the
    airdate-eve or later — scrape anyway to catch an early release, while still
    skipping when skip_until is well in the future (preserving rate-limit
    protection).

    Returns True (scrape) when today >= skip_until - window_days, when there is
    no skip_until, or when skip_until is unparseable (a bad date must not
    silently suppress scraping). Returns False (honor the skip) otherwise.
    """
    if not skip_until_str:
        return True
    try:
        skip_date = date.fromisoformat(skip_until_str)
    except (ValueError, TypeError):
        return True
    return today >= skip_date - timedelta(days=window_days)

def _boot_backoff(attempt, cap=300):
    """Capped exponential backoff (seconds) for in-process boot retries.

    Keeps the container alive and self-healing on a transient boot failure
    instead of exiting and relying on Docker's restart backoff. Under
    `restart: unless-stopped` a crash-loop accumulates an exponential delay
    that can leave the container "not running" for long stretches — which is
    exactly the "the bot didn't start automatically" symptom. Retrying inside
    the process keeps the container up and the dashboard's status/controls live.

    Sequence: 5, 10, 20, 40, 80, 160, 300, 300, ... seconds (capped)."""
    return min(cap, 5 * (2 ** min(attempt, 6)))

# Persisted run-state record. The dashboard derives last_run / next_run from
# this file instead of scraping the rolling container-log tail (the German run
# markers "Prüfe …"/"Schlafe N Sekunden" roll off the 500-line window under
# verbose logging, which made the UI show "No runs yet" / "—" while the bot was
# running fine). Written next to ani.json so the bot and the dashboard share one
# config dir. The `runs` history is bounded and holds one summary per cycle, so
# a future run-history UI can render one summary per run from it.
RUN_STATE_FILE = "run_state.json"
RUN_STATE_HISTORY_MAX = 50
EVENTS_CAP = 40

def _utcnow_iso():
    """UTC timestamp as RFC3339-ish ISO8601 with a trailing Z. Matches the
    dashboard's UTC clock (datetime.utcnow) so its next-run math stays correct."""
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _run_state_path():
    """Path to run_state.json, alongside the watchlist (ani.json). Derived from
    botfile so a --configfile override keeps the record next to the config."""
    return os.path.join(os.path.dirname(botfile) or ".", RUN_STATE_FILE)

def _record_event(events, kind, anime, episodes=None, detail=None):
    """Append one bounded run-state event to `events` (mutated in place).

    Kept deliberately simple (no try/except) — event collection runs inline in
    the hot scrape loop and must never introduce an exception path into a
    cycle. `detail` is truncated to 200 chars; callers must never pass a URL,
    credential, or JD host/port (run_state.json is dashboard-visible)."""
    event = {"kind": kind, "anime": anime}
    if episodes:
        event["episodes"] = list(episodes)
    if detail:
        event["detail"] = str(detail)[:200]
    events.append(event)

def write_run_state(started_ts, finished_ts, timedelay, counts, events=None):
    """Persist one per-cycle run-state record and append it to a bounded history.

    Best-effort: a write failure must never break the bot loop, so all errors
    are swallowed. Written atomically (tmp + os.replace) so the dashboard never
    reads a half-written file. `counts` is a free-form dict (entries/checked/
    downloaded/errors/skipped/unavailable today). `events` is an ordered list
    of what actually happened this cycle (download/error/unavailable/complete),
    capped at EVENTS_CAP entries — the first EVENTS_CAP are kept and
    `events_truncated` is set when the cap clipped the list; `counts` stays
    authoritative regardless of clipping."""
    try:
        next_run_ts = ""
        if isinstance(timedelay, int) and timedelay > 0:
            try:
                fin = datetime.strptime(finished_ts, "%Y-%m-%dT%H:%M:%SZ")
                next_run_ts = (fin + timedelta(seconds=timedelay)).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError):
                next_run_ts = ""
        events = events or []
        record = {
            "started_ts": started_ts,
            "finished_ts": finished_ts,
            "timedelay": timedelay,
            "next_run_ts": next_run_ts,
            "counts": counts,
            "events": events[:EVENTS_CAP],
        }
        if len(events) > EVENTS_CAP:
            record["events_truncated"] = True
        path = _run_state_path()
        try:
            with open(path, "r") as f:
                prev = json.load(f)
            runs = prev.get("runs")
            if not isinstance(runs, list):
                runs = []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            runs = []
        runs.append(record)
        if len(runs) > RUN_STATE_HISTORY_MAX:
            runs = runs[-RUN_STATE_HISTORY_MAX:]
        state = {"schema": 1, "last_run": record, "runs": runs}
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Run-state bookkeeping is never allowed to take down the bot loop.
        pass

def loadconfig():
    try:
        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        infile = open(botfile, "r")
        data = json.load(infile)
        infile.close()
    except Exception as e:
        printException(e)
        print("ani.json nicht gefunden, ")
        return False, False, False, False, False, False, False, False, False, False, False, False, False
    # Sentinel defaults: if the file has no "settings" block at all, fall through
    # to a clean False-tuple (→ caller treats it as "no/bad config" and retries)
    # instead of raising UnboundLocalError on the return below.
    jdhost = hoster = browser = browserlocation = pushkey = timedelay = False
    myjd_user = myjd_pass = myjd_device = jd_deprecated = jd_deprecatedport = False
    al_user = al_pass = False
    for key in data:
        if(key == "settings"):
            try:
                value = data[key]
                jdhost = value['jdhost']
                hoster = value['hoster']
                browser = value['browserengine']
                browserlocation = value['browserlocation']
                pushkey = value['pushbullet_apikey']
                timedelay = value['timedelay']
                myjd_user = value['myjd_user']
                myjd_pass = value['myjd_pw']
                myjd_device = value['myjd_device']
                jd_deprecated = value['jd_deprecated']
                jd_deprecatedport = value['jd_deprecatedport']
            except Exception as e:
                printException(e)
                print("Fehlerhafte ani.json Konfiguration")
                # 13-tuple to match the caller's unpack — a short tuple here would
                # raise ValueError at the call site and crash the process.
                return False, False, False, False, False, False, False, False, False, False, False, False, False
            # anime-loads.org login: prefer the environment (AL_USER/AL_PASS from
            # .env), fall back to ani.json settings for backward compatibility.
            al_user = os.environ.get('AL_USER') or value.get('al_user')
            al_pass = os.environ.get('AL_PASS') or value.get('al_pass')
    return jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device,jd_deprecated,jd_deprecatedport, al_user, al_pass

def editconfig():
    try:
        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        infile = open(botfile, "r")
        data = json.load(infile)
        infile.close()
        for key in data:
            if(key == "settings"):
                value = data[key]
                jdhost = value['jdhost']       
                hoster = value['hoster']
                browser = value['browserengine']
                browserlocation = value['browserlocation']
                pushkey = value['pushbullet_apikey']
                timedelay = value['timedelay']
                myjd_user = value['myjd_user']
                myjd_pass = value['myjd_pw']
                myjd_device = value['myjd_device']
                jd_deprecated = value['jd_deprecated']
                jd_deprecatedport = value['jd_deprecatedport']
    except:
        jdhost = ""
        hoster = ""
        browser = ""
        browserlocation = ""
        pushkey = ""
        timedelay = ""
        myjd_user = ""
        myjd_pw = ""
        myjd_device = ""
        jd_deprecated = ""
        jd_deprecatedport = ""

    if(hoster == 1):
        hosterstr = "rapidgator"
    elif(hoster == 0):
        hosterstr = "ddownload"
    changehoster  = True
    if(hoster != ""):
        if(compare(input("Dein gewählter hoster: " + hosterstr + ", möchtest du ihn wechseln? [J/N]: "), {"j", "ja", "yes", "y"}) == False):
            changehoster = False
    if(changehoster):
        while(True):
            host = input("Welchen hoster bevorzugst du? rapidgator oder ddownload: ")
            if("ddownload" in host):
                hoster = animeloads.DDOWNLOAD
                break
            elif("rapidgator" in host):
                hoster = animeloads.RAPIDGATOR
                break
            else:
                print("Bitte gib entweder rapidgator oder ddowwnload ein")

    change_jdhost = True


    jd_device = ""
    jd_user = ""
    jd_pass = ""

    jd_choice = input("Läuft Jdownloader auf deinem lokalen Rechner[1] oder möchtest du MyJDownloader nutzen[2]?  (1 oder 2): ")
    if(jd_choice == "1"):
        if(jdhost != ""):
            if(compare(input("Deine Adresse des Computers, auf dem JDownloader läuft lautet: " + jdhost + ", möchtest du ihn wechseln? [J/N]: "), {"j", "ja", "yes", "y"}) == False):
                change_jhdhost = False
      
        if(change_jdhost):
            if(input("Läuft dein JD2 auf deinem Lokalen Computer? Dann Eingabe leer lassen und bestätigen, falls nicht, gib die Adresse des Zeilrechners an: ") != ""):
                jdhost = input
            else:
                jdhost = "127.0.0.1"
        jd_device = ""
        jd_pass = ""
        jd_user = ""
    
    else:
        jd_choice = 2
        jd=myjdapi.Myjdapi()
        jd.set_app_key("animeloads")
        
        logincorrect = False
        while(logincorrect == False):
            jd_user = input("MyJdownloader Nutzername: ")
            jd_pass = getpass("MyJdownloader Passwort: ")
            
            try:
              jd.connect(jd_user, jd_pass)
              logincorrect = True
            except:
                print("Fehlerhafte Logindaten")
        
        print("Logindaten sind korrekt")
        jd.update_devices()
        devices = jd.list_devices() 
        
        print("Deine verbundenen Geräte: ")
        for dev in devices:
            print(dev['name'])
        
        foundDevice = False
        while(foundDevice == False):
            jd_device = input("Gib den Namen des Gerätes, welches du benutzen willst ein: ")
            for dev in devices:
                devname = dev['name']
                if(jd_device == devname):
                    foundDevice = True
                    break
            if(foundDevice == False):
                print("Gerät nicht gefunden...")
        
        print("Nutze Gerät: " + jd_device)
        
        if(compare(input("Möchtest du das MyJDownloader passwort speichern (unverschlüsselt!!!)? Andernfalls musst du es jeden Programmstart eingeben [J/N]: "), {"j", "ja", "yes", "y"}) == False):
            jd_pass = ""

        jdhost = ""

    if(browser == 0):
        browserstring = "Firefox"
    elif(browser == 1):
        browserstring = "Chrome"

    if("--docker" in sys.argv):
        browser = animeloads.FIREFOX
        print("Überspringe Browserwahl, da in Docker")
    else:
        changebrowser = True
        if(browser != ""):
            if(compare(input("Dein gewählter Browser: " + browserstring + ", möchtest du ihn wechseln? [J/N]: "), {"j", "ja", "yes", "y"}) == False):
                changebrowser = False
        if(changebrowser):
            while(True):
                browser = input("Welchen Browser möchtest du nutzen? Darunter fallen auch forks der jeweiligen Browser (Chrome/Firefox)? Achte darauf, dass Chromedriver (Chrome) oder Geckodriver (Firefox) im gleichen Ordern wie das Script liegt: ")
                if(browser == "Chrome"):
                    browser = animeloads.CHROME
                    break
                elif(browser == "Firefox"):
                    browser = animeloads.FIREFOX
                    break
                else:
                    print("Fehlerhafter Input, entweder Chrome oder Firefox")
                    
            if(compare(input("Ist dein Browser ein fork von chrome/firefox oder an einem anderen als dem standardpfad installiert? [J/N]: "), {"j", "ja", "yes", "y"})):
                browserloc = input("Dann gib jetzt den Pfad der Browserdatei an (inklusive Endung): ")


    change_pushbullet = True

    if(pushkey != ""):
        if(compare(input("Dein Pushbullet API-Key ist: " +  pushkey + ", möchtest du ihn wechseln? [J/N]: "), {"j", "ja", "yes", "y"}) == False):
            change_pushbullet == False
    
    if(change_pushbullet):
        print("Hier kannst du deinen Pushbullet Account verbinden, damit du benachrichtigt wirst, wenn neue Folgen verfügbar sind und runtergeladen werden")
        if(compare(input("Möchtest du Pushbullet verwenden? [J/N]: "), {"j", "ja", "yes", "y"})):
            pushkey = input("Dann gib hier deinen Access Token ein (https://www.pushbullet.com/#settings): ")
        else:
            pushkey = ""

    change_timedelay = True
    if(timedelay != ""):
        if(compare(input("Deine Pause zwischen den Episodenupdates ist: " +  str(timedelay) + ", möchtest du sie ändern? [J/N]: "), {"j", "ja", "yes", "y"}) == False):
            change_timedelay = False

    if(change_timedelay):
        while(True):
            print("Hier kannst du deine Zeit, die zwischen der Suche nach neuen Episoden gewartet wird, einstellen.")
            timedelay_str = input("Wielange möchtest du warten? (In Sekunden. Empfohlen: 600 Sekunden (10 minuten)): ")
            try:
                timedelay = int(timedelay_str)
                break
            except:
                print("Bitte gib eine korrekte Zahl ein")

    settingsdata = {
        "hoster": hoster,
        "browserengine": browser,
        "pushbullet_apikey": pushkey,
        "browserlocation": browserlocation,
        "jdhost": jdhost,
        "timedelay": timedelay,
        "myjd_user": jd_user,
        "myjd_pw": jd_pass,
        "myjd_device": jd_device,
        "jd_deprecated": jd_deprecated,
        "jd_deprecatedport" : jd_deprecatedport
    }

    ani_exists = True

    try:
        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        f = open(botfile, "r")
        data = json.load(f)
        infile.close()
    except:
        ani_exists = False

    if(ani_exists):
        data['settings'] = settingsdata
        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        jfile = open(botfile, "w")
        jfile.write(json.dumps(data, indent=4, sort_keys=True))
        jfile.flush()
        jfile.close
    else:
        settingsdata = {"settings": settingsdata}
        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        jfile = open(botfile, "w")
        jfile.write(json.dumps(settingsdata, indent=4, sort_keys=True))
        jfile.flush()
        jfile.close

def addAnime():
    jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device,jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()
 
    while(jdhost == False):
        print("Noch keine oder Fehlerhafte konfiguration, leite weiter zu Einstellungen")
        editconfig()
        jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, al_user, al_pass = loadconfig()

    al = animeloads(browser=browser, browserloc=browserlocation)
    exit = False
    search = False

    while(exit == False):
        search = False
        print("Gib nun entweder eine URL zu einem Anime-Eintrag oder einen Namen, nach dem du suchen willst ein")
        aniquery = input("URL/Anime (Du kannst jederzeit \"suche\" eingeben, um zurück zur Suche zu kommen oder \"exit\", um das Programm zu beenden): ")
        if(aniquery == "exit"):
            break
        if("https://www.anime-loads.org/media/" in aniquery):
            print("Hole Anime von URL: " + aniquery)
            anime = al.getAnime(aniquery)

            releases = anime.getReleases()
        
            print("\n\nReleases:\n")
        
            for rel in releases:
                print(rel.tostring())
    
            print("\n")
            relchoice = ""
            while(True):
                relchoice = input("Wähle eine Release ID: ")
                if(relchoice == "exit"):
                    exit = True
                    break
                elif(relchoice == "suche"):
                    search = True
                    break
                try:
                    relchoice = int(relchoice)
                    if(relchoice <= len(releases)):
                        break
                    else:
                        raise Exception()
                except:
                    print("Fehlerhafte Eingabe, versuche erneut")
    
            if(search or exit):
                continue

            release = releases[relchoice-1]
            print("Du hast folgendes Release gewählt: " + str(release.tostring()))
    
            print("\n")

            print("Das Release hat " + str(release.getEpisodeCount()) + " Episode(n)")
            curEpisodes = -1
            while(curEpisodes == -1):
                epi_in = input("Wieviel Episoden hast du bereits runtergeladen? Die restlichen verfügbaren werden dann automatisch heruntergeladen (Leerlassen, wenn nur neue Episoden runterladen willst): ")
                if(epi_in == "exit"):
                    exit = True
                    break
                elif(epi_in == "suche"):
                    search = True
                    break
                try:
                    if(epi_in == ""):
                        curEpisodes = release.getEpisodeCount()
                    else:
                        epi_in_int = int(epi_in)
                        if(epi_in_int > release.getEpisodeCount()):
                            print("Deine Episodenzahl darf nicht größer als verfügbare Episoden sein")
                        else:
                            curEpisodes = epi_in_int
                except:
                    print("Fehlerhafte Eingabe, muss eine Zahl sein")

            print("\n")

            customPackage = ""
            if(compare(input("Möchtest du dem Anime einen spezifischen Paketnamen geben? Andernfalls wird der Name des Anime genutzt [J/N]: "), {"j", "ja", "yes", "y"}) == True):
                customPackage = input("Packagename: ")

            destinationFolder = ""
            if jd_deprecated:
                if (compare(input("Möchtest du dem Anime an einen bestimmten Ort speichern? (z.B. \"C://anime/s2\" ) [J/N]: "), {"j", "ja", "yes", "y"}) == True):
                    destinationFolder = input("Pfad: ")

            animedata = {
                "name": anime.getName(),
                "missing": [],
                "releaseID": relchoice,
                "episodes": curEpisodes,
                "url": anime.getURL(),
                "customPackage": customPackage,
                "destinationFolder": destinationFolder
            }
        
            os.makedirs(os.path.dirname(botfolder), exist_ok=True)
            f = open(botfile, "r")
            data = json.load(f)
            f.close()

            haveAddedAnime = False

            try:
                anidata = data['anime']
            except:
                print("Erster Anime in Liste, füge hinzu")
                fullanimedata = []
                fullanimedata.append(animedata)
                data['anime'] = fullanimedata 
                haveAddedAnime = True
                os.makedirs(os.path.dirname(botfolder), exist_ok=True)
                jfile = open(botfile, "w")
                jfile.write(json.dumps(data, indent=4, sort_keys=True))
                jfile.flush()
                jfile.close()
                print("Anime wurde hinzugefügt")

            if(haveAddedAnime == False):              #Füge zu liste hinzu
                isNewAnime = True
                for animeentry in anidata:
                    url = animeentry['url']
                    release = animeentry['releaseID']
                    if(url == anime.getURL() and release == relchoice):
                        print("Anime mit gleichem Release ist bereits in Liste, gehe zurück zur Suche")
                        isNewAnime = False
                if(isNewAnime):
                    print("Füge Anime zu liste hinzu")
                    fullanimedata = data['anime']
                    fullanimedata.append(animedata)
                    data['anime'] = fullanimedata 
#                animedata = {"anime": animedata}
#                data.append(animedata)


                    os.makedirs(os.path.dirname(botfolder), exist_ok=True)
                    jfile = open(botfile, "w")
                    jfile.write(json.dumps(data, indent=4, sort_keys=True))
                    jfile.flush()
                    jfile.close()
                    print("Anime wurde hinzugefügt")

            print("\n\n\n")

        elif(aniquery != "suche"):
            results = al.search(aniquery)
        
            if(len(results) == 0):
                print("Keine Ergebnisse")
                search = True
                break

            print("Ergebnisse: ")
    
            for idx, result in enumerate(results):
                print("[" + str(idx + 1) + "] " + result.tostring())
    
            while(True):
                anichoice = input("Wähle einen Anime (Zahl links daneben eingeben): ")
                if(anichoice == "exit"):
                    exit = True
                    break
                elif(anichoice == "suche"):
                    search = True
                    break
                try:
                    anichoice = int(anichoice)
                    anime = results[anichoice - 1].getAnime()
                    break
                except:
                    print("Fehlerhafte eingabe, versuche erneut")
    
            if(search or exit):
                continue

            releases = anime.getReleases()
        
            print("\n\nReleases:\n")
        
            for rel in releases:
                print(rel.tostring())
    
            print("\n")
            relchoice = ""
            while(True):
                relchoice = input("Wähle eine Release ID: ")
                if(relchoice == "exit"):
                    exit = True
                    break
                elif(relchoice == "suche"):
                    search = True
                    break
                try:
                    relchoice = int(relchoice)
                    if(relchoice <= len(releases)):
                        break
                    else:
                        raise Exception()
                except:
                    print("Fehlerhafte Eingabe, versuche erneut")
    
            if(search or exit):
                continue

            release = releases[relchoice-1]
            print("Du hast folgendes Release gewählt: " + str(release.tostring()))
    
            print("\n")

            print("Das Release hat " + str(release.getEpisodeCount()) + " Episode(n)")
            curEpisodes = -1
            while(curEpisodes == -1):
                epi_in = input("Wieviel Episoden hast du bereits runtergeladen? Die restlichen verfügbaren werden dann automatisch heruntergeladen (Leerlassen, wenn nur neue Episoden runterladen willst): ")
                if(epi_in == "exit"):
                    exit = True
                    break
                elif(epi_in == "suche"):
                    search = True
                    break
                try:
                    if(epi_in == ""):
                        curEpisodes = release.getEpisodeCount()
                    else:
                        epi_in_int = int(epi_in)
                        if(epi_in_int > release.getEpisodeCount()):
                            print("Deine Episodenzahl darf nicht größer als verfügbare Episoden sein")
                        else:
                            curEpisodes = epi_in_int
                except:
                    print("Fehlerhafte Eingabe, muss eine Zahl sein")

            print("\n")

            customPackage = ""

            if(compare(input("Möchtest du dem Anime einen spezifischen Paketnamen geben? Andernfalls wird der Name des Anime genutzt [J/N]: "), {"j", "ja", "yes", "y"}) == True):
                customPackage = input("Packagename: ")

            animedata = {
                "name": anime.getName(),
                "missing": [],
                "releaseID": relchoice,
                "episodes": curEpisodes,
                "url": anime.getURL(),
                "customPackage": customPackage
            }

            if jd_deprecated:
                destinationFolder = ""
                if (compare(input("Möchtest du dem Anime an einen bestimmten Ort speichern? (z.B. \"C://anime/s2\" ) [J/N]: "), {"j", "ja", "yes", "y"}) == True):
                    destinationFolder = input("Pfad: ")

                animedata = {
                    "name": anime.getName(),
                    "missing": [],
                    "releaseID": relchoice,
                    "episodes": curEpisodes,
                    "url": anime.getURL(),
                    "customPackage": customPackage,
                    "destinationFolder": destinationFolder
                }

            os.makedirs(os.path.dirname(botfolder), exist_ok=True)
            f = open(botfile, "r")
            data = json.load(f)
            f.close()

            haveAddedAnime = False

            try:
                anidata = data['anime']
            except:
                print("Erster Anime in Liste, füge hinzu")
                fullanimedata = []
                fullanimedata.append(animedata)
                data['anime'] = fullanimedata 
                haveAddedAnime = True
                os.makedirs(os.path.dirname(botfolder), exist_ok=True)
                jfile = open(botfile, "w")
                jfile.write(json.dumps(data, indent=4, sort_keys=True))
                jfile.flush()
                jfile.close()
                print("Anime wurde hinzugefügt")

            if(haveAddedAnime == False):              #Füge zu liste hinzu
                isNewAnime = True
                for animeentry in anidata:
                    url = animeentry['url']
                    release = animeentry['releaseID']
                    if(url == anime.getURL() and release == relchoice):
                        print("Anime mit gleichem Release ist bereits in Liste, gehe zurück zur Suche")
                        isNewAnime = False
                if(isNewAnime):
                    print("Füge Anime zu liste hinzu")
                    fullanimedata = data['anime']
                    fullanimedata.append(animedata)
                    data['anime'] = fullanimedata 
#                animedata = {"anime": animedata}
#                data.append(animedata)
                    os.makedirs(os.path.dirname(botfolder), exist_ok=True)
                    jfile = open(botfile, "w")
                    jfile.write(json.dumps(data, indent=4, sort_keys=True))
                    jfile.flush()
                    jfile.close()
                    print("Anime wurde hinzugefügt")

            print("\n\n\n")


def handle_failed_batch(batch_result, all_wanted, animeentry, run_counts,
                        today_iso, name, push, log_fn=log, events=None):
    """Apply the outcome of a failed ``downloadBatchCNL`` to bot state.

    Distinguishes two cases that ``downloadBatchCNL`` cannot tell apart on its
    own:

    * **All-phantom** — the batch failed *only* because every wanted episode
      lies beyond the actually-available max (``available_max``). The site DOM
      over-reported the episode count, so none of the wanted episodes exist in
      any release. This is the benign UNAVAILABLE case (the same reality the
      single-ep path handles): logged at INFO, NOT counted as an error.
    * **Numbering mismatch** — ``downloadBatchCNL`` flagged the result with
      ``reason_code == "episode_numbering_mismatch"``: every wanted episode
      and every release-provided episode are disjoint, but not because the
      wanted episodes are beyond ``available_max`` (that's all-phantom,
      below) — the release numbers its files in a different scheme (e.g.
      absolute numbering continuing across cours). This is a third, distinct
      condition; it must never be confused with all-phantom or genuine
      failure. Logged as ``[MISMATCH]`` (actionable, names the suggested
      ``episode_offset``), NOT counted as an error — a dedicated
      ``run_counts["mismatch"]`` tally instead.
    * **Genuine failure** — no ``available_max`` in the response (e.g. a
      MyJD/JD error or the ``except`` path's caller), or a *partial* phantom
      where some wanted episodes are still in range. Logged as ``[ERROR]`` and
      counted in ``run_counts["errors"]``.

    Either way, if the CNL response carried an ``available_max`` the cap is
    refreshed on ``animeentry`` (``al_available_max`` + ``al_available_max_set_at``)
    so the daily revalidation keeps working and genuinely-new episodes recover.

    Returns ``True`` when the cap was refreshed (caller should ``save_ani()``),
    ``False`` otherwise. Mutates ``animeentry``, ``run_counts`` and (when
    passed) ``events`` in place; performs no I/O of its own, which keeps it
    unit-testable.
    """
    reason = batch_result.get("reason", "unbekannt")
    batch_max = batch_result.get("available_max")
    reason_code = batch_result.get("reason_code")
    all_phantom = (batch_max is not None and
                   all(ep > batch_max for ep in all_wanted))
    if reason_code == "episode_numbering_mismatch":
        log_fn("[MISMATCH] " + name + ": " + reason, push)
        run_counts["mismatch"] = run_counts.get("mismatch", 0) + 1
        if events is not None:
            _record_event(events, "mismatch", name, episodes=all_wanted, detail=reason)
    elif all_phantom:
        log_fn("[UNAVAILABLE] " + name + ": keine Downloadlinks für gewünschte "
               "Episoden — verfügbar bis Episode " + str(batch_max)
               + ", markiere als nicht verfügbar", push)
        run_counts["unavailable"] = run_counts.get("unavailable", 0) + 1
        if events is not None:
            _record_event(events, "unavailable", name, episodes=all_wanted,
                          detail="No download links available (max episode " + str(batch_max) + ")")
    else:
        run_counts["errors"] += 1
        log_fn("[ERROR] Batch-CNL fehlgeschlagen für " + name + ": " + reason + " — überspringe", push)
        if events is not None:
            _record_event(events, "error", name, episodes=all_wanted, detail=reason)
    if batch_max is not None:
        animeentry['al_available_max'] = batch_max
        animeentry['al_available_max_set_at'] = today_iso
        return True
    return False


def startbot():

    jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()
 
    interactive = "--docker" not in sys.argv
    if "--not-interactive" in sys.argv:
        interactive = False
    if "--interactive" in sys.argv:
        interactive = True

    config_attempt = 0
    while(jdhost == False):
        if(interactive):
            print("Noch keine oder Fehlerhafte konfiguration, leite weiter zu Einstellungen")
            editconfig()
            jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()
        else:
            # Non-interactive (Docker): do NOT sys.exit — a transient cause such as
            # the /config volume not being mounted yet on host boot would otherwise
            # crash-loop the container under `restart: unless-stopped`. Stay alive
            # and re-read the config with backoff so it self-heals once available.
            config_attempt += 1
            delay = _boot_backoff(config_attempt)
            _log.error("Keine oder fehlerhafte Konfiguration (Versuch %d) — erneuter Versuch in %ds "
                       "(haeufige Ursache: /config noch nicht gemountet oder ani.json fehlt)",
                       config_attempt, delay)
            time.sleep(delay)
            jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()

    if(pushkey != ""):
        pb = Pushbullet(pushkey)
    else:
        pb = ""
    
    # The animeloads() constructor launches headless Firefox/geckodriver to fetch
    # DDoS-Guard cookies. A cold-start Selenium failure (resource contention while
    # the host is still bringing services up, a stale profile/geckodriver hiccup)
    # raises here. Unguarded, that exception exits the process and crash-loops the
    # container under `restart: unless-stopped` — the most likely "didn't start
    # automatically" path. Retry in-process with backoff so a transient failure
    # self-recovers and the container stays up.
    al = None
    init_attempt = 0
    while al is None:
        try:
            al = animeloads(browser=browser, browserloc=browserlocation)
        except Exception as e:
            if interactive:
                raise
            init_attempt += 1
            delay = _boot_backoff(init_attempt)
            printException(e)
            _log.error("Browser/Selenium-Initialisierung fehlgeschlagen (Versuch %d) — "
                       "erneuter Versuch in %ds", init_attempt, delay)
            time.sleep(delay)
    tvdb = TVDBClient()

    if(interactive):
        if(compare(input("Möchtest du dich anmelden? [J/N]: "), {"j", "ja", "yes", "y"})):
            user = input("Username: ")
            password = getpass("Passwort: ")
            try:
                al.login(user, password)
            except:
                print("Fehlerhafte Anmeldedaten, fahre mit anonymen Account fort")
        else:
            print("Überspringe Anmeldung")
    else:
        if(al_user is not None and al_pass is not None):
            try:
                al.login(al_user, al_pass)
                _log.info("Erfolgreich bei Anime-Loads angemeldet")
            except:
                _log.warning("Fehlerhafte Anmeldedaten, fahre mit anonymen Account fort")
        else:
            _log.info("Keine Anmeldedaten für Anime-Loads hinterlegt, fahre mit anonymen Account fort")

    if(jdhost == "" and myjd_pass == ""):
        if(interactive == False):
            # Misconfiguration: neither a local JD host nor a MyJDownloader
            # password. Don't sys.exit (crash-loops under unless-stopped); stay
            # alive and re-read the config with backoff so the owner can fix it
            # without manually restarting the container.
            jdpw_attempt = 0
            while(jdhost == "" and myjd_pass == ""):
                jdpw_attempt += 1
                delay = _boot_backoff(jdpw_attempt)
                _log.error("Kein MyJdownloader Passwort und kein JD-Host gesetzt — "
                           "Container bleibt aktiv, erneute Pruefung in %ds", delay)
                time.sleep(delay)
                jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()
        else:
            print("Kein MyJdownloader Passwort gesetzt")  # interactive prompt
            logincorrect = False
            jd=myjdapi.Myjdapi()
            jd.set_app_key("animeloads")
            while(logincorrect == False):
                myjd_pass = getpass("MyJdownloader Passwort: ")

                try:
                  jd.connect(myjd_user, myjd_pass)
                  logincorrect = True
                except:
                    print("Fehlerhafte Logindaten")
    _log.info("Erfolgreich eingeloggt")
    port_attempt = 0
    while (jd_deprecated and jd_deprecatedport == ""):
        if interactive:
            _log.error("Kein JD port gesetzt. beende...")
            sys.exit(1)
        # Non-interactive: keep the container alive and re-read config so a fix
        # (or a late volume mount) is picked up without a manual restart.
        port_attempt += 1
        delay = _boot_backoff(port_attempt)
        _log.error("Kein JD port gesetzt — Container bleibt aktiv, erneute Pruefung in %ds", delay)
        time.sleep(delay)
        jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, jd_deprecated, jd_deprecatedport, al_user, al_pass = loadconfig()

    while(True):
        # Per-cycle run-state bookkeeping (persisted for the dashboard's
        # last_run/next_run, independent of the rolling log tail).
        run_started = _utcnow_iso()
        run_counts = {"entries": 0, "checked": 0, "downloaded": 0, "errors": 0,
                      "skipped": 0, "unavailable": 0, "mismatch": 0}
        events = []
        os.makedirs(os.path.dirname(botfolder), exist_ok=True)
        f = open(botfile, "r")
        data = json.load(f)
        f.close()

        anidata = ""
        try:
            anidata = data['anime']
        except:
            # No anime configured yet (fresh deploy, or none added via the
            # dashboard). Do NOT return — that exits the process (exit 0) and
            # stops/tight-loops the container under `restart: unless-stopped`,
            # which reads as "the bot won't stay running". Stay alive and
            # re-check after the poll interval so entries added later via the
            # dashboard are picked up without a manual container restart.
            recheck = timedelay if isinstance(timedelay, int) and timedelay > 0 else 600
            _log.info("Keine Anime in der Liste — erneute Pruefung in " + str(recheck) + " Sekunden")
            write_run_state(run_started, _utcnow_iso(), recheck, run_counts, events)
            time.sleep(recheck)
            continue

        def save_ani():
            os.makedirs(os.path.dirname(botfolder), exist_ok=True)
            jfile = open(botfile, "w")
            jfile.write(json.dumps(data, indent=4, sort_keys=True))
            jfile.flush()
            jfile.close()

        if(anidata != ""):
            run_counts["entries"] = len(anidata)
            for idx, animeentry in enumerate(anidata):
                name = animeentry['name']
                url = animeentry['url']
                releaseID = animeentry['releaseID']
                try:
                    customPackage = animeentry['customPackage']
                except:
                    customPackage = ""
                # JD package name must be unique per release so JDownloader does
                # not merge two seasons of the same show (which share customPackage
                # by design — that field defines the Plex destination folder).
                jdPackageName = (
                    animeentry.get('display_title')
                    or animeentry.get('name')
                    or customPackage
                )
                try:
                    destinationFolder = animeentry['destinationFolder']
                except:
                    destinationFolder = None
                missingEpisodes = animeentry['missing']
                episodes = animeentry['episodes']

                # --- Smart skip logic -------------------------------------------
                # Step 1: Already marked complete and no missing episodes
                if animeentry.get('complete') and len(missingEpisodes) == 0:
                    _log.info("[SKIP] " + name + " is complete")
                    run_counts["skipped"] += 1
                    continue

                # Step 2: Check cached anime-loads.org status (no network call)
                al_status = animeentry.get('al_status', '')
                al_max = animeentry.get('al_max_episodes')
                if al_status in ("Abgeschlossen", "Completed", "Complete") \
                        and al_max and episodes >= al_max \
                        and len(missingEpisodes) == 0:
                    _log.info("[COMPLETE] " + name + " — al_status: " + al_status + ", all " + str(al_max) + " episodes downloaded")
                    animeentry['complete'] = True
                    save_ani()
                    run_counts["skipped"] += 1
                    continue

                # Step 3: Waiting for next episode airdate (skip_until).
                # Only a REAL TVDB-predicted airdate (skip_real_airdate) gets the
                # early-scrape window — scrape on the eve to catch an episode
                # anime-loads.org published before its airdate. Synthetic throttle
                # dates (no-airdate default, THROTTLE step) regenerate daily, so
                # early-scraping them would recur into perpetual every-cycle
                # scraping; those are honored strictly. Missing marker (legacy
                # entry) → treat as not-real until the next Step 4 pass re-marks it.
                skip_until = animeentry.get('skip_until', '')
                if skip_until:
                    if animeentry.get('skip_real_airdate'):
                        honor_skip = not should_scrape_despite_skip(skip_until, date.today(), EARLY_SCRAPE_DAYS)
                    else:
                        try:
                            honor_skip = date.today() < date.fromisoformat(skip_until)
                        except (ValueError, TypeError):
                            honor_skip = False
                    if honor_skip:
                        if len(missingEpisodes) == 0:
                            _log.info("[SKIP] " + name + " — next episode airs " + skip_until)
                            run_counts["skipped"] += 1
                            continue
                        else:
                            _log.info("[RETRY] " + name + " — next episode airs " + skip_until
                                      + " but " + str(len(missingEpisodes)) + " missing episodes to retry")

                # Step 4: TVDB-based checks (lightweight HTTP, no Selenium)
                # Movies are not series — skip TVDB series status logic.
                tvdb_id = animeentry.get('tvdb_id')
                tvdb_season = animeentry.get('tvdb_season')
                is_movie_cached = animeentry.get('media_type') == 'movie'
                if tvdb_id and tvdb.available and not is_movie_cached:
                    try:
                        series_status = tvdb.get_series_status(tvdb_id)
                        if series_status:
                            animeentry['tvdb_series_status'] = series_status

                        if series_status == "Ended":
                            # Check if all episodes for this season are downloaded
                            if tvdb_season:
                                tvdb_ep_count = tvdb.get_season_episode_count(tvdb_id, tvdb_season)
                            else:
                                tvdb_ep_count = None
                            if tvdb_ep_count and episodes >= tvdb_ep_count and len(missingEpisodes) == 0:
                                _log.info("[COMPLETE] " + name + " — series ended, all episodes downloaded")
                                animeentry['complete'] = True
                                save_ani()
                                run_counts["skipped"] += 1
                                continue

                        elif series_status == "Continuing" and tvdb_season:
                            airdate = tvdb.get_next_episode_airdate(tvdb_id, tvdb_season, episodes)
                            if airdate:
                                try:
                                    air_date_obj = date.fromisoformat(airdate)
                                    if air_date_obj > date.today():
                                        # Cache the airdate for the skip_until badge / next cycle,
                                        # but scrape anyway once within EARLY_SCRAPE_DAYS of it to
                                        # catch an episode published before its TVDB airdate. Mark
                                        # this as a real airdate so Step 3 applies the early-scrape.
                                        animeentry['skip_until'] = airdate
                                        animeentry['skip_real_airdate'] = True
                                        save_ani()
                                        if should_scrape_despite_skip(airdate, date.today(), EARLY_SCRAPE_DAYS):
                                            _log.info("[EARLY] " + name + " — next episode airs " + airdate
                                                      + ", scraping within " + str(EARLY_SCRAPE_DAYS) + "d for early release")
                                        elif len(missingEpisodes) == 0:
                                            _log.info("[SKIP] " + name + " — next episode airs " + airdate)
                                            run_counts["skipped"] += 1
                                            continue
                                        else:
                                            _log.info("[RETRY] " + name + " — next episode airs " + airdate
                                                      + " but " + str(len(missingEpisodes)) + " missing episodes to retry")
                                except ValueError:
                                    pass
                            else:
                                # No known airdate — synthetic throttle date (regenerates daily),
                                # so it is honored strictly (skip_real_airdate=False); the early-scrape
                                # window applies only to real predicted airdates. Skip 1 day to avoid
                                # pointless scraping while rechecking TVDB once per day.
                                default_skip = (date.today() + timedelta(days=1)).isoformat()
                                animeentry['skip_until'] = default_skip
                                animeentry['skip_real_airdate'] = False
                                save_ani()
                                if len(missingEpisodes) == 0:
                                    _log.info("[SKIP] " + name + " — no airdate known, re-check " + default_skip)
                                    run_counts["skipped"] += 1
                                    continue
                                else:
                                    _log.info("[RETRY] " + name + " — no airdate known, re-check " + default_skip
                                              + " but " + str(len(missingEpisodes)) + " missing episodes to retry")
                    except Exception as e:
                        _log.warning("[TVDB] Error checking " + name + ": " + str(e))
                # --- End skip logic ----------------------------------------------

                try:
                    anime = al.getAnime(url)
                    release = anime.getReleases()[releaseID-1]
                except:
                    _log.warning("Failed to get Anime, skipping...")
                    run_counts["checked"] += 1
                    run_counts["errors"] += 1
                    _record_event(events, "error", name, detail="Failed to fetch anime data")
                    continue

                now = datetime.now()
                run_counts["checked"] += 1
                _log.info("[" + now.strftime("%H:%M:%S") + "] Prüfe " + name + " auf updates")
                # updateInfo already called by getAnime — skip redundant call
                curEpisodes = release.getEpisodeCount()               #Anzahl der Episoden aktuell online

                # Cap curEpisodes by known-available max (DOM may over-report if tabs have no links).
                # The cap is single-day: a stale cap from a prior day must not permanently hide
                # episodes that the site has since added. Revalidate by clearing yesterday's cap
                # when DOM reports more — if those new episodes are still phantom, the batch/single-ep
                # paths below will re-set the cap with today's date.
                today_iso = date.today().isoformat()
                al_available_max = animeentry.get('al_available_max')
                cap_set_at = animeentry.get('al_available_max_set_at')
                if al_available_max is not None and al_available_max < curEpisodes and cap_set_at != today_iso:
                    _log.info("[CAP-RESET] " + name + ": clearing stale al_available_max="
                              + str(al_available_max) + " (set " + (cap_set_at or "never")
                              + ") — DOM reports " + str(curEpisodes))
                    animeentry.pop('al_available_max', None)
                    animeentry.pop('al_available_max_set_at', None)
                    al_available_max = None
                    save_ani()
                if al_available_max is not None and al_available_max < curEpisodes:
                    curEpisodes = al_available_max
                    # Self-heal: drop missing/episodes values that exceed the real max
                    missingEpisodes = [m for m in missingEpisodes if m <= al_available_max]
                    animeentry['missing'] = missingEpisodes
                    if int(animeentry['episodes']) > al_available_max:
                        animeentry['episodes'] = al_available_max
                        episodes = al_available_max
                    save_ani()

                # Cache anime-loads.org status for dashboard
                if anime.status:
                    animeentry['al_status'] = anime.status
                if anime.maxEpisodes != 999999:
                    animeentry['al_max_episodes'] = anime.maxEpisodes
                # Throttle: anime finished but release on site is still missing
                # episodes. Use freshly-scraped values; recheck once per day via Step 3.
                fresh_al_status = animeentry.get('al_status', '')
                fresh_al_max = animeentry.get('al_max_episodes')
                if fresh_al_status in ("Abgeschlossen", "Completed", "Complete") \
                        and fresh_al_max and curEpisodes < fresh_al_max \
                        and len(missingEpisodes) == 0:
                    throttle_date = (date.today() + timedelta(days=1)).isoformat()
                    _log.info("[THROTTLE] " + name + " — complete but release has "
                          + str(curEpisodes) + "/" + str(fresh_al_max) + " eps, re-check " + throttle_date)
                    animeentry['skip_until'] = throttle_date
                    # Synthetic throttle date — honored strictly, not early-scraped.
                    animeentry['skip_real_airdate'] = False
                    save_ani()
                # Cache media type + naming metadata (used by mover to route movies
                # to a separate output folder with Plex "Title (Year)" convention).
                if anime.type:
                    animeentry['media_type'] = anime.type
                if getattr(anime, 'year', 0):
                    animeentry['year'] = anime.year
                display = (getattr(anime, 'gerName', '') or
                           getattr(anime, 'engName', '') or
                           getattr(anime, 'japName', ''))
                if display:
                    animeentry['display_title'] = display
                # Collect all wanted episodes (missing + new)
                wanted_missing = list(missingEpisodes)
                wanted_new = list(range(episodes + 1, curEpisodes + 1)) if int(episodes) < curEpisodes else []
                all_wanted = wanted_missing + wanted_new
                if len(all_wanted) > 1:
                    # Multi-episode: CNL only, no per-episode fallback
                    if not al.username or al.username == "anonymous":
                        run_counts["errors"] += 1
                        log("[ERROR] " + name + ": Login erforderlich für Batch-CNL (" + str(len(all_wanted)) + " Episoden) — überspringe", pb)
                        _record_event(events, "error", name, episodes=all_wanted,
                                      detail="Login required for batch download")
                    else:
                        try:
                            log("[BATCH] Versuche Batch-Download für " + str(len(all_wanted)) + " Episoden von " + name, pb)
                            batch_result = anime.downloadBatchCNL(
                                release, hoster, browser, browserlocation,
                                jdhost=jdhost, myjd_user=myjd_user, myjd_pw=myjd_pass,
                                myjd_device=myjd_device, jd_deprecated=jd_deprecated,
                                jd_deprecatedport=jd_deprecatedport,
                                pkgName=jdPackageName, destinationFolder=destinationFolder,
                                wanted_episodes=set(all_wanted),
                                episode_offset=animeentry.get('episode_offset', 0) or 0)
                            if batch_result["success"]:
                                batch_sent = set(batch_result["episodes_sent"])
                                run_counts["downloaded"] += len(batch_sent)
                                log("[BATCH] " + str(len(batch_sent)) + " Episoden von " + name + " zu JDownloader hinzugefügt", pb)
                                _record_event(events, "download", name, episodes=sorted(batch_sent))
                                # Record actual available max from CNL data (authoritative; DOM may over-report)
                                batch_max = batch_result.get("available_max")
                                if batch_max is not None:
                                    animeentry['al_available_max'] = batch_max
                                    animeentry['al_available_max_set_at'] = today_iso
                                # Update ani.json for batch-sent episodes
                                for ep in sorted(batch_sent):
                                    if ep in missingEpisodes:
                                        missingEpisodes.remove(ep)
                                    if ep > animeentry['episodes']:
                                        animeentry['episodes'] = ep
                                # Rebuild missing: drop batch-sent and anything beyond available_max
                                remaining_missing = [m for m in missingEpisodes if m not in batch_sent]
                                if batch_max is not None:
                                    remaining_missing = [m for m in remaining_missing if m <= batch_max]
                                animeentry['missing'] = remaining_missing
                                # Capture the actual JD download folder pattern so the mover can match it.
                                release_pattern = batch_result.get("release_pattern") or getattr(anime, '_last_release_pattern', '')
                                if release_pattern:
                                    animeentry['download_folder_pattern'] = release_pattern
                                save_ani()
                                if batch_result["episodes_not_found"]:
                                    _log.warning("[BATCH] Episoden nicht im Batch gefunden: %s — werden beim nächsten Lauf erneut versucht",
                                                 batch_result["episodes_not_found"])
                            else:
                                # Decide error-vs-benign and refresh the cap in one place
                                # (see handle_failed_batch). save_ani() stays here so the
                                # helper remains pure/testable.
                                if handle_failed_batch(batch_result, all_wanted, animeentry,
                                                       run_counts, today_iso, name, pb,
                                                       events=events):
                                    save_ani()
                        except Exception as e:
                            printException(e)
                            run_counts["errors"] += 1
                            log("[ERROR] Batch-CNL fehlgeschlagen für " + name + ": " + str(e) + " — überspringe", pb)
                            _record_event(events, "error", name, episodes=all_wanted,
                                          detail="Batch-CNL failed: " + type(e).__name__)

                elif len(all_wanted) == 1:
                    # Single episode: use per-episode download
                    ep = all_wanted[0]
                    is_missing = ep in wanted_missing
                    log("[DOWNLOAD] Lade Episode " + str(ep) + " von " + name, pb)
                    ep_unavailable = False
                    try:
                        if(myjd_user != ""):
                            dl_ret = anime.downloadEpisode(ep, release, hoster, browser, browserlocation, myjd_user=myjd_user, myjd_pw=myjd_pass, myjd_device=myjd_device,jd_deprecated=jd_deprecated,jd_deprecatedport=jd_deprecatedport, pkgName=jdPackageName, destinationFolder=destinationFolder)
                        else:
                            dl_ret = anime.downloadEpisode(ep, release, hoster, browser, browserlocation, jdhost, jd_deprecated=jd_deprecated,jd_deprecatedport=jd_deprecatedport, pkgName=jdPackageName, destinationFolder=destinationFolder)
                    except ALLinkExtractionException as e:
                        # CNL returned no usable key/links — episode has no download data on the site
                        ep_unavailable = True
                        dl_ret = False
                    except Exception as e:
                        printException(e)
                        dl_ret = False
                    if(dl_ret == True):
                        run_counts["downloaded"] += 1
                        log("[DOWNLOAD] Episode " + str(ep) + " von " + name + " wurde zu JDownloader hinzugefügt", pb)
                        if is_missing:
                            if ep in missingEpisodes:
                                missingEpisodes.remove(ep)
                            animeentry['missing'] = missingEpisodes
                        if ep > animeentry['episodes']:
                            animeentry['episodes'] = ep
                        release_pattern = getattr(anime, '_last_release_pattern', '')
                        if release_pattern:
                            animeentry['download_folder_pattern'] = release_pattern
                        save_ani()
                        _record_event(events, "download", name, episodes=[ep])
                    elif ep_unavailable:
                        log("[UNAVAILABLE] Episode " + str(ep) + " von " + name + " — keine Downloadlinks, markiere als nicht verfügbar", pb)
                        run_counts["unavailable"] += 1
                        _record_event(events, "unavailable", name, episodes=[ep],
                                      detail="No download links available")
                        # Cap al_available_max so we stop asking for this (or higher) ep
                        prev_max = animeentry.get('al_available_max')
                        new_max = ep - 1
                        if prev_max is None or prev_max > new_max:
                            animeentry['al_available_max'] = new_max
                        animeentry['al_available_max_set_at'] = today_iso
                        if ep in missingEpisodes:
                            missingEpisodes.remove(ep)
                            animeentry['missing'] = missingEpisodes
                        # Roll back episodes counter if it was advanced into this unavailable ep
                        if int(animeentry['episodes']) >= ep:
                            animeentry['episodes'] = new_max
                        save_ani()
                    elif isinstance(dl_ret, Exception):
                        run_counts["errors"] += 1
                        log("[ERROR] Episode " + str(ep) + " von " + name + ": " + str(dl_ret), pb)
                        _record_event(events, "error", name, episodes=[ep],
                                      detail="Download failed: " + type(dl_ret).__name__)
                    else:
                        run_counts["errors"] += 1
                        log("[ERROR] Episode " + str(ep) + " von " + name + ": JDownloader nicht erreichbar?", pb)
                        _record_event(events, "error", name, episodes=[ep], detail="JDownloader unreachable")
                        # Transient failure: don't mutate state — next run will retry naturally

                else:
                    _log.info("[INFO] " + name + " hat keine neuen Folgen verfügbar")

                # Auto-detect completion from anime-loads.org status
                updated_missing = animeentry.get('missing', [])
                if anime.type == "movie":
                    # Movies have no episode count on the site (maxEpisodes stays 999999).
                    # A single successful release download is the whole thing.
                    if animeentry.get('episodes', 0) >= 1 and len(updated_missing) == 0:
                        _log.info("[COMPLETE] " + name + " — movie release downloaded")
                        animeentry['complete'] = True
                        save_ani()
                        _record_event(events, "complete", name)
                elif anime.status in ("Abgeschlossen", "Completed") \
                        and anime.maxEpisodes != 999999 \
                        and animeentry['episodes'] >= anime.maxEpisodes \
                        and len(updated_missing) == 0:
                    _log.info("[COMPLETE] " + name + " — anime-loads status: " + anime.status)
                    animeentry['complete'] = True
                    save_ani()
                    _record_event(events, "complete", name)
            write_run_state(run_started, _utcnow_iso(), timedelay, run_counts, events)
            _log.info("Schlafe " + str(timedelay) + " Sekunden")
            time.sleep(timedelay)

def removeAnime():
    jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, al_user, al_pass = loadconfig()
 
    while(jdhost == False):
        print("Noch keine oder Fehlerhafte konfiguration, leite weiter zu Einstellungen")
        editconfig()
        jdhost, hoster, browser, browserlocation, pushkey, timedelay, myjd_user, myjd_pass, myjd_device, al_user, al_pass = loadconfig()

    os.makedirs(os.path.dirname(botfolder), exist_ok=True)
    f = open(botfile, "r")
    data = json.load(f)
    f.close()

    anidata = ""
    try:
        anidata = data['anime']
    except:
        print("Du hast keine Anime in deiner Liste")


    if(anidata != ""):
        print("Deine Liste: ")
        while(True):
            for idx, animeentry in enumerate(anidata):
                print("[ID: " + str(idx+1) + "] " + animeentry['name'] + " mit Release " + str(animeentry['releaseID']))
            selection = input("Welchen Anime möchtest du löschen? (ID eingeben, \"exit\" zum beenden): ")
            if(selection == "exit"):
                print("Exit, beende...")
                break
            else:
                try:
                    sel_int = int(selection) - 1
                    data['anime'].pop(sel_int)
                    os.makedirs(os.path.dirname(botfolder), exist_ok=True)
                    jfile = open(botfile, "w")
                    jfile.write(json.dumps(data, indent=4, sort_keys=True))
                    jfile.flush()
                    jfile.close()
                    print("Anime wurde gelöscht")
                except:
                    print("Fehler beim löschen des Eintrags")

def printhelp():
    print("anibot.py [edit | start | add | remove]")
    print("[edit]:    Ändere deine Einstellungen")
    print("[start]:   Starte Bot und lade Episoden runter")
    print("[add]:     Füge neue Anime zu deiner Liste hinzu")
    print("[remove]:  Lösche Anime aus deiner Liste")


# CLI dispatch. Guarded by __name__ == "__main__" so that `import anibot`
# (e.g. from the test suite) does NOT launch the bot, while running the module
# as a script — `python anibot.py [args]`, the Docker ENTRYPOINT/CMD — still
# dispatches identically. The if-blocks below don't create a new scope, so the
# module-level globals botfile/botfolder are reassigned exactly as before.
if __name__ == "__main__":
    commandSet = False
    if(arglen >= 2):
        for idx, arg in enumerate(sys.argv):
            if(arg == "--configfile"):
                try:
                    botfile = sys.argv[idx+1]
                    botfolder_arr = botfile.split("/")[:-1]
                    botfolder = ""
                    for p in botfolder_arr:
                        botfolder += p
                        botfolder += "/"
                    print("Config Datei: " + botfile)
                except Exception as e:
                    botfile = "config/ani.json"
                    botfolder = "config/"
                    print("--configfile gegeben, aber kein Pfad (oder fehlerhafter) danach, setze Pfad auf ./config/ani.json")
            if(arg == "start"):
                commandSet = True
                startbot()
            elif(arg == "edit"):
                commandSet = True
                editconfig()
                print("Einstellungen gespeichert")
            elif(arg == "add"):
                commandSet = True
                addAnime()
            elif(arg == "remove"):
              commandSet = True
              removeAnime()
            elif("help" in arg):
                printhelp()

    else:
        if(arglen == 1):
            startbot()
        printhelp()

    if(commandSet == False):
        startbot()

    #episodes = getEpisodes()
