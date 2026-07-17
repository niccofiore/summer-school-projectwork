#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibra_meteo.py  -  Autoguidovie / calibrazione effetto meteo sull'occupazione

COSA FA
  1. Legge contapax_espansi_corsa_fermata.csv (anche 1+ GB, a blocchi) per TUTTE le linee.
  2. Calcola per ogni riga un "indice di occupazione relativa" = pax a bordo / media storica
     della sua cella strutturale (linea, palina, periodicita, fascia oraria).
     Cosi' si toglie l'effetto di orario/stagione/fermata e resta il resto (incluso il meteo).
  3. Scarica da Open-Meteo lo storico meteo giornaliero (pioggia, temp max/min) per un punto
     rappresentativo di ogni linea (con cache su disco: se rilanci, non riscarica).
  4. Stima quanto pioggia e temperatura spostano la domanda, per ogni linea e in aggregato.
  5. Scrive un unico file: meteo_calibrazione.json  ->  mandalo indietro.

COME SI LANCIA
  - Metti questo file nella stessa cartella dei CSV (o passa --folder).
  - pip install pandas requests   (requests e' opzionale, in mancanza usa urllib)
  - python calibra_meteo.py
  - Alla fine trovi meteo_calibrazione.json nella stessa cartella.

Nessun dato sensibile esce: al meteo vengono inviate solo coordinate geografiche e date.
"""

import os, sys, json, time, math, argparse, urllib.request, urllib.error
from collections import defaultdict

try:
    import pandas as pd
except ImportError:
    sys.exit("ERRORE: manca pandas. Installa con:  pip install pandas")

# ---------------------------------------------------------------- parametri
AP = argparse.ArgumentParser()
AP.add_argument("--folder", default=os.path.dirname(os.path.abspath(__file__)),
                help="cartella con i CSV (default: cartella dello script)")
AP.add_argument("--chunksize", type=int, default=1_500_000, help="righe per blocco")
AP.add_argument("--min-cella", type=int, default=20,
                help="minimo osservazioni per validare una media di cella")
AP.add_argument("--griglia", type=float, default=0.1,
                help="arrotondamento coordinate per raggruppare le chiamate meteo (gradi)")
A = AP.parse_args()

F = A.folder
def path(name): return os.path.join(F, name)

CONTAPAX = path("contapax_espansi_corsa_fermata.csv")
CALEND   = path("calendario_servizio.csv")
POSIZ    = path("posizione_fermate.csv")
OUT      = path("meteo_calibrazione.json")
WCACHE   = path("meteo_cache.json")

for req in (CONTAPAX, CALEND, POSIZ):
    if not os.path.exists(req):
        sys.exit(f"ERRORE: file non trovato: {req}\nLancia lo script nella cartella dei CSV o usa --folder.")

def log(*a): print(*a, flush=True)

# ---------------------------------------------------------------- calendario
log("[1/6] Leggo il calendario di servizio ...")
cal = pd.read_csv(CALEND, dtype=str)
cal_per   = dict(zip(cal["DAT_DATA_SERVIZIO"], cal["DES_PERIODICITA"].fillna("n.d.")))
# giorni da ESCLUDERE dalla statistica (ponti, feste anomale, ecc.)
cal_excl  = set(cal.loc[cal["FLG_ESCLUSIONE_STATISTICA"] == "S", "DAT_DATA_SERVIZIO"])
log(f"     periodicita note per {len(cal_per)} date; {len(cal_excl)} date escluse dalla statistica")

# ---------------------------------------------------------------- posizioni fermate
log("[2/6] Leggo le posizioni delle fermate ...")
pos = pd.read_csv(POSIZ, sep=";", dtype=str)
pos["lat"] = pos["lat"].str.replace(",", ".", regex=False).astype(float)
pos["lon"] = pos["lon"].str.replace(",", ".", regex=False).astype(float)
palina_coord = {r.fermata: (r.lat, r.lon) for r in pos.itertuples(index=False)}

# ---------------------------------------------------------------- PASS 1: medie di cella
COLS = ["COD_LINEA", "DAT_DATA_SERVIZIO", "COD_PALINA", "NUM_PAX_A_BORDO", "DES_FASCIA_ORARIA"]
cell_sum = defaultdict(float)   # (linea,palina,per,fascia) -> somma pax
cell_cnt = defaultdict(int)     # (linea,palina,per,fascia) -> conteggio
line_palina_cnt = defaultdict(int)  # (linea,palina) -> conteggio (per punto rappresentativo)

log("[3/6] PASS 1 su contapax: calcolo le medie storiche per cella (puo' richiedere qualche minuto) ...")
nrow = 0
t0 = time.time()
for chunk in pd.read_csv(CONTAPAX, usecols=COLS, dtype=str, chunksize=A.chunksize):
    d = chunk["DAT_DATA_SERVIZIO"]
    keep = ~d.isin(cal_excl)
    chunk = chunk[keep]
    pax = pd.to_numeric(chunk["NUM_PAX_A_BORDO"], errors="coerce").fillna(0.0).to_numpy()
    per = chunk["DAT_DATA_SERVIZIO"].map(cal_per).fillna("n.d.").to_numpy()
    line = chunk["COD_LINEA"].to_numpy()
    pal  = chunk["COD_PALINA"].to_numpy()
    fas  = chunk["DES_FASCIA_ORARIA"].fillna("n.d.").to_numpy()
    for i in range(len(pax)):
        k = (line[i], pal[i], per[i], fas[i])
        cell_sum[k] += pax[i]; cell_cnt[k] += 1
        line_palina_cnt[(line[i], pal[i])] += 1
    nrow += len(chunk)
    log(f"     ... {nrow:,} righe   ({time.time()-t0:.0f}s)")

cell_mean = {k: cell_sum[k]/cell_cnt[k] for k in cell_cnt if cell_cnt[k] >= A.min_cella and cell_sum[k]/cell_cnt[k] >= 2.0}
log(f"     celle valide: {len(cell_mean):,}")

# punto rappresentativo per linea = palina piu' frequente CON coordinate
line_rep = {}
tmp = defaultdict(list)
for (ln, pal), c in line_palina_cnt.items():
    if pal in palina_coord:
        tmp[ln].append((c, pal))
for ln, lst in tmp.items():
    lst.sort(reverse=True)
    line_rep[ln] = lst[0][1]
log(f"     linee con punto rappresentativo: {len(line_rep)}")

# ---------------------------------------------------------------- PASS 2: ratio per linea-giorno
log("[4/6] PASS 2 su contapax: calcolo occupazione relativa per linea/giorno ...")
ld_sum = defaultdict(float)  # (linea,data) -> somma ratio
ld_cnt = defaultdict(int)    # (linea,data) -> conteggio
nrow = 0; t0 = time.time()
for chunk in pd.read_csv(CONTAPAX, usecols=COLS, dtype=str, chunksize=A.chunksize):
    d = chunk["DAT_DATA_SERVIZIO"]
    chunk = chunk[~d.isin(cal_excl)]
    pax = pd.to_numeric(chunk["NUM_PAX_A_BORDO"], errors="coerce").fillna(0.0).to_numpy()
    per = chunk["DAT_DATA_SERVIZIO"].map(cal_per).fillna("n.d.").to_numpy()
    line = chunk["COD_LINEA"].to_numpy()
    pal  = chunk["COD_PALINA"].to_numpy()
    fas  = chunk["DES_FASCIA_ORARIA"].fillna("n.d.").to_numpy()
    dat  = chunk["DAT_DATA_SERVIZIO"].to_numpy()
    for i in range(len(pax)):
        base = cell_mean.get((line[i], pal[i], per[i], fas[i]))
        if not base:
            continue
        r = pax[i]/base
        if r > 5.0: r = 5.0
        ld_sum[(line[i], dat[i])] += r
        ld_cnt[(line[i], dat[i])] += 1
    nrow += len(chunk)
    log(f"     ... {nrow:,} righe   ({time.time()-t0:.0f}s)")

# dataframe linea-giorno
rows = [(ln, dt, ld_sum[(ln,dt)]/ld_cnt[(ln,dt)], ld_cnt[(ln,dt)]) for (ln,dt) in ld_cnt]
ldf = pd.DataFrame(rows, columns=["linea","data","ratio","n"])
dmin, dmax = ldf["data"].min(), ldf["data"].max()
log(f"     coppie linea-giorno: {len(ldf):,}   periodo {dmin} -> {dmax}")

# ---------------------------------------------------------------- meteo (con cache)
log("[5/6] Scarico il meteo storico da Open-Meteo (con cache su disco) ...")
def grid_key(lat, lon):
    g = A.griglia
    return (round(round(lat/g)*g, 3), round(round(lon/g)*g, 3))

# cache
wcache = {}
if os.path.exists(WCACHE):
    try: wcache = json.load(open(WCACHE, encoding="utf-8"))
    except Exception: wcache = {}

def fetch_grid(lat, lon, start, end):
    ck = f"{lat},{lon},{start},{end}"
    if ck in wcache:
        return wcache[ck]
    url = ("https://archive-api.open-meteo.com/v1/archive"
           f"?latitude={lat}&longitude={lon}&start_date={start}&end_date={end}"
           "&daily=precipitation_sum,temperature_2m_max,temperature_2m_min&timezone=Europe%2FRome")
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                j = json.loads(resp.read().decode("utf-8"))
            dd = j.get("daily", {})
            out = {t: [p, tx, tn] for t, p, tx, tn in zip(
                dd.get("time", []), dd.get("precipitation_sum", []),
                dd.get("temperature_2m_max", []), dd.get("temperature_2m_min", []))}
            wcache[ck] = out
            json.dump(wcache, open(WCACHE, "w"))  # salva subito: rilanci = no refetch
            return out
        except Exception as e:
            last = e; time.sleep(1.5*(attempt+1))
    log(f"     ATTENZIONE: meteo non scaricato per {lat},{lon}: {last}")
    return {}

# griglia unica per tutte le linee rappresentate
line_grid = {}
grids = {}
for ln, pal in line_rep.items():
    lat, lon = palina_coord[pal]
    gk = grid_key(lat, lon)
    line_grid[ln] = gk
    grids[gk] = (gk[0], gk[1])
log(f"     punti meteo da scaricare (raggruppati su griglia {A.griglia}gradi): {len(grids)}")

grid_weather = {}
for i, (gk, (lat, lon)) in enumerate(grids.items(), 1):
    grid_weather[gk] = fetch_grid(lat, lon, dmin, dmax)
    log(f"     [{i}/{len(grids)}] {gk}: {len(grid_weather[gk])} giorni")

# ---------------------------------------------------------------- stima effetti
log("[6/6] Stimo gli effetti meteo ...")
def wmean(sub):
    n = sub["n"].sum()
    return (sub["ratio"]*sub["n"]).sum()/n if n > 0 else float("nan")

def stima(sub):
    # aggancia meteo
    prec, tmax, tmin = [], [], []
    for r in sub.itertuples(index=False):
        gk = line_grid.get(r.linea)
        w = grid_weather.get(gk, {}).get(r.data) if gk else None
        prec.append(w[0] if w and w[0] is not None else float("nan"))
        tmax.append(w[1] if w and w[1] is not None else float("nan"))
        tmin.append(w[2] if w and w[2] is not None else float("nan"))
    s = sub.copy(); s["prec"]=prec; s["tmax"]=tmax; s["tmin"]=tmin
    s = s.dropna(subset=["prec"])
    if len(s) == 0: return None
    dry  = s[s["prec"] < 1.0]
    wet  = s[s["prec"] >= 1.0]
    hvy  = s[s["prec"] >= 5.0]
    mild = s[(s["tmax"]>=10)&(s["tmax"]<=25)]
    hot  = s[s["tmax"]>=30]
    cold = s[s["tmin"]<=0]
    def pct(a, b):
        ma, mb = wmean(a), wmean(b)
        if not (ma==ma and mb==mb and mb>0): return None
        return round((ma/mb-1)*100, 1)
    base_dry = wmean(dry)
    out = {
        "n_giorni": int(s["data"].nunique()),
        "n_giorni_asciutti": int(dry["data"].nunique()),
        "n_giorni_piovosi": int(wet["data"].nunique()),
        "effetto_pioggia_pct":       pct(wet, dry),   # pioggia>=1mm vs asciutto
        "effetto_pioggia_forte_pct": pct(hvy, dry),   # pioggia>=5mm vs asciutto  (usalo come incremento_max)
        "effetto_caldo_pct":         pct(hot, mild),  # tmax>=30 vs 10-25
        "effetto_freddo_pct":        pct(cold, mild), # tmin<=0 vs mild
        "ratio_medio_asciutto": round(base_dry,3) if base_dry==base_dry else None,
        "buckets_pioggia": {}
    }
    for lab, lo, hi in [("0",-1,0.1),("0-2",0.1,2),("2-5",2,5),("5-10",5,10),("10+",10,1e9)]:
        b = s[(s["prec"]>=lo)&(s["prec"]<hi)]
        out["buckets_pioggia"][lab] = {"n_giorni": int(b["data"].nunique()),
                                        "ratio_medio": round(wmean(b),3) if len(b) else None}
    return out

result = {
    "generato": time.strftime("%Y-%m-%d %H:%M"),
    "periodo": [dmin, dmax],
    "metodo": ("occupazione relativa = pax a bordo / media storica per (linea,palina,periodicita,fascia); "
               "effetto = variazione % della media pesata dell'occupazione relativa nei giorni piovosi/caldi/freddi "
               "rispetto ai giorni asciutti/miti. Giorni con esclusione statistica scartati."),
    "note": ("effetto_pioggia_forte_pct e' il candidato diretto a sostituire 'incremento_max' nella pagina. "
             "Valori con pochi giorni (<10) vanno presi con cautela. Positivo = piu' domanda."),
    "globale": None,
    "per_linea": {}
}

result["globale"] = stima(ldf)
for ln, sub in ldf.groupby("linea"):
    if sub["data"].nunique() >= 30:   # solo linee con storia sufficiente
        st = stima(sub)
        if st: result["per_linea"][ln] = st

json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

# riassunto a video
g = result["globale"] or {}
log("\n==================== RISULTATO ====================")
log(f"Periodo: {dmin} -> {dmax}")
if g:
    log(f"GLOBALE  pioggia(>=1mm): {g.get('effetto_pioggia_pct')}%   "
        f"pioggia forte(>=5mm): {g.get('effetto_pioggia_forte_pct')}%   "
        f"caldo(>=30): {g.get('effetto_caldo_pct')}%   freddo(<=0): {g.get('effetto_freddo_pct')}%")
    log(f"         giorni asciutti {g.get('n_giorni_asciutti')} / piovosi {g.get('n_giorni_piovosi')}")
if "110" in result["per_linea"]:
    l = result["per_linea"]["110"]
    log(f"LINEA110 pioggia(>=1mm): {l.get('effetto_pioggia_pct')}%   pioggia forte(>=5mm): {l.get('effetto_pioggia_forte_pct')}%")
log(f"\nLinee elaborate: {len(result['per_linea'])}")
log(f"FATTO. Mandami questo file:\n  {OUT}")
log("==================================================")
