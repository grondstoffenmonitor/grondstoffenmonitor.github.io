"""Haalt elke dag de nieuwste grondstofprijzen op en schrijft data/data.json.

Bronnen (allemaal openbaar, geen API-sleutels nodig):
- ECB: euro-dollarkoers (maand en dag)
- Eurostat: producentenprijsindexen (EU-27 en Nederland)
- FRED / IMF: wereldmarktprijzen suiker, cacao, koffie
- ZuivelNL: botternotering (wekelijks) en gemiddelde melkprijs (maandelijks)
- Drewry: World Container Index (wekelijks)

Boter, melk en containervracht hebben geen openbare reeks; nieuwe waarden
worden aan data/boter.csv, data/melk.csv en data/wci.csv toegevoegd.
"""
import csv
import datetime as dt
import html
import io
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
START = "2024-01"
UA = "Mozilla/5.0 (compatible; grondstoffenmonitor/1.0)"
errors = []


def get(url, timeout=60, pogingen=3):
    """Haalt een pagina op; probeert het bij een tijdelijke storing nog twee keer."""
    import time
    for poging in range(pogingen):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            if poging == pogingen - 1:
                raise
            time.sleep(10 * (poging + 1))


def safe(name, fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        errors.append(f"{name}: {e}")
        print(f"FOUT {name}: {e}", file=sys.stderr)
        return default


# ---------- bronnen ----------

def ecb(key, start):
    txt = get(f"https://data-api.ecb.europa.eu/service/data/EXR/{key}?format=csvdata&startPeriod={start}")
    rows = list(csv.DictReader(io.StringIO(txt)))
    return [(r["TIME_PERIOD"], float(r["OBS_VALUE"])) for r in rows if r.get("OBS_VALUE")]


def eurostat(geo, codes):
    q = "&".join([f"geo={geo}"] + [f"nace_r2={c}" for c in codes] +
                 ["s_adj=NSA", "unit=I21", "indic_bt=PRC_PRR_DOM", f"sinceTimePeriod={START}"])
    js = json.loads(get(f"https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/sts_inppd_m?{q}"))
    ids, sizes = js["id"], js["size"]
    cat = {d: js["dimension"][d]["category"]["index"] for d in ids}
    inv = {d: {v: k for k, v in cat[d].items()} for d in ids}
    out = {}
    for flat, val in js["value"].items():
        i = int(flat)
        pos = {}
        for d, s in reversed(list(zip(ids, sizes))):
            pos[d] = inv[d][i % s]
            i //= s
        out.setdefault(pos["nace_r2"], []).append((pos["time"], float(val)))
    return {k: sorted(v) for k, v in out.items()}


def fred(sid):
    txt = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={START}-01")
    rows = list(csv.reader(io.StringIO(txt)))[1:]
    return [(r[0][:7], float(r[1])) for r in rows if len(r) > 1 and r[1] not in (".", "")]


def cbs_diesel():
    """Dagelijkse gemiddelde pompprijs diesel in Nederland (CBS, euro per liter incl. btw)."""
    url = ("https://opendata.cbs.nl/ODataApi/odata/80416ned/TypedDataSet"
           "?$filter=Perioden%20ge%20%27" + START.replace("-", "") + "01%27&$select=Perioden,Diesel_2,BenzineEuro95_1")
    js = json.loads(get(url))
    out = {"diesel": [], "benzine": []}
    for r in js.get("value", []):
        p = str(r.get("Perioden", ""))
        if len(p) != 8 or not p.isdigit():
            continue
        for sleutel, kolom in (("diesel", "Diesel_2"), ("benzine", "BenzineEuro95_1")):
            v = r.get(kolom)
            if v is not None:
                out[sleutel].append((f"{p[:4]}-{p[4:6]}-{p[6:]}", float(v)))
    return {k: sorted(v) for k, v in out.items()}


def cbs_cpi():
    """Inflatie Nederland (CBS, CPI 2025=100): jaarmutatie in % voor alle bestedingen en voor voeding."""
    import urllib.parse
    filt = ("(Bestedingscategorieen eq 'T001112  ' or Bestedingscategorieen eq 'CPI010000') "
            "and Perioden ge '" + START.replace("-", "MM") + "'")
    url = ("https://opendata.cbs.nl/ODataApi/odata/86141NED/TypedDataSet?$filter=" + urllib.parse.quote(filt) +
           "&$select=Bestedingscategorieen,Perioden,JaarmutatieCPI_5")
    js = json.loads(get(url))
    out = {"totaal": [], "voeding": []}
    for r in js.get("value", []):
        p, v = str(r.get("Perioden", "")), r.get("JaarmutatieCPI_5")
        if v is None or "MM" not in p:
            continue
        sleutel = "totaal" if str(r.get("Bestedingscategorieen", "")).strip() == "T001112" else "voeding"
        out[sleutel].append((p.replace("MM", "-"), float(v)))
    return {k: sorted(v) for k, v in out.items()}


def tradex_video():
    """Nieuwste video (geen Short) van het YouTube-kanaal van Tradex Foods (3-Minute Market Insight)."""
    import time
    import urllib.parse
    kanaal = "UCl4BaI1Unfx0UO5TivOVk0g"
    xml = ""
    for poging in range(3):
        try:
            xml = get("https://www.youtube.com/feeds/videos.xml?channel_id=" + kanaal)
            break
        except Exception:  # noqa: BLE001
            time.sleep(5)
    if not xml:
        # Feed hapert soms; lees dan de videopagina van het kanaal (nieuwste video staat bovenaan).
        pagina = get("https://www.youtube.com/channel/" + kanaal + "/videos?hl=en")
        vid = re.search(r'"videoId":"([\w-]{11})"', pagina).group(1)
        info = json.loads(get("https://www.youtube.com/oembed?format=json&url=" +
                              urllib.parse.quote("https://www.youtube.com/watch?v=" + vid)))
        return dict(id=vid, titel=info.get("title", "Tradex Foods"), datum="")
    for entry in xml.split("<entry>")[1:]:
        if "/shorts/" in entry:
            continue
        vid = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", entry).group(1)
        titel = html.unescape(re.search(r"<title>([^<]*)</title>", entry).group(1))
        datum = re.search(r"<published>([^<]+)</published>", entry).group(1)[:10]
        return dict(id=vid, titel=titel, datum=datum)
    raise ValueError("geen video gevonden in feed")


AK = "https://tax.alaska.gov/programs/programs/reports/annual/TriannualSalmon.aspx?ReportDate="


def alaska_pink():
    """Roze zalm (pink) Alaska, diepvries H&G: gemiddelde eerste groothandelsprijs ($/lb) per periode van vier maanden.
    Bron: Alaska Department of Revenue. Al opgehaalde periodes staan in data/zalm.csv; alleen nieuwe worden opgevraagd."""
    velden = ["periode", "pink_usd_lb"]
    if not (DATA / "zalm.csv").exists():
        write_csv("zalm.csv", [], velden)
    bekend = {r["periode"] for r in read_csv("zalm.csv")}
    vandaag = dt.date.today()
    nieuw = []
    fout = ""
    for jaar in range(2021, vandaag.year + 1):
        for maand, dag in ((4, 30), (8, 31), (12, 31)):
            per = f"{jaar}-{maand:02d}"
            if per in bekend or dt.date(jaar, maand, dag) >= vandaag:
                continue
            try:
                pagina = get(AK + f"{maand}/{dag}/{jaar}")
            except Exception as e:  # noqa: BLE001
                fout = fout or f"{per}: {e}"
                continue
            pagina = html.unescape(pagina)
            blok = re.search(r"<td[^>]*>\s*Frozen\s+headed\s*&\s*gutted", pagina, re.I)
            if not blok:
                k = pagina.lower().find("frozen headed")
                fout = fout or f"{per}: geen tabel ({len(pagina)} tekens: {pagina[max(0, k - 80):k + 80]!r})"
                continue
            j = pagina.lower().find("avg wholesale price per pound", blok.end())
            if j < 0:
                continue
            cellen = re.findall(r"<td[^>]*>\s*([^<]*?)\s*</td>", pagina[j:pagina.find("</tr>", j)])
            # kolommen: Chinook, Sockeye, Coho, Pink, Chum
            if len(cellen) >= 4 and cellen[3].startswith("$") and float(cellen[3].lstrip("$").replace(",", "")) > 0:
                nieuw.append({"periode": per, "pink_usd_lb": cellen[3].lstrip("$").replace(",", "")})
    rows = upsert("zalm.csv", ["periode"], velden, nieuw)
    if not rows and fout:
        raise RuntimeError(fout)
    return rows


def text_of(page):
    page = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(page))


def num(s):
    return float(s.replace(".", "").replace(",", ".")) if "," in s else float(s.replace(",", ""))


def boter_live():
    t = text_of(get("https://www.zuivelnl.org/marktinformatie/zuivelnoteringen"))
    d = re.search(r"noteringen per (\d{2})-(\d{2})-(\d{4})", t)
    blok = re.search(r"Verse boter(.{0,160}?)Vol melkpoeder", t)
    bedragen = re.findall(r"€\s*([\d.,]+)", blok.group(1)) if blok else []
    if not (d and bedragen):
        raise ValueError("botternotering niet gevonden op de pagina")
    # het blok bevat eerst de wijziging ("+ € 3") en daarna de notering ("€ 375")
    return (f"{d.group(3)}-{d.group(2)}-{d.group(1)}", num(bedragen[-1]))


MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
           "september", "oktober", "november", "december"]


def melk_live():
    t = text_of(get("https://www.zuivelnl.org/marktinformatie/melkprijzen"))
    m = re.search(r"melkprijs is in (" + "|".join(MAANDEN) + r") (\d{4})", t, re.I)
    v = re.search(r"gemiddeld €\s*([\d.,]+)\s*per 100 kg", t, re.I)
    if not (m and v):
        raise ValueError("melkprijs niet gevonden op de pagina")
    return (f"{m.group(2)}-{MAANDEN.index(m.group(1).lower()) + 1:02d}", num(v.group(1)))


MND_EN = {m: i + 1 for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def wci_live():
    t = text_of(get("https://www.drewry.co.uk/supply-chain-advisors/supply-chain-expertise/world-container-index-assessed-by-drewry"))
    d = re.search(r"assessment for \w+, (\d{1,2}) (\w{3}) (\d{4})", t)
    if not d:
        raise ValueError("datum WCI niet gevonden")
    datum = f"{d.group(3)}-{MND_EN[d.group(2)]:02d}-{int(d.group(1)):02d}"
    out = []
    for code in ("WCI-SHA-RTM", "WCI-COMPOSITE"):
        m = re.search(code + r"\s*\$([\d,]+)\s*\$([\d,]+)\s*\$([\d,]+)\s*(-?\d+)%", t)
        if m:
            out.append((datum, code, int(m.group(3).replace(",", "")), int(m.group(4))))
    if not any(o[1] == "WCI-SHA-RTM" for o in out):
        m = re.search(r"Shanghai to Rotterdam (?:\w+ )*?(?:(increased|decreased|rose|fell|dropped|declined|surged)\s+(\d+)%\s+)?(?:\w+ )*?(?:to|at) \$([\d,]+)", t)
        if not m:
            raise ValueError("tarief Shanghai-Rotterdam niet gevonden")
        pct = int(m.group(2)) if m.group(2) else 0
        if m.group(1) in ("decreased", "fell", "dropped", "declined"):
            pct = -pct
        out.append((datum, "WCI-SHA-RTM", int(m.group(3).replace(",", "")), pct))
    return out


# ---------- historie-bestanden ----------

def read_csv(name):
    with open(DATA / name, newline="") as f:
        return list(csv.DictReader(f))


def write_csv(name, rows, fields):
    with open(DATA / name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def upsert(name, key_fields, fields, new_rows):
    rows = read_csv(name)
    idx = {tuple(r[k] for k in key_fields): r for r in rows}
    for nr in new_rows:
        idx[tuple(str(nr[k]) for k in key_fields)] = {k: str(nr[k]) for k in fields}
    rows = sorted(idx.values(), key=lambda r: tuple(r[k] for k in key_fields))
    write_csv(name, rows, fields)
    return rows


# ---------- rekenwerk ----------

def pct(a, b):
    return round((a / b - 1) * 100, 1) if b else None


def yoy(series):
    """series: [(YYYY-MM, v)] -> (maand, laatste, % t.o.v. zelfde maand vorig jaar)."""
    if not series:
        return None, None, None
    m, v = series[-1]
    prev = dict(series).get(f"{int(m[:4]) - 1}{m[4:]}")
    return m, v, pct(v, prev) if prev else None


def maandgem(punten):
    per = {}
    for d, v in punten:
        per.setdefault(d[:7], []).append(v)
    return [(m, sum(v) / len(v)) for m, v in sorted(per.items())]


def main():
    vandaag = dt.date.today()
    vorige_maand = (vandaag.replace(day=1) - dt.timedelta(days=1)).replace(day=1)

    fx_m = safe("ECB maand", lambda: ecb("M.USD.EUR.SP00.A", START), [])
    fx_d = safe("ECB dag", lambda: ecb("D.USD.EUR.SP00.A", vorige_maand.isoformat()), [])
    eu = safe("Eurostat EU", lambda: eurostat("EU27_2020", ["C1052", "C1081", "C1062", "C1082", "C1721", "C2222"]), {})
    nl = safe("Eurostat NL", lambda: eurostat("NL", ["C1052", "C1721", "C2222"]), {})
    brandstof = safe("CBS brandstof", cbs_diesel, {})
    cpi = safe("CBS inflatie", cbs_cpi, {})
    video = safe("Tradex video", tradex_video, None)
    zalm = safe("Alaska zalm", alaska_pink, [])
    fx_lang = safe("ECB maand vanaf 2021", lambda: ecb("M.USD.EUR.SP00.A", "2021-01"), [])
    diesel = brandstof.get("diesel", [])
    benzine = brandstof.get("benzine", [])
    wm = {s: safe(f"FRED {s}", lambda s=s: fred(s), []) for s in ("PSUGAISAUSDM", "PCOCOUSDM", "PCOFFOTMUSDM")}

    b = safe("ZuivelNL boter", boter_live, None)
    boter = upsert("boter.csv", ["datum"], ["datum", "boter_eur_per_100kg"],
                   [{"datum": b[0], "boter_eur_per_100kg": b[1]}] if b else [])
    mk = safe("ZuivelNL melk", melk_live, None)
    melk = upsert("melk.csv", ["maand"], ["maand", "prijs_eur_100kg"],
                  [{"maand": mk[0], "prijs_eur_100kg": mk[1]}] if mk else [])
    w = safe("Drewry WCI", wci_live, [])
    wci = upsert("wci.csv", ["datum", "route"], ["datum", "route", "tarief", "week_pct"],
                 [{"datum": a, "route": r, "tarief": t, "week_pct": p} for a, r, t, p in w])

    # dollarprijzen naar euro (zelfde maand)
    koers = dict(fx_m)
    naar_kg = {"PSUGAISAUSDM": lambda v: v / 100 * 2.20462,   # dollarcent/pound -> dollar/kg
               "PCOCOUSDM": lambda v: v / 1000,                # dollar/ton -> dollar/kg
               "PCOFFOTMUSDM": lambda v: v / 100 * 2.20462}
    wm_eur = {s: [(m, naar_kg[s](v) / koers[m]) for m, v in reeks if m in koers] for s, reeks in wm.items()}

    boter_pt = [(r["datum"], float(r["boter_eur_per_100kg"]) / 100) for r in boter]
    boter_m = maandgem(boter_pt)
    melk_m = [(r["maand"], float(r["prijs_eur_100kg"])) for r in melk]
    sha = [(r["datum"], float(r["tarief"]), r["week_pct"]) for r in wci if r["route"] == "WCI-SHA-RTM"]

    tiles = []

    def tile(groep, tid, naam, eenheid, waarde, periode, verandering, label, bron, prefix="", suffix="", dec=None, hoog_goed=False):
        tiles.append(dict(groep=groep, id=tid, naam=naam, eenheid=eenheid, waarde=waarde, periode=periode,
                          verandering=verandering, label=label, bron=bron, prefix=prefix, suffix=suffix,
                          dec=dec, hoog_goed=hoog_goed))

    m, v, c = yoy(fx_m)
    tile("Valuta & vracht", "eurusd", "Euro in dollar", "ECB-koers, maandgemiddelde", v, m, c, "op jaar",
         "https://data.ecb.europa.eu/data/datasets/EXR/EXR.M.USD.EUR.SP00.A", prefix="1 euro = ", suffix=" dollar", dec=3, hoog_goed=True)
    if sha:
        d, t, p = sha[-1]
        tile("Valuta & vracht", "wci", "Container Shanghai → Rotterdam", "per 40ft container", t, d,
             float(p) if p not in ("", None) else None, "op week",
             "https://www.drewry.co.uk/supply-chain-advisors/supply-chain-expertise/world-container-index-assessed-by-drewry", prefix="$ ", dec=0)

    for sid, reeks, naam in (("diesel", diesel, "Diesel (pompprijs NL)"), ("benzine", benzine, "Benzine Euro95 (pompprijs NL)")):
        if not reeks:
            continue
        d, v = reeks[-1]
        doel = dt.date.fromisoformat(d) - dt.timedelta(days=365)
        vj = min(reeks, key=lambda r: abs((dt.date.fromisoformat(r[0]) - doel).days))
        ok = abs((dt.date.fromisoformat(vj[0]) - doel).days) <= 7
        excl = f"{v / 1.21:.2f}".replace(".", ",")
        tile("Brandstof", sid, naam, f"per liter incl. btw (excl. btw € {excl})", v, d,
             pct(v, vj[1]) if ok else None, "op jaar", "https://www.cbs.nl/nl-nl/cijfers/detail/80416ned", prefix="€ ", dec=3)

    cbs_cpi_url = "https://www.cbs.nl/nl-nl/cijfers/detail/86141NED"
    for sid, sleutel, naam, uitleg in (("cpi", "totaal", "Inflatie Nederland (CPI)", "alle bestedingen, prijsstijging t.o.v. een jaar eerder"),
                                        ("cpi_voeding", "voeding", "Inflatie voedingsmiddelen", "voeding en alcoholvrije dranken, t.o.v. een jaar eerder")):
        reeks = cpi.get(sleutel, [])
        if reeks:
            m, v = reeks[-1]
            tile("Inflatie", sid, naam, uitleg, v, m, None, "", cbs_cpi_url, suffix="%", dec=1)

    m, v, c = yoy(melk_m)
    tile("Zuivel", "melk", "Melk (melkprijs EU-gemiddelde)", "per liter standaardmelk",
         v / 100 * 1.03 if v else None, m, c, "op jaar", "https://www.zuivelnl.org/marktinformatie/melkprijzen", prefix="€ ", dec=3)
    if boter_pt:
        d, v = boter_pt[-1]
        doel = dt.date.fromisoformat(d) - dt.timedelta(days=364)
        vj = min(boter_pt, key=lambda r: abs((dt.date.fromisoformat(r[0]) - doel).days))
        ok = abs((dt.date.fromisoformat(vj[0]) - doel).days) <= 10
        tile("Zuivel", "boter", "Boter (ZuivelNL-notering)", "per kg", v, d, pct(v, vj[1]) if ok else None, "op jaar",
             "https://www.zuivelnl.org/marktinformatie/zuivelnoteringen", prefix="€ ", dec=2)
    es = "https://ec.europa.eu/eurostat/databrowser/view/sts_inppd_m/default/table"

    m, v, c = yoy(wm_eur["PSUGAISAUSDM"])
    tile("Suiker & glucose", "suiker", "Suiker wereldmarkt", "per kg", v, m, c, "op jaar", "https://fred.stlouisfed.org/series/PSUGAISAUSDM", prefix="€ ", dec=2)
    m, v, c = yoy(eu.get("C1062", []))
    tile("Suiker & glucose", "c1062", "Zetmeel & glucose (EU)", "producentenprijs, index 2021=100", v, m, c, "op jaar", es, dec=1)

    m, v, c = yoy(wm_eur["PCOCOUSDM"])
    tile("Cacao, koffie & chocolade", "cacao", "Cacao wereldmarkt", "per kg", v, m, c, "op jaar", "https://fred.stlouisfed.org/series/PCOCOUSDM", prefix="€ ", dec=2)
    m, v, c = yoy(wm_eur["PCOFFOTMUSDM"])
    tile("Cacao, koffie & chocolade", "koffie", "Koffie wereldmarkt (arabica)", "per kg", v, m, c, "op jaar", "https://fred.stlouisfed.org/series/PCOFFOTMUSDM", prefix="€ ", dec=2)
    m, v, c = yoy(eu.get("C1082", []))
    tile("Cacao, koffie & chocolade", "c1082", "Cacao, chocolade & snoep (EU)", "producentenprijs, index 2021=100", v, m, c, "op jaar", es, dec=1)

    kl = dict(fx_lang)
    zalm_kg = []
    for r in zalm:
        j, mnd = int(r["periode"][:4]), int(r["periode"][5:])
        k = [kl[f"{j}-{x:02d}"] for x in range(mnd - 3, mnd + 1) if f"{j}-{x:02d}" in kl]
        if k:  # dollar per pound -> euro per kg, met de gemiddelde koers over dezelfde vier maanden
            zalm_kg.append((r["periode"], float(r["pink_usd_lb"]) * 2.20462 / (sum(k) / len(k))))
    m, v, c = yoy(zalm_kg)
    tile("Vis", "zalm", "Roze zalm Alaska (diepvries H&G)", "per kg, gemiddelde 4 maanden t/m", v, m, c, "op jaar",
         "https://tax.alaska.gov/programs/programs/reports/index.aspx?60624", prefix="€ ", dec=2)

    m, v, c = yoy(nl.get("C1721", []))
    tile("Verpakking", "karton", "Karton & golfkarton (NL)", "producentenprijs, index 2021=100", v, m, c, "op jaar", es, dec=1)
    m, v, c = yoy(nl.get("C2222", []))
    tile("Verpakking", "plastic", "Plastic verpakking (NL)", "producentenprijs, index 2021=100", v, m, c, "op jaar", es, dec=1)

    r4 = lambda s: [[a, round(b, 4)] for a, b in s]  # noqa: E731
    klein = [
        dict(id="dollar", titel="Euro in dollar, dagkoers vorige en deze maand", dag=True, reeks=r4(fx_d)),
        dict(id="diesel", titel="Diesel pompprijs NL (€/liter, maandgemiddelde)", reeks=r4(maandgem(diesel))),
        dict(id="benzine", titel="Benzine Euro95 pompprijs NL (€/liter, maandgemiddelde)", reeks=r4(maandgem(benzine))),
        dict(id="cpi", titel="Inflatie Nederland (% t.o.v. vorig jaar)", reeks=r4(cpi.get("totaal", []))),
        dict(id="cpi_voeding", titel="Inflatie voedingsmiddelen (% t.o.v. vorig jaar)", reeks=r4(cpi.get("voeding", []))),
        dict(id="boter", titel="Boter (€/kg)", reeks=r4(boter_m)),
        dict(id="suiker", titel="Suiker (€/kg)", reeks=r4(wm_eur["PSUGAISAUSDM"])),
        dict(id="cacao", titel="Cacao (€/kg)", reeks=r4(wm_eur["PCOCOUSDM"])),
        dict(id="koffie", titel="Koffie arabica (€/kg)", reeks=r4(wm_eur["PCOFFOTMUSDM"])),
        dict(id="zalm", titel="Roze zalm Alaska, diepvries H&G (€/kg, per 4 maanden)", reeks=r4(zalm_kg)),
        dict(id="karton", titel="Karton Nederland (index, 2021=100)", reeks=r4(nl.get("C1721", []))),
        dict(id="plastic", titel="Plastic verpakking Nederland (index, 2021=100)", reeks=r4(nl.get("C2222", []))),
        dict(id="container", titel="Container Shanghai → Rotterdam ($ per 40ft)", dag=True, reeks=[[d, t] for d, t, _ in sha]),
    ]
    BOTER_2021, MELK_2021 = 425.08, 35.91  # ZuivelNL-jaargemiddelden 2021 (euro per 100 kg)
    eu_lijnen = [dict(naam=n, reeks=r4(eu.get(c, []))) for c, n in
                 [("C1052", "Consumptie-ijs"), ("C1081", "Suiker"), ("C1062", "Zetmeel & glucose"),
                  ("C1082", "Cacao & chocolade"), ("C1721", "Karton"), ("C2222", "Plastic verpakking")]]
    eu_lijnen.append(dict(naam="Boter (NL-notering)", reeks=[[a, round(b * 100 / BOTER_2021 * 100, 1)] for a, b in boter_m]))
    eu_lijnen.append(dict(naam="Melk (EU-melkprijs)", reeks=[[a, round(b / MELK_2021 * 100, 1)] for a, b in melk_m]))

    # Valt een bron een dag uit, houd dan de vorige cijfers vast in plaats van lege blokjes.
    oud_pad = DATA / "data.json"
    if oud_pad.exists():
        try:
            oud = json.loads(oud_pad.read_text())
            ot = {t["id"]: t for t in oud.get("tegels", [])}
            tiles = [ot.get(t["id"], t) if t["waarde"] is None else t for t in tiles]
            ok = {k["id"]: k for k in oud.get("klein", [])}
            klein = [ok.get(k["id"], k) if not k["reeks"] else k for k in klein]
            oe = {l["naam"]: l for l in oud.get("eu", [])}
            eu_lijnen = [oe.get(l["naam"], l) if not l["reeks"] else l for l in eu_lijnen]
            if not video:
                video = oud.get("video")
        except Exception as e:  # noqa: BLE001
            errors.append(f"vorige data.json: {e}")

    out = dict(bijgewerkt=dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
               tegels=tiles, klein=klein, eu=eu_lijnen, video=video, fouten=errors)
    (DATA / "data.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"Klaar: {len(tiles)} tegels, {len(errors)} fouten")
    for e in errors:
        print(" -", e)


if __name__ == "__main__":
    main()
