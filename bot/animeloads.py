import requests, http.cookiejar
import logging

import myjdapi

from lxml import etree
from lxml import html

from html_table_parser import HTMLTableParser

import subprocess

from Cryptodome.Cipher import AES
from binascii import unhexlify
import base64

import numpy, random, time

import re

import os, sys

from urllib.parse import unquote

from selenium import webdriver
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By


#captcha
import time, json, hashlib, cv2, numpy, shutil

_log = logging.getLogger("animeloads")

MODERN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

SELENIUM_HOMEPAGE_LOAD_TIMEOUT = int(os.environ.get("SELENIUM_HOMEPAGE_LOAD_TIMEOUT", "30"))
SELENIUM_PAGE_LOAD_TIMEOUT     = int(os.environ.get("SELENIUM_PAGE_LOAD_TIMEOUT", "60"))
BROWSER_INIT_WAIT              = int(os.environ.get("BROWSER_INIT_WAIT", "3"))
BROWSER_PAGE_WAIT              = int(os.environ.get("BROWSER_PAGE_WAIT", "10"))
BROWSER_DOWNLOAD_PAGE_WAIT     = int(os.environ.get("BROWSER_DOWNLOAD_PAGE_WAIT", "15"))
CAPTCHA_FETCH_DELAY_MIN        = float(os.environ.get("CAPTCHA_FETCH_DELAY_MIN", "1.5"))
CAPTCHA_FETCH_DELAY_MAX        = float(os.environ.get("CAPTCHA_FETCH_DELAY_MAX", "3.0"))
CAPTCHA_VERIFY_DELAY_MIN       = float(os.environ.get("CAPTCHA_VERIFY_DELAY_MIN", "0.8"))
CAPTCHA_VERIFY_DELAY_MAX       = float(os.environ.get("CAPTCHA_VERIFY_DELAY_MAX", "2.0"))
CAPTCHA_DOWNLOAD_DELAY_MIN     = float(os.environ.get("CAPTCHA_DOWNLOAD_DELAY_MIN", "0.5"))
CAPTCHA_DOWNLOAD_DELAY_MAX     = float(os.environ.get("CAPTCHA_DOWNLOAD_DELAY_MAX", "1.5"))
CNL_RETRY_DELAY_MIN            = float(os.environ.get("CNL_RETRY_DELAY_MIN", "5"))
CNL_RETRY_DELAY_MAX            = float(os.environ.get("CNL_RETRY_DELAY_MAX", "10"))
JD_HTTP_TIMEOUT                = int(os.environ.get("JD_HTTP_TIMEOUT", "30"))


def _create_driver(browser, browserloc=""):
    """Create a headless Selenium WebDriver."""
    if browser == animeloads.FIREFOX or browser == 0:
        opts = FirefoxOptions()
        opts.add_argument("--headless")
        opts.set_preference("general.useragent.override", MODERN_UA)
        if browserloc:
            opts.binary_location = browserloc
        svc = FirefoxService(log_output=os.devnull)
        return webdriver.Firefox(service=svc, options=opts)
    elif browser == animeloads.CHROME or browser == 1:
        opts = ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--user-agent=" + MODERN_UA)
        if browserloc:
            opts.binary_location = browserloc
        svc = ChromeService(log_output=os.devnull)
        return webdriver.Chrome(service=svc, options=opts)
    else:
        raise ALInvalidBrowserException("Nicht unterstützter Browser")


def _parse_episode_from_url(url_str):
    """Extract 1-based episode number from a download URL. Returns int or None."""
    # SxxExx pattern (most common)
    m = re.search(r'[Ss]\d+[Ee](\d+)', url_str)
    if m:
        return int(m.group(1))
    # Ep/Episode pattern
    m = re.search(r'(?:Ep|Episode)[._-]?(\d+)', url_str, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # " - NN " pattern common in anime releases
    m = re.search(r'\s-\s(\d{2,3})[\s._]', url_str)
    if m:
        return int(m.group(1))
    # E followed by digits at word boundary (e.g. _E03_ or .E03.)
    m = re.search(r'[._-]E(\d{2,3})[._-]', url_str, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _release_name_from_links(links_decoded):
    """Derive a release-name pattern from the first decoded CNL link.

    Used by anibot to persist `download_folder_pattern` so the mover can match
    the actual JD download folder (which JD names from the release archive,
    not from the CNL package name).
    """
    if not links_decoded:
        return ""
    first = links_decoded[0]
    if isinstance(first, bytes):
        first = first.decode('utf-8', errors='replace')
    name = first.rsplit('/', 1)[-1]
    return re.sub(r'\.(part\d+\.)?(rar|r\d{2,3}|mkv|mp4|avi)$', '', name, flags=re.IGNORECASE)


def _group_urls_by_episode(decoded_urls, total_episodes):
    """Group decrypted URL byte-strings by episode number.

    Returns dict mapping 1-based episode number to list of URL strings.
    """
    grouped = {}
    unparsed = []

    for url_bytes in decoded_urls:
        if not url_bytes:
            continue
        url_str = url_bytes.decode('utf-8', errors='replace')
        ep = _parse_episode_from_url(url_str)
        if ep is not None:
            grouped.setdefault(ep, []).append(url_str)
        else:
            unparsed.append(url_str)

    # If we parsed nothing, try sequential assignment
    if not grouped and unparsed and total_episodes > 0:
        chunk_size = max(1, len(unparsed) // total_episodes)
        for i in range(total_episodes):
            start = i * chunk_size
            end = start + chunk_size if i < total_episodes - 1 else len(unparsed)
            if start < len(unparsed):
                grouped[i + 1] = unparsed[start:end]
        _log.warning("Could not parse episode numbers from URLs, used sequential assignment")
    elif unparsed:
        _log.warning("Could not parse episode from %d/%d URLs",
                     len(unparsed), len(unparsed) + sum(len(v) for v in grouped.values()))

    return grouped


class animeloads:

    FIREFOX = 0
    CHROME = 1
    DDOWNLOAD = 0
    RAPIDGATOR = 1

    def __init__(self, user="", pw="", browser="", browserloc=""):
        self.user = user
        self.pw = pw
        self.browser = browser
        self.browserloc = browserloc
        self.session = requests.session()
        self.username = "anonymous"
        self.isVIP = False
        self.session.headers = {'User-Agent': MODERN_UA}
        if(user != "" and pw != ""):
            self.login(self.user, self.pw)

        # Fetch DDoS-Guard cookies for the requests session.
        # Needed for login() and search() which use requests.
        _t0 = time.time()
        _log.debug("AL.__init__: creating DDoS-Guard driver...")
        driver = _create_driver(browser, browserloc)
        _log.debug("AL.__init__: driver created in %.1fs", time.time() - _t0)
        driver.set_page_load_timeout(SELENIUM_HOMEPAGE_LOAD_TIMEOUT)
        _t1 = time.time()
        _log.debug("AL.__init__: loading homepage...")
        try:
            driver.get("https://www.anime-loads.org/")
        except Exception:
            pass
        _log.debug("AL.__init__: homepage loaded in %.1fs (or timed out)", time.time() - _t1)
        time.sleep(BROWSER_INIT_WAIT)
        for cookie in driver.get_cookies():
            self.session.cookies.set(cookie['name'], cookie['value'])
        driver.quit()
        _log.debug("AL.__init__: total init %.1fs", time.time() - _t0)

    def search(self, query):
        searchdata = self.session.get(apihelper.getSearchURL(query), allow_redirects=False)
        searchresults = []
        _log.debug("Search response status: %s", searchdata.status_code)
        if(searchdata.status_code == 302):  #only 1 result, got redirect
            redir_url = searchdata.headers['Location']
            redir_anime = anime(redir_url, self.session, self)
            #num__results = 1
            result = searchResult(redir_url, redir_anime.getName(), redir_anime.getType(), redir_anime.getYear(), redir_anime.getCurrentEpisodes(), redir_anime.getMaxEpisodes(), ["UNKNOWN"], ["UNKNOWN"], redir_anime.getMainGenre(), self.session, self)
            searchresults.append(result)
        else:
            search_dom = etree.HTML(searchdata.text)
            searchboxes = search_dom.xpath("//div/div[@class='panel panel-default' and 1]/div[@class='panel-body' and 1]")
            #num__results = len(searchboxes)
            for result in searchboxes:
                url = ""                #done
                name = ""               #done
                typ = ""                #done
                relDate = ""            #done
                epCountCurrent = 0      #done
                epCountMax = 0          #done
                dubLang = []            #done
                subLang = []            #done
                genre = ""              #done
                boxtree = etree.HTML(html.tostring(result))
                result_links = boxtree.xpath("//a")
                isFirst = True              #Check if link is first entry, cause there are two
                for link in result_links:
                    linktext = link.text
                    href = link.get('href')
                    #if linktext is None or href is None:
                    #    continue
                    if("genre" in href):
                        genre = linktext
                    elif("https://www.anime-loads.org/anime-series" in href):
                        typ = "series"
                    elif("https://www.anime-loads.org/anime-movies" in href):
                        typ = "movie"
                    elif("https://www.anime-loads.org/bonus" in href):
                        typ = "bonus"
                    elif("https://www.anime-loads.org/ova" in href):
                        typ = "ova"
                    elif("https://www.anime-loads.org/special" in href):
                        typ = "special"
                    elif("https://www.anime-loads.org/web" in href):
                        typ = "web"
                    elif("https://www.anime-loads.org/year/" in href):  #Year-element
                        yearsplit = href.split("/")
                        year = yearsplit[len(yearsplit)-1]
                    elif("https://www.anime-loads.org/media/" in href): #href to detail site, contains link and name
                        if(isFirst):
                            isFirst = False
                            continue
                        url = href
                        name = linktext
                        isFirst = True

                result_span = boxtree.xpath("//span[@class='label label-gold']")    #Span-Element for Episode count
                for span in result_span:
                    epsplit = str(html.tostring(span)).split("/")
                    epCountCurrent = re.sub("[^0-9]", "", epsplit[1])   #Remove non-numbers from string
                    epCountMax = re.sub("[^0-9]", "", epsplit[2])

                lang_class = boxtree.xpath("//div[@class='mt10 mb10' and 3]")[0]       #Line which contains language flags, take first line as there is only one
                langsplit = str(html.tostring(lang_class)).split("\"")
                for substring in langsplit:
                    if("Sprache: " in substring):
                        dubLang.append(substring.replace("Sprache: ", ""))
                    elif("Untertitel: " in substring):
                        subLang.append(substring.replace("Untertitel: ", ""))
                result = searchResult(url, name, typ, relDate, epCountCurrent, epCountMax, dubLang, subLang, genre, self.session, self)
                searchresults.append(result)
        return searchresults


    def getAnime(self, url):
        return anime(url, self.session, self)


    def login(self, user, pw):
        data = {"identity": user, "password": pw, "remember": "1"}
        headers = {
            'User-Agent': MODERN_UA
        }
        r = self.session.post("https://www.anime-loads.org/auth/signin", data=data, headers=headers)

        for c in self.session.cookies:
            if("username" in c.value):
                data = unquote(c.value)
                jdata = json.loads(data)
                for key in jdata:
                    if(key == "username"):
                        self.username = jdata[key]
                    elif(key == "is_vip"):
                        if(jdata[key] == True):
                            self.isVIP = True
                return True
        raise ALInvalidLoginException("Login data is invalid")



class utils:
    @staticmethod
    def decodeCNL(k, crypted):
#        k_list = list(k)
#        tmp = k_list[15]
#        k_list[15] = k_list[16]
#        k_list[16] = tmp
#        k = "".join(k_list)

        key = unhexlify(k)
        data = base64.standard_b64decode(crypted)
        obj = AES.new(key, AES.MODE_CBC, key)
        d = obj.decrypt(data)
        d = map(lambda x: x.strip(b'\r\x00'), d.split(b'\n'))
        d = list(d)
        return d

    @staticmethod
    def b(s):
        s = str(s)
        a = numpy.int32(0)
        if(len(s) == 0):
            return 0
        for curChar in range(0, len(s)):
            a = numpy.int32(a)
            a = ((a << 5) - a + ord(s[curChar]))
        return abs(a)

    @staticmethod
    def getAdString():
        rand1 = random.uniform(0, 1)
        first = ("" + str(utils.b(str(abs(rand1)))))


        date = str(round(time.time() * 1000))
        sec = ("" + str(utils.b(date)))

        rand3 = random.uniform(0, 1)
        third = ("" + str(utils.b(str(abs(rand3)))))

        return first + "" + sec + third

    @staticmethod
    def addToJD(host, passwords, source, crypted, jk):
        data = {
            "passwords": str(passwords),
            "source": str(source),
            "jk": str(jk),
            "crypted": str(crypted)
        }
        headers = {
        "User-Agent": MODERN_UA,
        # Loopback /flashgot referer makes JDownloader's extern interface skip the
        # "external application tries to add links" confirmation dialog (it checks the
        # referer host/path, not the socket IP) — same trick the FlashGot extension uses.
        "Referer": "http://127.0.0.1/flashgot"
        }
        try:
            req = requests.post("http://" + host + ":9666/flash/addcrypted2", data=data, headers=headers, timeout=JD_HTTP_TIMEOUT)
        except requests.exceptions.Timeout:
            # JDownloader's CNL endpoint often doesn't send a response
            # but the links are still added — treat timeout as success
            _log.warning("addToJD: request timed out (links likely added)")
            return True
        except Exception:
            return False
        retdata = req.text
        if(retdata == "failed"):
            return False
        else:
            return True

    @staticmethod
    def addFlashLinksToJD(host, links, pkgName="", destinationFolder="", passwords=""):
        """Send newline-separated plain URLs to JDownloader's Flashgot /flash/add
        endpoint on port 9666 (same port addToJD uses for /flash/addcrypted2)."""
        data = {
            "urls": links,
            "package": pkgName,
            "autostart": "1",
        }
        if destinationFolder:
            data["destination"] = destinationFolder
        if passwords:
            data["passwords"] = str(passwords)
        # Loopback /flashgot referer suppresses JDownloader's external-add confirmation
        # dialog (see addToJD) so headless batch adds never block on a manual "Allow".
        headers = {"User-Agent": MODERN_UA, "Referer": "http://127.0.0.1/flashgot"}
        try:
            req = requests.post("http://" + str(host) + ":9666/flash/add",
                                data=data, headers=headers, timeout=JD_HTTP_TIMEOUT)
            _log.info("addFlashLinksToJD: status=%d body=%s",
                      req.status_code, repr(req.text[:200]))
        except requests.exceptions.Timeout:
            _log.warning("addFlashLinksToJD: request timed out (links likely added)")
            return True
        except Exception as e:
            _log.error("addFlashLinksToJD: %s", e)
            return False
        if req.status_code >= 400:
            _log.error("addFlashLinksToJD: HTTP %d — JDownloader rejected the request", req.status_code)
            return False
        if req.text.strip().lower() == "failed":
            _log.error("addFlashLinksToJD: JDownloader returned 'failed'")
            return False
        return True

    @staticmethod
    def addToJDdeprecated(host, port, passwords, links, pkgName, destinationFolder=""):
        data = {
               "params":[
                  {
                     "packageName": pkgName,
                     "autoExtract": True,
                     "autostart": True,
                     "destinationFolder": destinationFolder,
                     "extractPassword": passwords,
                     "links": links,
                     "overwritePackagizerRules": True,
                     "priority": "DEFAULT"
                  }
               ]
            }
        headers = {
            "User-Agent": MODERN_UA,
            "Content-type": "application/json"
        }
        try:
            req = requests.post(f'http://{host}:{port}/linkgrabberv2/addLinks', data=json.dumps(data), headers=headers)
        except Exception as e:
            _log.error("Fehler beim senden des requests zum JDownloader: %s", e)
        retdata = json.loads(req.text)
        if ("type" in retdata):
            _log.error("Senden zum JD fehlgeschlagen: %s", retdata["type"])
            return False
        elif ("data" in retdata):
            _log.info("Folge wurde zu JDownloader hinzugefügt. JobID: %s", retdata["data"]["id"])
            return True

    @staticmethod
    def addToMYJD(myjd_user, myjd_pass, myjd_device, links, pkgName, pwd, destinationFolder=None):
        jd=myjdapi.Myjdapi()
        jd.set_app_key("animeloads")
        jd.connect(myjd_user, myjd_pass)
        jd.update_devices()

        device=jd.get_device(myjd_device)

        dl = device.linkgrabber

        return dl.add_links([{
                              "autostart": True,
                              "links": links,
                              "packageName": pkgName,
                              "extractPassword": pwd,
                              "priority": "DEFAULT",
                              "downloadPassword": None,
                              "destinationFolder": destinationFolder,
                              "overwritePackagizerRules": False
                          }])

    @staticmethod
    def transfer_login_cookies(session, driver):
        """Copy login cookies from requests.Session into a Selenium WebDriver.

        The driver must already be on the anime-loads.org domain.
        """
        count = 0
        for c in session.cookies:
            cookie_dict = {
                'name': c.name,
                'value': c.value,
                'domain': c.domain if c.domain else '.anime-loads.org',
                'path': c.path if c.path else '/',
            }
            try:
                driver.add_cookie(cookie_dict)
                count += 1
            except Exception as e:
                _log.debug("transfer_login_cookies: skipping %s: %s", c.name, e)
        _log.debug("transfer_login_cookies: transferred %d cookies", count)

    @staticmethod
    def addPlainLinksToJD(host, port, passwords, links, pkgName, destinationFolder=""):
        """Send plain-text links to JDownloader via the local API.

        Same as addToJDdeprecated but takes host+port separately and
        is intended for batch CNL where we decrypt and filter first.
        """
        data = {
            "params": [{
                "packageName": pkgName,
                "autoExtract": True,
                "autostart": True,
                "destinationFolder": destinationFolder,
                "extractPassword": passwords,
                "links": links,
                "overwritePackagizerRules": True,
                "priority": "DEFAULT"
            }]
        }
        headers = {"Content-Type": "application/json", "User-Agent": MODERN_UA}
        try:
            req = requests.post("http://" + str(host) + ":" + str(port) + "/linkgrabberv2/addLinks",
                                json=data, headers=headers, timeout=JD_HTTP_TIMEOUT)
            _log.info("addPlainLinksToJD: status=%d body=%s",
                      req.status_code, repr(req.text[:200]))
        except requests.exceptions.Timeout:
            _log.warning("addPlainLinksToJD: request timed out (links likely added)")
            return True
        except Exception as e:
            _log.error("addPlainLinksToJD: %s", e)
            return False
        if req.status_code >= 400:
            _log.error("addPlainLinksToJD: HTTP %d — JDownloader rejected the request", req.status_code)
            return False
        try:
            retdata = req.json()
            _log.info("addPlainLinksToJD: job ID %s", retdata.get("id", "unknown"))
        except Exception:
            pass
        return True

    @staticmethod
    def getDifference(img1, img2):
        res = cv2.absdiff(img1, img2)
        res = res.astype(numpy.uint8)
        percentage = (numpy.count_nonzero(res) * 100)/ res.size
        return percentage

    @staticmethod
    def doCaptcha(cID, driver, session, b64):
        captcha_baseURL = "https://www.anime-loads.org/files/captcha"
        img_baseURL = captcha_baseURL + "?cid=0&hash="

        # Step 1: Request captcha images
        _log.debug("Getting captcha images")
        js = ("var xhr2 = new XMLHttpRequest(); "
              "xhr2.open('POST', '" + captcha_baseURL + "', false); "
              "xhr2.setRequestHeader('Content-type', 'application/x-www-form-urlencoded'); "
              "xhr2.setRequestHeader('X-Requested-With', 'XMLHttpRequest'); "
              "xhr2.send('cID=0&rT=1'); "
              "return xhr2.response;")

        ajaxresponse = driver.execute_script(js)
        _log.debug("Captcha image request response: %s", repr(ajaxresponse[:200]) if ajaxresponse else 'empty')
        captchaIDs = json.loads(ajaxresponse) if ajaxresponse else []
        if not captchaIDs:
            _log.warning("Failed to get captcha images")
            return {"code": "error", "message": "no captcha images"}

        # Fetch captcha images via browser to ensure same session as rT=1 request.
        # Use sync XHR to get raw bytes, then convert to base64 via btoa with
        # binary-safe encoding (charCodeAt & 0xff).
        images = []
        images_url = []
        for c in captchaIDs:
            url = img_baseURL + c
            images_url.append(c)
            js_fetch_img = (
                "var xhr = new XMLHttpRequest();"
                "xhr.open('GET', '" + url + "', false);"
                "xhr.overrideMimeType('text/plain; charset=x-user-defined');"
                "xhr.send();"
                "var raw = xhr.responseText;"
                "var bytes = '';"
                "for (var i = 0; i < raw.length; i++) {"
                "  bytes += String.fromCharCode(raw.charCodeAt(i) & 0xff);"
                "}"
                "return btoa(bytes);"
            )
            b64_data = driver.execute_script(js_fetch_img)
            if b64_data:
                images.append(base64.b64decode(b64_data))
            else:
                # Fallback to requests
                for cookie in driver.get_cookies():
                    session.cookies.set(cookie['name'], cookie['value'])
                response = session.get(url, stream=True, headers={'User-Agent': MODERN_UA})
                images.append(response.content)
        _log.debug("Got captcha images")
        time.sleep(random.uniform(CAPTCHA_FETCH_DELAY_MIN, CAPTCHA_FETCH_DELAY_MAX))

        # Step 2: Solve captcha (find unique image)
        _log.debug("Calculating correct captcha")
        cv_images = []
        for image in images:
            nparr = numpy.frombuffer(image, numpy.uint8)
            img_np = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            cv_images.append(img_np)

        perc = []
        for img in cv_images:
            iPerc = 0
            for i in cv_images:
                if hashlib.md5(i).digest() != hashlib.md5(img).digest():
                    iPerc += utils.getDifference(img, i)
            perc.append(iPerc)

        biggest = 0
        bigI = -1
        for idx, iPerc in enumerate(perc):
            _log.debug("Percentage img %d: %s", idx, iPerc)
            if iPerc > biggest:
                biggest = iPerc
                bigI = idx

        _log.debug("Correct captcha: Image Nr. %d with confidence %s", bigI + 1, biggest)

        # Save captcha images and solver decision for later analysis
        from datetime import datetime
        import shutil
        captcha_log_root = "/config/logs/captcha"
        log_dir = os.path.join(captcha_log_root, datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
        try:
            os.makedirs(log_dir, exist_ok=True)
            for idx, img_bytes in enumerate(images):
                with open(os.path.join(log_dir, f"img_{idx}.png"), "wb") as f:
                    f.write(img_bytes)
            decision = {
                "timestamp": datetime.now().isoformat(),
                "selected_index": bigI,
                "selected_hash": images_url[bigI],
                "confidence": biggest,
                "percentages": perc,
                "all_hashes": images_url,
                "result": "pending"
            }
            with open(os.path.join(log_dir, "decision.json"), "w") as f:
                json.dump(decision, f, indent=2)
            # Cleanup: keep only last 50 captcha logs
            try:
                entries = sorted(os.listdir(captcha_log_root))
                while len(entries) > 50:
                    old = entries.pop(0)
                    shutil.rmtree(os.path.join(captcha_log_root, old), ignore_errors=True)
            except:
                pass
        except Exception as e:
            _log.warning("Failed to save captcha debug images: %s", e)

        time.sleep(random.uniform(CAPTCHA_VERIFY_DELAY_MIN, CAPTCHA_VERIFY_DELAY_MAX))

        # Step 3: Verify answer via rT=2 using jQuery $.ajax (must use jQuery,
        # raw XHR returns empty — jQuery adds headers/encoding the server requires).
        _log.debug("Submitting captcha answer")
        js_verify = (
            "return $.ajax({url:'https://www.anime-loads.org/files/captcha',"
            "type:'post',data:{cID:0,pC:'" + images_url[bigI] + "',rT:2},"
            "async:false}).responseText"
        )
        verify_resp = driver.execute_script(js_verify)
        _log.debug("rT=2 verify response: %s", repr(verify_resp))

        if verify_resp != '1':
            # rT=2 returns '' for both wrong answer AND rate limit.
            # Use solver confidence to distinguish: if confidence is high
            # (answer was likely correct), this is a rate limit.
            is_likely_ratelimit = verify_resp == '' and biggest > 50

            if is_likely_ratelimit:
                _log.warning("Captcha answer likely correct (confidence=%.0f) but rT=2 returned '' — rate limited", biggest)
                result_message = "slowdown"
            else:
                _log.error("Captcha verify failed (expected '1', got %s, confidence=%.0f)", repr(verify_resp), biggest)
                result_message = "Captcha verify failed"

            try:
                decision["result"] = "rate_limited" if is_likely_ratelimit else "verify_failed"
                decision["server_message"] = f"rT=2 returned {repr(verify_resp)}, confidence={biggest:.0f}"
                with open(os.path.join(log_dir, "decision.json"), "w") as f:
                    json.dump(decision, f, indent=2)
            except:
                pass
            return {"code": "error", "message": result_message}

        # Step 4: rT=2 returned '1' (correct answer) — now submit the download request
        time.sleep(random.uniform(CAPTCHA_DOWNLOAD_DELAY_MIN, CAPTCHA_DOWNLOAD_DELAY_MAX))
        enc_str = b64.decode('ascii')
        js_final = (
            "return $.ajax({url:'https://www.anime-loads.org/ajax/captcha',"
            "type:'post',data:{enc:'" + enc_str + "',response:'captcha',"
            "'captcha-idhf':'0','captcha-hf':'" + images_url[bigI] + "'},"
            "async:false}).responseText"
        )
        ajaxresponse = driver.execute_script(js_final)
        try:
            response_json = json.loads(ajaxresponse) if ajaxresponse else {}
        except json.JSONDecodeError:
            response_json = {"code": "error", "message": "invalid JSON: " + str(ajaxresponse)[:100]}

        code = response_json.get("code", "")
        _log.debug("Download submit: code=%s, message=%s", code, response_json.get('message', ''))

        # Update decision log with server result
        try:
            decision["result"] = response_json.get("code", "unknown")
            decision["server_message"] = str(response_json.get("message", ""))
            with open(os.path.join(log_dir, "decision.json"), "w") as f:
                json.dump(decision, f, indent=2)
        except:
            pass

        return response_json


class apihelper:
    @staticmethod
    def getSearchURL(query):
        return "https://www.anime-loads.org/search?q=" + query




class searchResult():
    def __init__(self, url, name, typ, relDate, epCountCurrent, epCountMax, dubLang, subLang, genre, session, animeloads):
        self.url = url
        self.animeloads = animeloads
        self.name = name
        self.typ = typ
        self.relDate = relDate
        self.epCountCurrent = epCountCurrent
        self.epCountMax = epCountMax
        self.dubLang = dubLang
        self.subLang = subLang
        self.genre = genre
        self.session = session

    def getUrl(self):
        return self.url

    def getName(self):
        return self.name

    def getTyp(self):
        return self.typ

    def getReleaseDate(self):
        return self.relDate

    def getCurrentEpisodeCount(self):
        return self.epCountCurrent

    def getMaxEpisodeCount(self):
        return self.epCountMax

    def getDubLang(self):
        return self.dubLang

    def getSubLang(self):
        return self.getSubLang

    def getGenre(self):
        return self.genre

    def tostring(self):
        return "Name: " + self.name + ", Typ: " + self.typ + ", Episoden: " + str(self.epCountCurrent) + "/" + str(self.epCountMax) + ", Dub: " + str(self.dubLang) + ", Subs: " + str(self.subLang)

    def getAnime(self):
        return anime(self.url, self.session, self.animeloads)   #todo session redundant

class anime():
    def __init__(self, url, session, animeloads):
        self.url = url
        self.animeloads = animeloads
        self.session = session
        self.gerName = ""
        self.engName = ""
        self.japName = ""
        self.synonymes = []
        self.type = ""
        self.year = 0
        self.runtime = 0
        self.status = ""
        self.mainGenre = ""
        self.sideGenres = []
        self.tags = []
        self.releases = []
        self.curEpisodes = 1337
        self.maxEpisodes = 1337
        self.coverurl = ""
        self.updateInfo()

    def updateInfo(self):
        _t_total = time.time()
        # Use Selenium instead of requests to avoid poisoning the IP.
        # Keep the browser session alive — downloadEpisode will reuse it.
        _t0 = time.time()
        _log.debug("updateInfo: creating driver for %s", self.url)
        driver = _create_driver(self.animeloads.browser, self.animeloads.browserloc)
        _log.debug("updateInfo: driver created in %.1fs", time.time() - _t0)
        driver.set_page_load_timeout(SELENIUM_PAGE_LOAD_TIMEOUT)
        # Go directly to the anime page — single page load only.
        # DDoS-Guard challenge is handled inline by the browser.
        _t1 = time.time()
        _log.debug("updateInfo: loading page %s ...", self.url)
        try:
            driver.get(self.url)
            _log.debug("updateInfo: page loaded in %.1fs", time.time() - _t1)
        except Exception as _e:
            _log.debug("updateInfo: page load timed out after %.1fs: %s", time.time() - _t1, _e)
        _log.debug("updateInfo: sleeping %ds for DDoS-Guard...", BROWSER_PAGE_WAIT)
        time.sleep(BROWSER_PAGE_WAIT)  # DDoS-Guard + ads fully load

        _t2 = time.time()
        _log.debug("updateInfo: getting page_source...")
        class _FakeResponse:
            def __init__(self, text):
                self.text = text
        detailPage = _FakeResponse(driver.page_source)
        _log.debug("updateInfo: page_source retrieved in %.1fs (%d chars)", time.time() - _t2, len(detailPage.text))
        # Store the driver for reuse by downloadEpisode
        self._driver = driver

        try:
            _t3 = time.time()
            p = HTMLTableParser()
            p.feed(detailPage.text)
            _log.debug("updateInfo: HTML table parsing took %.1fs (%d tables)", time.time() - _t3, len(p.tables))

            detailtable = p.tables[0]
            for detail in detailtable:
                leftEntry = detail[0]
                rightEntry = detail[1]
                if(leftEntry in ("Titel", "Title")):
                    if(self.japName == ""):
                        self.japName = rightEntry
                    elif(self.gerName == ""):
                        self.gerName = rightEntry
                    else:
                        self.engName = rightEntry
                elif(leftEntry in ("Synonyme", "Synonyms")):
                    self.synonymes = rightEntry.split(", ")
                elif(leftEntry in ("Typ", "Type")):
                    if(rightEntry in ("Anime Series", "Anime Serie")):
                        self.type = "series"
                    elif(rightEntry == "Web"):
                        self.type = "web"
                    elif(rightEntry == "Special"):
                        self.type = "special"
                    elif(rightEntry == "OVA"):
                        self.type = "ova"
                    elif(rightEntry in ("Anime Film", "Anime Movie")):
                        self.type = "movie"
                    elif(rightEntry == "Bonus"):
                        self.type = "bonus"
                elif(leftEntry in ("Episoden", "Episodes")):
                    epString = rightEntry.split("/")
                    try:
                        self.curEpisodes = int(epString[0])
                    except:
                        self.curEpisodes = 0
                    try:
                        self.maxEpisodes = int(epString[1])
                    except:
                        self.maxEpisodes = 999999
                elif(leftEntry in ("Jahr", "Year")):
                    self.year = int(rightEntry)
                elif(leftEntry in ("Laufzeit", "Runtime")):
                    runtimestring = re.sub("[^0-9]", "", rightEntry)
                    if(runtimestring == ""):
                        runtimestring = "0"
                    self.runtime = int(runtimestring)
                elif(leftEntry == "Status"):
                    self.status = rightEntry
                elif(leftEntry in ("Hauptgenre", "Main Genre")):
                    self.mainGenre = rightEntry
                elif(leftEntry in ("Nebengenre", "Sub Genre")):
                    self.sideGenres = rightEntry.split("  ")
                elif(leftEntry == "Tags"):
                    self.tags = rightEntry.split("  ")


            _t4 = time.time()
            rel_dom = etree.HTML(detailPage.text)
            _log.debug("updateInfo: etree.HTML() parse took %.1fs", time.time() - _t4)

            self.coverurl = rel_dom.xpath("//*[@id='description']/div[1]/div[1]/img/@src")[0]

            releases_unparsed = rel_dom.xpath("//div[@id='downloads']/div[@class='row' and 1]/div[@class='col-sm-3' and 1]/ul[@class='nav nav-pills nav-stacked' and 1]")[0]    #Get left list of releases
            releases_dom = etree.HTML(html.tostring(releases_unparsed))
            releases = releases_dom.xpath("//li")   #Get single releases from list

            numReleases = len(releases) - 1         #Last release is "Add release", ignoring
            _log.debug("updateInfo: found %d releases, starting release loop", numReleases)

            #rid, group, res, dubs, subs, format, size, password, anmerkung=""
            for relIndex in range(0, numReleases):
                relID = relIndex + 1
                group = ""      #done
                res = ""
                dubs = []       #done
                subs = []       #done
                videoformat = ""
                size = 0
                password = ""
                anmerkung = ""
                episodes = 0
                table = p.tables[relID]    #First release, get name and anmerkung

                for idx in range(0, len(table)):
                    try:
                        tLeft = table[idx][0]
                        tRight = table[idx][1]
                    except:
                        continue

                    if(tLeft in ("Release Gruppe", "Release Group")):
                        group = tRight
                    elif(tLeft in ("Auflösung", "Resolution")):
                        res = tRight.split("x")[1] + "p"
                    elif(tLeft == "Typ"):
                        videoformat = tRight
                    elif(tLeft in ("Größe", "Filesize")):
                        # Extract first number only (site format: "Ø 1.37 GB (Paket: ~13.73 GB)")
                        size_match = re.search(r"[\d.]+", tRight)
                        size_str = size_match.group() if size_match else "0"
                        if("GB" in tRight.split("(")[0] if "(" in tRight else tRight):
                            try:
                                size = float(size_str) * 1000
                            except:
                                size = 0.0
                        else:
                            try:
                                size = float(size_str)
                            except:
                                size = 0.0
                    elif(tLeft in ("Passwort", "Password")):
                        password = tRight
                    elif(tLeft in ("Release Anmerkungen", "Release Notes")):
                        anmerkung = tRight


                detailhtmlstring = str(html.tostring(releases[relIndex]))        #Detail html from release for languages#

                ###################
                #WARNING: UGLY CODE
                ###################

                dublangsubstring = detailhtmlstring.split("title=\"Sprache: ")      #HTMLCode split for Languages
                for idx in range(1, len(dublangsubstring)):                         #Start with second result, first is always garbage
                    dublang = dublangsubstring[idx].split("\"")[0]                  #Cut string after ", language end there
                    dubs.append(dublang)                                            #Add to dublist



                sublangsubstring = detailhtmlstring.split("title=\"Untertitel: ")   #HTMLcode split for Subtitles
                for idx in range(1, len(sublangsubstring)):                         #Start with second result, first is always garbage
                    sublang = sublangsubstring[idx].split("\"")[0]                  #Cut string after ", subtitle end there
                    subs.append(sublang)                                            #Add to sublist

                ##############
                #UGLY CODE END
                ##############
                _t_ep = time.time()
                all_eps = rel_dom.xpath("//a[starts-with(@aria-controls, 'downloads_episodes_" + str(relID) + "_')]")
                epnum = len(all_eps)
                _log.debug("updateInfo: release %d/%d: %s %s — %d eps (xpath %.3fs)",
                           relID, numReleases, group, res, epnum, time.time() - _t_ep)

                tmprel = release(self.url, relID, group, res, dubs, subs, epnum, videoformat, size, password, anmerkung, detailPage.text, self.session)
                self.releases.append(tmprel)
            _log.debug("updateInfo: total %.1fs", time.time() - _t_total)
            return True

        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]

            if("IndexError" in str(exc_type)):
                _log.error("Fehler beim parsen. Dies liegt sehr wahrscheinlich daran, dass anime-loads gerade offline ist oder einen Fehler hat.\nWenn der Fehler länger besteht, bitte auf Github melden.")
            else:
                _log.error("Fehler beim parsen, bitte auf Github melden")
                _log.error("%s %s %s", exc_type, fname, exc_tb.tb_lineno)
            try:
                if hasattr(self, '_driver') and self._driver:
                    self._driver.quit()
                    self._driver = None
            except Exception:
                pass
            return False


    #quality: prefer 1080p/DTS
    def getBestReleaseByQuality(self, releaseList=[]):
        if(len(releaseList) == 0):
            releaseList = self.releases

        if(len(releaseList) == 1): #Only 1 release available, has to be the best
            return releaseList[0]

        rel720p = []
        rel1080p = []
        relrest = []
        for rel in releaseList:
            if(rel.getResolution() == "1080p"):
                rel1080p.append(rel)
            elif(rel.getResolution() == "720p"):
                rel720p.append(rel)
            else:
                relrest.append(rel)
        if(len(rel1080p) == 0):         #Get best video quality
            if(len(rel720p) == 0):
                bestList = relrest
            else:
                bestList = rel720p
        else:
            bestList = rel1080p

        #PCM>FLAC=DTS>AC3>AAC>MP3           #Thx to Toastkiste21 & Tanukichan
        pointlist = []
        for rel in bestList:            #Get best audio quality
            points = 0
            audio = rel.getAudioCodec()
            audiotypes = ["MP3", "AAC", "AC3", "DTS", "FLAC&DTS", "PCM"]        #list, from worse to best
            for idx, typ in enumerate(audiotypes):
                audios = typ.split("&")
                for splitaudio in audios:
                    if(splitaudio == audio):
                        points += idx       #Audio found, add index to points(best has highest index)
                    else:
                        points += 1         #Other, unknown audio, only one point
            pointlist.append(points)


        bestrel = bestList[0]
        bestscore = 0
        for idx, rel in enumerate(bestList):
            score = pointlist[idx]
            codec = rel.getVideoCodec().lower()
            if(codec == "x265" or codec == "hevc"):
                score += 2
            #print("Release " + str(rel.getID()) + " with " + str(score) + " points")
            if(score > bestscore):
                bestscore = score
                bestrel = rel

        return rel


    #language: prefer GerDub/GerSub
    def getBestReleaseByLanguage(self, releaseList=[]):
        if(len(releaseList) == 0):
            releaseList = self.releases

        if(len(releaseList) == 1): #Only 1 release available, has to be the best
            return releaseList[0]

        dubreleases = []
        for rel in releaseList:
            for dub in rel.getDubs():
                if(dub == "Deutsch"):
                    dubreleases.append(rel)
        if(len(dubreleases) == 0):
            for rel in releaseList:
                for dub in rel.getDubs():
                    if(dub == "Englisch"):
                        dubreleases.append(rel)
        if(len(dubreleases) == 0):
            dubreleases = releaseList #No (Eng/Ger)dub releases, just going with subs
        maxsubs = 0
        relmaxsubs = []
        for rel in dubreleases:
            if(len(rel.getSubs()) >= maxsubs):
                maxsubs = len(rel.getSubs())
                relmaxsubs.append(rel)

        return self.getBestReleaseByQuality(relmaxsubs)


    #episodes: most episodes
    def getBestReleaseByEpisodes(self, releaseList=[]):
        if(len(releaseList) == 0):
            releaseList = self.releases

        if(len(releaseList) == 1): #Only 1 release available, has to be the best
            return releaseList[0]

        max = 0
        maxrel = []
        for rel in releaseList:
            if(rel.getEpisodeCount() >= max):
                max = rel.getEpisodeCount()
                maxrel.append(rel)
        return self.getBestReleaseByQuality(maxrel)


    def getName(self):
        if(self.gerName != ""):
            return self.gerName
        elif(self.engName != ""):
            return self.engName
        else:
            return self.japName

    def getNameGerman(self):
        return self.gerName

    def getNameJapanese(self):
        return self.japName

    def getNameEnglish(self):
        return self.japName

    def getURL(self):
        return self.url

    def getRuntime(self):
        return self.runtime

    def getType(self):
        return self.type

    def getSynonymes(self):
        return self.synonymes

    def getYear(self):
        return self.year

    def getCurrentEpisodes(self):
        return self.curEpisodes

    def getMaxEpisodes(self):
        return self.maxEpisodes

    def getStatus(self):
        return self.status

    def getMainGenre(self):
        return self.mainGenre

    def getSideGenres(self):
        return self.sideGenres

    def getTags(self):
        return self.tags

    def getReleases(self):
        return self.releases

    def getCoverURL(self):
        return self.coverurl

    #Returns either raw PNG data or base64 encoded string
    def getCover(self, selfbase64=False):
        imgrequest = self.session.get(self.coverurl, stream=True)
        imgdata = imgrequest.raw.data
        if(selfbase64):
            encdata = base64.b64encode(imgdata)
            return encdata
        else:
            return imgdata

    def tostring(self):
        return str("[Jap/Ger/EngName]: " + self.japName + "/" + self.gerName + "/" + self.engName + ", [Synonyms]: " + str(self.synonymes) + ", [Type]: " + \
            self.type + ", [Episodes]: " + str(self.curEpisodes) + "/" + str(self.maxEpisodes) + ", [Status]: " + self.status + ", [Year]: " + str(self.year) \
                + ", [Maingenre]: " + self.mainGenre + ", [Sidegenres]: " + str(self.sideGenres) + ", [Tags]: " + str(self.tags))


    def downloadEpisode(self, episode, release, hoster, browser, browserlocation="", jdhost="", myjd_user="", myjd_pw="", myjd_device="",jd_deprecated=False,jd_deprecatedport="3128", pkgName="", destinationFolder=""):
        if(browser == "Firefox"):
            browser = animeloads.FIREFOX
        elif(browser == "Chrome"):
            browser = animeloads.CHROME
        try:
            if("ddownload" in hoster.lower()):
                hoster = animeloads.DDOWNLOAD
            elif("rapidgator" in hoster.lower()):
                hoster = animeloads.RAPIDGATOR
        except:
            pass


        anime_identifier = self.url.split("/")[len(self.url.split("/"))-1] #Name of Anime, used for POST Request
        unenc_request = "[\"media\",\"" + anime_identifier + "\",\"downloads\"," + str(release.getID()) + "," + str(int(episode)-1) + "]"
        #print(unenc_request)
        b64 = base64.b64encode(unenc_request.encode("ascii"))
        data = {"enc": b64,
       "response": "nocaptcha"}

        ######################################
        #Requests code ohne benötigten Browser, wird allerdings als adblock erkannt
        ######################################


#        headers = {
#        'User-Agent': MODERN_UA,
#        "Referer": self.url,
#        "Accept": "application/json, text/javascript, */*; q=0.01",
#        "Accept-Language": "en-US,en;q=0.5",
#        "Connection": "keep-alive",
#        "Content-Length": "83",
#        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
#        "TE": "Trailers",
#        "X-Requested-With": "XMLHttpRequest",
#        "Cookie": "ci_session=a%3A4%3A%7Bs%3A10%3A%22session_id%22%3Bs%3A32%3A%227fda60af7999159425b7143f63cd5202%22%3Bs%3A10%3A%22ip_address%22%3Bs%3A13%3A%2289.244.161.10%22%3Bs%3A10%3A%22user_agent%22%3Bs%3A94%3A%22Mozilla%2F5.0+%28Windows+NT+10.0%3B+Win64%3B+x64%3B+rv%3A78.0%29+Gecko%2F20100101+Firefox%2F78.0+Waterfox%2F78.7.0%22%3Bs%3A13%3A%22last_activity%22%3Bi%3A1612911522%3B%7D3a772b4afefd4887a6eba3fffd868265c5769de6"
#        }

#        print("Session Cookies: " + str(cock))
#        r = requests.post("https://webhook.site/75132056-b86a-4285-ab83-2cf785a202a1", data=data, headers=headers)
#        rad = self.session.get("https://www.anime-loads.org/assets/pub/js/ads.com/ads.js?cb=13881964231")
#        print(rad.text)
#        print(rad.status_code)
#        print(rad.cookies)
#        r = self.session.post("https://www.anime-loads.org/ajax/captcha", data=data)
#        print(r.text)
#        r.raw.decode_content = True
#        print("Response: " + str(r.text))
#        cock = [
#            {'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path}
#            for c in self.session.cookies
#        ]
#        print("d_Cock: " + str(cock))
#        print("\n\n\n\ndl_Other cookies: " + str(r.cookies))


        # Reuse the browser session from updateInfo if available.
        # This is the same session that loaded the anime page with ads,
        # so the server recognises it as "not adblocked".
        driver = getattr(self, '_driver', None)
        if driver is None:
            driver = _create_driver(browser, browserlocation)
            driver.set_page_load_timeout(SELENIUM_PAGE_LOAD_TIMEOUT)
            try:
                driver.get(self.url)
            except Exception:
                pass
            time.sleep(BROWSER_DOWNLOAD_PAGE_WAIT)


        #JS to make post request to get URLs
        js = "var xhr = new XMLHttpRequest(); \
xhr.open('POST', 'https://www.anime-loads.org/ajax/captcha', false); \
xhr.setRequestHeader('Content-type', 'application/x-www-form-urlencoded'); \
xhr.send('enc=" + b64.decode('ascii') + "&response=nocaptcha'); \
return xhr.response"

        ajaxresponse = driver.execute_script(js)

        code = ""
        message = ""
        reflinks = ""
        content_ddl = ""
        content_rapid = ""

        response_json = json.loads(ajaxresponse)

        _log.debug("Response JSON: %s", response_json)

        for key in response_json:
            value = response_json[key]
            if(key == "code"):
                code = value
            elif(key == "message"):
                message = value
            elif(key == "reflink"):
                reflinks = value
            elif(key == "content"):
                # content can be a list or a dict keyed by index strings
                items = value.values() if isinstance(value, dict) else value
                for item in items:
                    try:
                        if item['hoster'] == "ddownload":
                            content_ddl = item
                        elif item['hoster'] == "rapidgator":
                            content_rapid = item
                    except:
                        pass

        tries = 0
        while(code != "success" and "noadblock" in message and tries < 5):
            _log.debug("Need to solve a captcha")
            cID = ""
            response_json = utils.doCaptcha(cID, driver, self.session, b64)
            if response_json == False:
                return ALUnknownException("Ein unbekannter Fehler beim lesen der Hosterlinks aufgetreten, möglicherweise serverseitig.")
            for key in response_json:
                value = response_json[key]
                if(key == "code"):
                    code = value
                elif(key == "message"):
                    message = value
                elif(key == "reflink"):
                    reflinks = value
                elif(key == "content"):
                    # content can be a list [{hoster:...}, ...] or a dict {"0": {hoster:...}, ...}
                    items = value.values() if isinstance(value, dict) else value
                    for item in items:
                        try:
                            if item['hoster'] == "ddownload":
                                content_ddl = item
                            elif item['hoster'] == "rapidgator":
                                content_rapid = item
                        except:
                            pass
            tries += 1

#            raise ALCaptchaException("Captcha is needed to get download links")

        if(code != "success"):
            try: driver.quit()
            except: pass
            self._driver = None
            if "slowdown" in message:
                return ALCaptchaException("Downloadlimit — zu viele Anfragen, bitte warten")
            elif "wrong_captcha" in message:
                return ALCaptchaException("Zu viele falsche Captchas, bitte warten")
            elif "Captcha verify failed" in message:
                return ALCaptchaException("Captcha konnte nicht gelöst werden")
            elif "cnl_login" in message:
                return ALInvalidLoginException("Login erforderlich")
            else:
                return ALUnknownException("Fehler: " + message if message else "Unbekannter Serverfehler")

        # Select hoster content
        if(hoster == animeloads.DDOWNLOAD):
            cnldata = content_ddl
        else:
            cnldata = content_rapid

        # Debug log the content structure
        _log.debug("content_ddl type=%s, content_rapid type=%s", type(content_ddl).__name__, type(content_rapid).__name__)
        if isinstance(content_ddl, dict):
            _log.debug("content_ddl keys=%s, has cnl=%s", list(content_ddl.keys()), 'cnl' in content_ddl)
        if isinstance(content_rapid, dict):
            _log.debug("content_rapid keys=%s, has cnl=%s", list(content_rapid.keys()), 'cnl' in content_rapid)

        # If captcha succeeded but CNL data is missing, retry nocaptcha request
        # (the captcha is already solved so the session should be authorized)
        def _has_valid_cnl(data):
            return (isinstance(data, dict) and 'cnl' in data
                    and isinstance(data['cnl'], dict) and 'jk' in data['cnl']
                    and len(data['cnl']['jk']) >= 17)

        for retry_i in range(2):
            if _has_valid_cnl(cnldata):
                break
            delay = random.uniform(CNL_RETRY_DELAY_MIN, CNL_RETRY_DELAY_MAX)
            _log.warning("CNL data missing or incomplete (retry %d/2), retrying nocaptcha after %.1fs...", retry_i+1, delay)
            time.sleep(delay)
            js_retry = ("var xhr = new XMLHttpRequest(); "
                        "xhr.open('POST', 'https://www.anime-loads.org/ajax/captcha', false); "
                        "xhr.setRequestHeader('Content-type', 'application/x-www-form-urlencoded'); "
                        "xhr.send('enc=" + b64.decode('ascii') + "&response=nocaptcha'); "
                        "return xhr.response")
            retry_resp = driver.execute_script(js_retry)
            try:
                retry_json = json.loads(retry_resp) if retry_resp else {}
            except json.JSONDecodeError:
                retry_json = {}
            _log.debug("Retry response code: %s", retry_json.get('code', 'none'))
            if retry_json.get("code") == "success" and "content" in retry_json:
                retry_content = retry_json["content"]
                items = retry_content.values() if isinstance(retry_content, dict) else retry_content
                for item in items:
                    try:
                        if item['hoster'] == "ddownload":
                            content_ddl = item
                        elif item['hoster'] == "rapidgator":
                            content_rapid = item
                    except:
                        pass
                if(hoster == animeloads.DDOWNLOAD):
                    cnldata = content_ddl
                else:
                    cnldata = content_rapid

        driver.quit()
        self._driver = None

        reflinks = ""
        k = ""
        jk = ""
        crypted = ""

        for key in cnldata:
            value = cnldata[key]
            if(key == "links"):
                reflinks = value
            elif(key == "cnl"):
                try:
                    k = value['jk']
                except:
                    raise ALLinkExtractionException("AES-Key konnte nicht gelesen werden")
                try:
                    crypted = value['crypted']
                except:
                    raise ALLinkExtractionException("Linkdaten konnten nicht gelesen werden")

        _log.debug("jk key: len=%d, value=%s", len(k), repr(k[:40]))
        try:
            k_list = list(k)
            tmp = k_list[15]
            k_list[15] = k_list[16]
            k_list[16] = tmp
            k = "".join(k_list)
        except:
            _log.error("Failed to deobfuscate key (len=%d, value=%s)", len(k), repr(k))
            raise ALLinkExtractionException("Linkdaten konnten nicht gelesen werden")

#        print("Key wurde erfolgreich deobfuskiert")

        jk = "function f(){ return \'" + k + "\';}"

        if(pkgName == ""):
            pkgName = anime_identifier
        if(jdhost != "" and myjd_user == "" and not jd_deprecated):
            return utils.addToJD(jdhost, release.getPassword(), self.url, crypted, jk)
        elif(jdhost == "" and myjd_user != ""):
            try:
                links_decoded = utils.decodeCNL(k, crypted)
                self._last_release_pattern = _release_name_from_links(links_decoded)
                _log.info("Successfully decoded links")
            except:
                _log.warning("Failed to decode links")
                raise ALUnknownException("Failed to decode links")
            links = []
            linkstring = ""
            for link in links_decoded:
                linkstring += link.decode('utf-8')
            try:
                 myjd_return = utils.addToMYJD(myjd_user, myjd_pw, myjd_device, linkstring, pkgName, release.getPassword())
            except:
                _log.error("Failed to add Link to MyJD")
                raise ALUnknownException("Failed to add Link to MyJD")
            try:
                pkgID = myjd_return['id']
                return True
            except Exception as e:
                _log.error("Error: %s", e)
                return False
        elif (jd_deprecated and jd_deprecatedport):
            try:
                links_decoded = utils.decodeCNL(k, crypted)
                self._last_release_pattern = _release_name_from_links(links_decoded)
                _log.info("Successfully decoded links")
            except:
                _log.warning("Failed to decode links")
                raise ALUnknownException("Failed to decode links")
            links = []
            linkstring = ""
            for link in links_decoded:
                linkstring += link.decode('utf-8')
            try:
                return utils.addToJDdeprecated(jdhost, jd_deprecatedport, release.getPassword(), linkstring, pkgName, destinationFolder)
            except Exception as e:
                _log.error("Error: %s", e)
                return False
        else:
            links_decoded = utils.decodeCNL(k, crypted)
            self._last_release_pattern = _release_name_from_links(links_decoded)
            links = []
            for link in links_decoded:
                links.append(link.decode('ascii'))
            return links


    def downloadBatchCNL(self, release, hoster, browser, browserlocation="",
                         jdhost="", myjd_user="", myjd_pw="", myjd_device="",
                         jd_deprecated=False, jd_deprecatedport="3128",
                         pkgName="", destinationFolder="",
                         wanted_episodes=None):
        """Download multiple episodes via batch Click'n'Load (single captcha).

        Returns dict: {"success": bool, "episodes_sent": [int], "episodes_not_found": [int]}
        Raises ALInvalidLoginException if not logged in.
        """
        if(browser == "Firefox"):
            browser = animeloads.FIREFOX
        elif(browser == "Chrome"):
            browser = animeloads.CHROME
        try:
            if("ddownload" in hoster.lower()):
                hoster = animeloads.DDOWNLOAD
            elif("rapidgator" in hoster.lower()):
                hoster = animeloads.RAPIDGATOR
        except:
            pass

        anime_identifier = self.url.split("/")[-1]
        # Build batch CNL enc — "cnl" string instead of episode index
        unenc_request = '["media","' + anime_identifier + '","downloads",' + str(release.getID()) + ',"cnl"]'
        b64 = base64.b64encode(unenc_request.encode("ascii"))

        _log.info("[BATCH] Requesting batch CNL for %s (release %d, %d wanted episodes)",
                  anime_identifier, release.getID(),
                  len(wanted_episodes) if wanted_episodes else 0)

        # Reuse driver from updateInfo, or create new one
        driver = getattr(self, '_driver', None)
        if driver is None:
            driver = _create_driver(browser, browserlocation)
            driver.set_page_load_timeout(SELENIUM_PAGE_LOAD_TIMEOUT)
            try:
                driver.get(self.url)
            except Exception:
                pass
            time.sleep(BROWSER_DOWNLOAD_PAGE_WAIT)

        # Transfer login cookies from requests session to Selenium
        utils.transfer_login_cookies(self.session, driver)
        # Refresh page so cookies take effect for subsequent XHR
        try:
            driver.get(self.url)
        except Exception:
            pass
        time.sleep(BROWSER_PAGE_WAIT)

        # Send nocaptcha request via Selenium XHR
        js = ("var xhr = new XMLHttpRequest(); "
              "xhr.open('POST', 'https://www.anime-loads.org/ajax/captcha', false); "
              "xhr.setRequestHeader('Content-type', 'application/x-www-form-urlencoded'); "
              "xhr.send('enc=" + b64.decode('ascii') + "&response=nocaptcha'); "
              "return xhr.response")

        ajaxresponse = driver.execute_script(js)
        response_json = json.loads(ajaxresponse)

        _log.debug("[BATCH] Initial response: code=%s message=%s",
                   response_json.get('code'), response_json.get('message'))

        code = response_json.get("code", "")
        message = response_json.get("message", "")

        # Check for login requirement
        if message == "cnl_login":
            driver.quit()
            self._driver = None
            raise ALInvalidLoginException("Batch CNL requires login — server returned cnl_login")

        # Parse initial content
        content_ddl = ""
        content_rapid = ""
        if "content" in response_json:
            items = response_json["content"]
            items = items.values() if isinstance(items, dict) else items
            for item in items:
                try:
                    if item['hoster'] == "ddownload":
                        content_ddl = item
                    elif item['hoster'] == "rapidgator":
                        content_rapid = item
                except:
                    pass

        # Handle captcha if needed
        tries = 0
        while code != "success" and "noadblock" in message and tries < 5:
            _log.debug("[BATCH] Solving captcha (attempt %d)", tries + 1)
            response_json = utils.doCaptcha("", driver, self.session, b64)
            if response_json is False:
                driver.quit()
                self._driver = None
                _log.warning("[BATCH] Captcha-Anfrage fehlgeschlagen")
                return {"success": False, "reason": "Captcha-Anfrage fehlgeschlagen", "episodes_sent": [], "episodes_not_found": list(wanted_episodes or [])}
            code = response_json.get("code", "")
            message = response_json.get("message", "")
            if "content" in response_json:
                items = response_json["content"]
                items = items.values() if isinstance(items, dict) else items
                for item in items:
                    try:
                        if item['hoster'] == "ddownload":
                            content_ddl = item
                        elif item['hoster'] == "rapidgator":
                            content_rapid = item
                    except:
                        pass
            tries += 1

        if code != "success":
            driver.quit()
            self._driver = None
            if "slowdown" in message:
                reason = "Downloadlimit — zu viele Anfragen, bitte warten"
            elif "wrong_captcha" in message:
                reason = "Zu viele falsche Captchas, bitte warten"
            elif "Captcha verify failed" in message:
                reason = "Captcha konnte nicht gelöst werden"
            else:
                reason = message if message else "Unbekannter Fehler"
            _log.warning("[BATCH] Failed after %d captcha attempts: %s", tries, reason)
            return {"success": False, "reason": reason, "episodes_sent": [], "episodes_not_found": list(wanted_episodes or [])}

        # Select hoster content
        if hoster == animeloads.DDOWNLOAD:
            cnldata = content_ddl
        else:
            cnldata = content_rapid

        if not isinstance(cnldata, dict) or 'cnl' not in cnldata:
            driver.quit()
            self._driver = None
            hoster_label = "ddownload" if hoster == animeloads.DDOWNLOAD else "rapidgator"
            reason = "Keine CNL-Daten für Hoster %s" % hoster_label
            _log.warning("[BATCH] %s", reason)
            return {"success": False, "reason": reason, "episodes_sent": [], "episodes_not_found": list(wanted_episodes or [])}

        # Extract and deobfuscate key
        k = cnldata['cnl'].get('jk', '')
        crypted = cnldata['cnl'].get('crypted', '')

        _log.info("[BATCH] jk len=%d, crypted len=%d", len(k), len(crypted))
        try:
            k_list = list(k)
            tmp = k_list[15]
            k_list[15] = k_list[16]
            k_list[16] = tmp
            k = "".join(k_list)
        except:
            _log.error("[BATCH] Failed to deobfuscate key (len=%d)", len(k))
            driver.quit()
            self._driver = None
            return {"success": False, "reason": "jk-Schlüssel konnte nicht entschlüsselt werden", "episodes_sent": [], "episodes_not_found": list(wanted_episodes or [])}

        # Decrypt all links
        try:
            links_decoded = utils.decodeCNL(k, crypted)
            self._last_release_pattern = _release_name_from_links(links_decoded)
            _log.info("[BATCH] Decrypted %d links", len(links_decoded))
        except Exception as e:
            _log.error("[BATCH] Failed to decrypt CNL: %s", e)
            driver.quit()
            self._driver = None
            return {"success": False, "reason": "CNL-Entschlüsselung fehlgeschlagen: %s" % e, "episodes_sent": [], "episodes_not_found": list(wanted_episodes or [])}

        driver.quit()
        self._driver = None

        # Group URLs by episode number
        total_episodes = release.getEpisodeCount()
        grouped = _group_urls_by_episode(links_decoded, total_episodes)
        _log.info("[BATCH] Grouped into %d episodes: %s", len(grouped), sorted(grouped.keys()))

        # Actual max episode with downloadable links (may differ from DOM-reported count)
        available_max = max(grouped.keys()) if grouped else None

        # Filter to wanted episodes
        episodes_sent = []
        episodes_not_found = []
        filtered_links = []

        if wanted_episodes:
            for ep in sorted(wanted_episodes):
                if ep in grouped:
                    filtered_links.extend(grouped[ep])
                    episodes_sent.append(ep)
                else:
                    episodes_not_found.append(ep)
        else:
            # No filter — send everything
            for ep in sorted(grouped.keys()):
                filtered_links.extend(grouped[ep])
                episodes_sent.append(ep)

        if not filtered_links:
            _log.warning("[BATCH] No links after filtering")
            return {"success": False, "reason": "Keine gewünschten Episoden im Batch gefunden", "episodes_sent": [], "episodes_not_found": episodes_not_found, "available_max": available_max}

        _log.info("[BATCH] Sending %d links for episodes %s to JDownloader",
                  len(filtered_links), episodes_sent)

        # Build link string for JDownloader
        linkstring = "\r\n".join(filtered_links)
        if pkgName == "":
            pkgName = anime_identifier
        password = release.getPassword()

        # Send to JDownloader via plain links API
        if myjd_user != "":
            try:
                utils.addToMYJD(myjd_user, myjd_pw, myjd_device, linkstring, pkgName, password)
            except Exception as e:
                _log.error("[BATCH] MyJD failed: %s", e)
                return {"success": False, "reason": "MyJDownloader-Fehler: %s" % e, "episodes_sent": [], "episodes_not_found": list(wanted_episodes or []), "available_max": available_max}
        elif jd_deprecated and jd_deprecatedport:
            try:
                ret = utils.addToJDdeprecated(jdhost, jd_deprecatedport, password, linkstring, pkgName, destinationFolder)
                if not ret:
                    return {"success": False, "reason": "JDownloader (deprecated) antwortete negativ", "episodes_sent": [], "episodes_not_found": list(wanted_episodes or []), "available_max": available_max}
            except Exception as e:
                _log.error("[BATCH] JD deprecated failed: %s", e)
                return {"success": False, "reason": "JDownloader (deprecated) Fehler: %s" % e, "episodes_sent": [], "episodes_not_found": list(wanted_episodes or []), "available_max": available_max}
        elif jdhost:
            # Use Flashgot /flash/add on port 9666 (same port as per-episode addToJD)
            try:
                ret = utils.addFlashLinksToJD(jdhost, linkstring, pkgName, destinationFolder, password)
                if not ret:
                    return {"success": False, "reason": "JDownloader antwortete negativ", "episodes_sent": [], "episodes_not_found": list(wanted_episodes or []), "available_max": available_max}
            except Exception as e:
                _log.error("[BATCH] Flash links failed: %s", e)
                return {"success": False, "reason": "JDownloader-Fehler: %s" % e, "episodes_sent": [], "episodes_not_found": list(wanted_episodes or []), "available_max": available_max}
        else:
            _log.error("[BATCH] No JDownloader target configured")
            return {"success": False, "reason": "Kein JDownloader-Ziel konfiguriert", "episodes_sent": [], "episodes_not_found": list(wanted_episodes or []), "available_max": available_max}

        _log.info("[BATCH] Erfolg: %d Episoden gesendet, %d fehlten",
                  len(episodes_sent), len(episodes_not_found))
        return {"success": True, "episodes_sent": episodes_sent, "episodes_not_found": episodes_not_found, "available_max": available_max, "release_pattern": getattr(self, '_last_release_pattern', '')}


class release:

    @staticmethod
    def parseAudioCodec(anmerkung):
        if(anmerkung == ""):
            return ""
        formats = ["PCM", "FLAC", "DTS", "AC3", "AAC", "MP3"]
        format = "UNKNOWN"
        for f in formats:
            if(f in anmerkung):
                format = f
        return format

    @staticmethod
    def parseVideoCodec(anmerkung):
        if(anmerkung == ""):
            return ""
        codecs = ["x264", "x265"]
        codec = "UNKNOWN"
        for c in codecs:
            if(c in anmerkung):
                codec = c
        return codec

    def __init__(self, url, rid, group, res, dubs, subs, episodeCount, videoformat, size, password, anmerkung="", sitesource="", session=""):   #TODO sortieren
        self.url = url
        self.rid = rid
        self.group = group
        self.res = res
        self.audiocodec = self.parseAudioCodec(anmerkung)
        self.dubs = dubs
        self.subs = subs
        self.videoformat = videoformat
        self.size = size
        self.episodeCount = episodeCount
        self.password = password
        self.videocodec = self.parseVideoCodec(anmerkung)
        self.anmerkung = anmerkung
        self.sitesource = sitesource
        self.session = session

    def getID(self):
        return self.rid

    def getGroup(self):
        return self.group

    def getResolution(self):
        return self.res

    def getAudioCodec(self):
        return self.audiocodec

    def getDubs(self):
        return self.dubs

    def getSubs(self):
        return self.subs

    def getVideoFormat(self):
        return self.videoformat

    def getSize(self):
        return self.size

    def getPassword(self):
        return self.password

    def getAnmerkung(self):
        return self.anmerkung

    def getVideoCodec(self):
        return self.videocodec

    def tostring(self):
        return "Release ID: " + str(self.rid) + ", Group: " + self.group + ", Resolution: " + self.res + ", Videocodec: " + self.videocodec + ", Audiocodec: " + self.audiocodec + ", Dubs: " + str(self.dubs) + \
            ", Subs: " + str(self.subs) + ", Format: " + self.videoformat + ", Size: " + str(self.size) + "MB, Password: " + self.password

    def getEpisodeCount(self):
        return self.episodeCount


#Exceptions
class ALCaptchaException(Exception):
    pass

class ALInvalidBrowserException(Exception):
    pass

class ALLinkExtractionException(Exception):
    pass

class ALInvalidLoginException(Exception):
    pass

class ALUnknownException(Exception):
    pass
