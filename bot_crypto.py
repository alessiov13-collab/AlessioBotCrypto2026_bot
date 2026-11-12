#!/usr/bin/env python3
"""
Bot crypto in SIMULAZIONE (paper trading) - dati pubblici Revolut X (o Kraken).
Riconosce le figure dell'analisi tecnica (pattern) e simula le operazioni.

Non usa soldi veri e non richiede account/chiavi API.
Richiede solo Python 3.9+ (nessuna libreria da installare).

Uso:
  python3 bot_crypto.py pattern             # elenca le figure trovate + grafico SVG
  python3 bot_crypto.py backtest            # testa la strategia sugli ultimi mesi
  python3 bot_crypto.py backtest ETH-EUR    # idem su Ethereum
  python3 bot_crypto.py confronta           # prova 30 min, 1 ora, 4 ore, 1 giorno
  python3 bot_crypto.py paper               # simulazione dal vivo (Ctrl+C per fermare)

Figure riconosciute (entrata solo alla ROTTURA della linea, come da manuale):
  Inversione:    doppio massimo/minimo, triplo massimo/minimo,
                 testa e spalle / testa e spalle inverso
  Continuazione: triangolo ascendente, discendente, simmetrico, rettangolo

Regole di trading (solo acquisto, niente vendite allo scoperto):
  - COMPRA alla rottura di una figura rialzista.
  - VENDE se tocca lo stop (sotto la figura), se raggiunge l'obiettivo
    (altezza della figura proiettata dal punto di rottura) o se si forma
    una figura ribassista.
  - Ogni operazione paga commissione Revolut X (0,09% taker) + slittamento (0,05%).
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ---------------- PARAMETRI (modificabili) ----------------
FONTE = "revolut"        # "revolut" (Revolut X) oppure "kraken"
COPPIA = "BTC-EUR"       # BTC-EUR = Bitcoin/Euro, ETH-EUR = Ethereum/Euro
INTERVALLO_MIN = 240     # minuti per candela: 30, 60, 240 (4 ore), 1440 (1 giorno)
NUM_CANDELE = 2000       # quante candele scaricare per il backtest
CAPITALE_INIZIALE = 1000.0
COMMISSIONE = 0.0009     # Revolut X: 0,09% taker (0% se l'ordine è "maker")
SLITTAMENTO = 0.0005     # differenza tra prezzo atteso e prezzo eseguito (stima)
# movimento minimo per considerare un massimo/minimo: più largo su candele lunghe
ZIGZAG_PER_INTERVALLO = {15: 0.01, 30: 0.015, 60: 0.02, 240: 0.04, 1440: 0.08}
ZIGZAG_SOGLIA = ZIGZAG_PER_INTERVALLO.get(INTERVALLO_MIN, 0.04)
TOLLERANZA = 0.02        # due massimi "uguali" se distano meno del 2%
FINESTRA = 200           # candele analizzate per cercare le figure
MAX_ETA_FIGURA = 40      # una figura vale solo se l'ultimo punto è recente
FILE_STATO = "stato_paper.json"
FILE_REGISTRO = "operazioni.csv"
FILE_GRAFICO = "grafico_pattern.svg"
# ----------------------------------------------------------


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "bot-crypto/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _scarica_revolut(coppia, intervallo, quante):
    """Candele pubbliche Revolut X (max 1000 per richiesta: scarica a blocchi)."""
    candele, until = {}, int(time.time() * 1000)
    while len(candele) < quante:
        since = until - 1000 * intervallo * 60_000
        url = (f"https://revx.revolut.com/api/1.0/public/candles/{coppia}"
               f"?interval={intervallo}&since={since}&until={until}")
        dati = _get_json(url).get("data", [])
        if not dati:
            break
        for c in dati:
            candele[int(c["start"]) // 1000] = {
                "t": int(c["start"]) // 1000, "o": float(c["open"]), "h": float(c["high"]),
                "l": float(c["low"]), "c": float(c["close"])}
        until = since
        time.sleep(1.1)  # limite Revolut: 1 richiesta al secondo
    return [candele[t] for t in sorted(candele)][-quante:]


def _scarica_kraken(coppia, intervallo):
    """Ultime ~720 candele dall'API pubblica Kraken."""
    simbolo = coppia.replace("-", "").replace("BTC", "XBT")
    dati = _get_json(f"https://api.kraken.com/0/public/OHLC?pair={simbolo}&interval={intervallo}")
    if dati.get("error"):
        raise RuntimeError(f"Errore Kraken: {dati['error']}")
    chiave = next(k for k in dati["result"] if k != "last")
    return [{"t": int(c[0]), "o": float(c[1]), "h": float(c[2]),
             "l": float(c[3]), "c": float(c[4])} for c in dati["result"][chiave]]


def scarica_candele(coppia, intervallo, quante=None):
    quante = quante or NUM_CANDELE
    if FONTE == "revolut":
        cs = _scarica_revolut(coppia, intervallo, quante)
    else:
        cs = _scarica_kraken(coppia, intervallo)
    # scarto l'ultima candela se è ancora in formazione
    if cs and cs[-1]["t"] + intervallo * 60 > time.time():
        cs = cs[:-1]
    if len(cs) < 100:
        raise RuntimeError(f"Dati insufficienti per {coppia} ({len(cs)} candele). "
                           f"Prova FONTE = \"kraken\" nei parametri.")
    return cs


# ------------------------------------------------------------------
# 1) ZIGZAG: trova i massimi (H) e minimi (L) significativi del grafico
# ------------------------------------------------------------------
def zigzag(cs, inizio, fine, soglia=None):
    """Punti di svolta confermati tra inizio e fine (inclusi): (indice, prezzo, 'H'/'L')."""
    soglia = soglia or ZIGZAG_SOGLIA
    punti, trend = [], 0
    hi = lo = inizio
    for i in range(inizio, fine + 1):
        h, l = cs[i]["h"], cs[i]["l"]
        if trend == 0:
            if h >= cs[hi]["h"]:
                hi = i
            if l <= cs[lo]["l"]:
                lo = i
            if hi < i and l <= cs[hi]["h"] * (1 - soglia):
                punti.append((hi, cs[hi]["h"], "H")); trend, lo = -1, i
            elif lo < i and h >= cs[lo]["l"] * (1 + soglia):
                punti.append((lo, cs[lo]["l"], "L")); trend, hi = 1, i
        elif trend == 1:
            if h >= cs[hi]["h"]:
                hi = i
            elif l <= cs[hi]["h"] * (1 - soglia):
                punti.append((hi, cs[hi]["h"], "H")); trend, lo = -1, i
        else:
            if l <= cs[lo]["l"]:
                lo = i
            elif h >= cs[lo]["l"] * (1 + soglia):
                punti.append((lo, cs[lo]["l"], "L")); trend, hi = 1, i
    return punti


def linea(p1, p2, x):
    """Valore in x della retta che passa per i punti p1=(i, prezzo) e p2."""
    (x1, y1), (x2, y2) = p1, p2
    return y1 + (y2 - y1) * (x - x1) / (x2 - x1) if x2 != x1 else y1


def simili(a, b, toll=TOLLERANZA):
    return abs(a - b) / max(abs(a), abs(b)) <= toll


# ------------------------------------------------------------------
# 2) FIGURE DI INVERSIONE
#    Scritte per la versione "in alto" (ribassista). La versione rialzista
#    (doppio minimo, testa e spalle inverso...) si ottiene capovolgendo il
#    grafico: prezzi col segno meno e massimi <-> minimi.
# ------------------------------------------------------------------
def _inversione_top(P, chiusura, chiusura_prec, i):
    tipi = "".join(p[2] for p in P[-5:])
    # Testa e spalle: spalla, collo, TESTA, collo, spalla
    if tipi == "HLHLH":
        (a, s1, _), (b, n1, _), (c, t, _), (d, n2, _), (e, s2, _) = P[-5:]
        if t > s1 * (1 + TOLLERANZA) and t > s2 * (1 + TOLLERANZA) and simili(s1, s2, 2 * TOLLERANZA):
            collo = linea((b, n1), (d, n2), i)
            collo_prec = linea((b, n1), (d, n2), i - 1)
            if chiusura < collo and chiusura_prec >= collo_prec:
                altezza = t - linea((b, n1), (d, n2), c)
                return ("testa e spalle", collo, s2, collo - altezza, P[-5:])
        # Triplo massimo: tre massimi simili
        media = (s1 + t + s2) / 3
        if all(simili(x, media) for x in (s1, t, s2)):
            collo = min(n1, n2)
            if chiusura < collo <= chiusura_prec:
                return ("triplo massimo", collo, max(s1, t, s2), collo - (media - collo), P[-5:])
    # Doppio massimo
    if tipi[-3:] == "HLH":
        (a, h1, _), (b, n, _), (c, h2, _) = P[-3:]
        if simili(h1, h2) and chiusura < n <= chiusura_prec:
            return ("doppio massimo", n, max(h1, h2), n - (max(h1, h2) - n), P[-3:])
    return None


NOMI_INVERSI = {"testa e spalle": "testa e spalle inverso",
                "triplo massimo": "triplo minimo",
                "doppio massimo": "doppio minimo"}


def figure_inversione(P, cs, i):
    trovate = []
    c, cp = cs[i]["c"], cs[i - 1]["c"]
    r = _inversione_top(P, c, cp, i)
    if r:
        trovate.append({"nome": r[0], "dir": "ribasso", "ingresso": r[1],
                        "stop": r[2], "obiettivo": r[3], "punti": r[4]})
    # capovolgo il grafico per cercare le figure rialziste
    Pinv = [(x, -p, "L" if t == "H" else "H") for x, p, t in P]
    r = _inversione_top(Pinv, -c, -cp, i)
    if r:
        trovate.append({"nome": NOMI_INVERSI[r[0]], "dir": "rialzo", "ingresso": -r[1],
                        "stop": -r[2], "obiettivo": -r[3],
                        "punti": [(x, -p, "L" if t == "H" else "H") for x, p, t in r[4]]})
    return trovate


# ------------------------------------------------------------------
# 3) FIGURE DI CONTINUAZIONE (triangoli e rettangolo)
# ------------------------------------------------------------------
def figure_continuazione(P, cs, i):
    if len(P) < 4:
        return []
    ultimi = P[-4:]
    H = [(x, p) for x, p, t in ultimi if t == "H"]
    L = [(x, p) for x, p, t in ultimi if t == "L"]
    if len(H) != 2 or len(L) != 2:
        return []
    (_, h1), (_, h2) = H
    (_, l1), (_, l2) = L
    alti_piatti, bassi_piatti = simili(h1, h2), simili(l1, l2)
    alti_scendono, bassi_salgono = h2 < h1 * (1 - TOLLERANZA), l2 > l1 * (1 + TOLLERANZA)

    if alti_piatti and bassi_salgono:
        nome = "triangolo ascendente"
    elif bassi_piatti and alti_scendono:
        nome = "triangolo discendente"
    elif alti_scendono and bassi_salgono:
        nome = "triangolo simmetrico"
    elif alti_piatti and bassi_piatti:
        nome = "rettangolo"
    else:
        return []

    if nome in ("triangolo ascendente", "rettangolo"):
        res = res_p = max(h1, h2)
    else:
        res, res_p = linea(H[0], H[1], i), linea(H[0], H[1], i - 1)
    if nome in ("triangolo discendente", "rettangolo"):
        sup = sup_p = min(l1, l2)
    else:
        sup, sup_p = linea(L[0], L[1], i), linea(L[0], L[1], i - 1)

    altezza = max(h1, h2) - min(l1, l2)
    c, cp = cs[i]["c"], cs[i - 1]["c"]
    if c > res and cp <= res_p:
        return [{"nome": nome, "dir": "rialzo", "ingresso": res, "stop": l2,
                 "obiettivo": res + altezza, "punti": ultimi}]
    if c < sup and cp >= sup_p:
        return [{"nome": nome, "dir": "ribasso", "ingresso": sup, "stop": h2,
                 "obiettivo": sup - altezza, "punti": ultimi}]
    return []


def cerca_figure(cs, i):
    """Figure che vengono rotte proprio sulla candela i (usa solo dati fino a i)."""
    P = zigzag(cs, max(0, i - FINESTRA), i)
    if not P or i - P[-1][0] > MAX_ETA_FIGURA:
        return []
    inv = figure_inversione(P, cs, i)
    # se c'è già una figura di inversione nella stessa direzione, ha la precedenza
    cont = [f for f in figure_continuazione(P, cs, i) if f["dir"] not in {g["dir"] for g in inv}]
    return inv + cont


# ------------------------------------------------------------------
# 4) TRADING SIMULATO
# ------------------------------------------------------------------
def decidi(i, cs, stato):
    """Restituisce (azione, motivo, figura) per la candela i."""
    prezzo = cs[i]["c"]
    figure = cerca_figure(cs, i)
    if stato["crypto"] > 0:
        if prezzo <= stato["stop"]:
            return "VENDI", "stop-loss", None
        if stato.get("obiettivo") and prezzo >= stato["obiettivo"]:
            return "VENDI", "obiettivo raggiunto", None
        for f in figure:
            if f["dir"] == "ribasso":
                return "VENDI", f["nome"], f
    else:
        for f in figure:
            if f["dir"] == "rialzo" and f["stop"] < prezzo:
                return "COMPRA", f["nome"], f
    return None, "", None


def esegui(azione, prezzo, stato, figura=None):
    if azione == "COMPRA":
        stato["crypto"] = stato["euro"] * (1 - COMMISSIONE - SLITTAMENTO) / prezzo
        stato["euro"] = 0.0
        stato["stop"] = figura["stop"]
        stato["obiettivo"] = figura["obiettivo"]
        stato["prezzo_acquisto"] = prezzo
    else:
        stato["euro"] = stato["crypto"] * prezzo * (1 - COMMISSIONE - SLITTAMENTO)
        stato["crypto"] = 0.0
        stato["stop"] = stato["obiettivo"] = 0.0


def stato_nuovo():
    return {"euro": CAPITALE_INIZIALE, "crypto": 0.0, "stop": 0.0, "obiettivo": 0.0,
            "prezzo_acquisto": 0.0, "ultima_candela": 0}


def data(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def backtest(cs, verbose=True, riepilogo=True):
    stato = stato_nuovo()
    operazioni, vinte, picco, max_dd = 0, 0, CAPITALE_INIZIALE, 0.0
    inizio = 50
    for i in range(inizio, len(cs)):
        c = cs[i]
        azione, motivo, figura = decidi(i, cs, stato)
        if azione:
            if azione == "VENDI":
                operazioni += 1
                vinte += c["c"] > stato["prezzo_acquisto"] * (1 + 2 * (COMMISSIONE + SLITTAMENTO))
            esegui(azione, c["c"], stato, figura)
            if verbose:
                print(f"{data(c['t'])}  {azione:6} a {c['c']:>10.2f} €  ({motivo})")
        valore = stato["euro"] + stato["crypto"] * c["c"]
        picco = max(picco, valore)
        max_dd = max(max_dd, (picco - valore) / picco)

    finale = stato["euro"] + stato["crypto"] * cs[-1]["c"]
    hold = CAPITALE_INIZIALE * (1 - COMMISSIONE - SLITTAMENTO) * cs[-1]["c"] / cs[inizio]["c"]
    giorni = (cs[-1]["t"] - cs[inizio]["t"]) / 86400
    if riepilogo:
        print("\n========== RISULTATO BACKTEST ==========")
        print(f"Periodo:            {data(cs[inizio]['t'])} -> {data(cs[-1]['t'])} ({giorni:.0f} giorni)")
        print(f"Capitale iniziale:  {CAPITALE_INIZIALE:.2f} €")
        print(f"Bot:                {finale:.2f} € ({(finale / CAPITALE_INIZIALE - 1) * 100:+.1f}%)")
        print(f"Compra e tieni:     {hold:.2f} € ({(hold / CAPITALE_INIZIALE - 1) * 100:+.1f}%)")
        print(f"Operazioni chiuse:  {operazioni}  (in guadagno: {vinte})")
        print(f"Calo massimo:       -{max_dd * 100:.1f}%")
        verdetto = "BATTE" if finale > hold else "NON batte"
        print(f"\n=> Il bot {verdetto} il semplice 'compra e tieni' in questo periodo.")
        if operazioni < 10:
            print("   Attenzione: con meno di 10 operazioni il risultato è poco significativo.")
    return {"finale": finale, "hold": hold, "operazioni": operazioni, "vinte": vinte,
            "max_dd": max_dd, "giorni": giorni}


def confronta(coppia, intervalli=(30, 60, 240, 1440)):
    """Backtest della stessa strategia su diversi intervalli di tempo."""
    global INTERVALLO_MIN, ZIGZAG_SOGLIA
    righe = []
    for iv in intervalli:
        INTERVALLO_MIN = iv
        ZIGZAG_SOGLIA = ZIGZAG_PER_INTERVALLO.get(iv, 0.04)
        print(f"Scarico candele da {iv} min...")
        try:
            cs = scarica_candele(coppia, iv)
        except Exception as e:
            print(f"  saltato: {e}")
            continue
        r = backtest(cs, verbose=False, riepilogo=False)
        righe.append((iv, r))
    nomi = {30: "30 min", 60: "1 ora", 240: "4 ore", 1440: "1 giorno"}
    print(f"\n================ CONFRONTO INTERVALLI ({coppia}) ================")
    print(f"{'Candele':9} {'Giorni':>6} {'Bot':>9} {'Compra&tieni':>13} {'Operaz.':>8} {'Vinte':>6} {'Calo max':>9}")
    for iv, r in righe:
        bot = (r["finale"] / CAPITALE_INIZIALE - 1) * 100
        hold = (r["hold"] / CAPITALE_INIZIALE - 1) * 100
        print(f"{nomi.get(iv, str(iv)):9} {r['giorni']:>6.0f} {bot:>+8.1f}% {hold:>+12.1f}% "
              f"{r['operazioni']:>8} {r['vinte']:>6} {-r['max_dd'] * 100:>8.1f}%")
    print("\nNota: ogni intervallo copre un periodo diverso (stesso numero di candele),")
    print("quindi confronta soprattutto 'Bot' con 'Compra&tieni' sulla stessa riga.")
    return righe


# ------------------------------------------------------------------
# 5) ELENCO FIGURE + GRAFICO SVG
# ------------------------------------------------------------------
def scansione(cs):
    trovate, viste = [], set()
    for i in range(50, len(cs)):
        for f in cerca_figure(cs, i):
            chiave = (f["nome"], f["dir"], tuple(p[0] for p in f["punti"]))
            if chiave in viste:  # stessa figura già segnalata
                continue
            viste.add(chiave)
            f["i"] = i
            trovate.append(f)
    print(f"Figure trovate su {COPPIA} ({len(cs)} candele):\n")
    for f in trovate:
        freccia = "▲" if f["dir"] == "rialzo" else "▼"
        print(f"{data(cs[f['i']]['t'])}  {freccia} {f['nome']:24} rottura a {f['ingresso']:.2f}  "
              f"stop {f['stop']:.2f}  obiettivo {f['obiettivo']:.2f}")
    if not trovate:
        print("Nessuna figura completata nel periodo.")
    disegna_svg(cs, trovate)
    print(f"\nGrafico salvato in {FILE_GRAFICO} (aprilo con il browser).")
    return trovate


def disegna_svg(cs, figure, larg=1400, alt=600, marg=60):
    pmin = min(c["l"] for c in cs); pmax = max(c["h"] for c in cs)
    X = lambda i: marg + (larg - 2 * marg) * i / (len(cs) - 1)
    Y = lambda p: alt - marg - (alt - 2 * marg) * (p - pmin) / (pmax - pmin)
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{larg}" height="{alt}" '
         f'font-family="sans-serif" font-size="12"><rect width="100%" height="100%" fill="#fff"/>',
         f'<text x="{marg}" y="30" font-size="18">{COPPIA} — figure riconosciute</text>']
    for k in range(5):
        p = pmin + (pmax - pmin) * k / 4
        s.append(f'<line x1="{marg}" x2="{larg - marg}" y1="{Y(p):.1f}" y2="{Y(p):.1f}" stroke="#eee"/>'
                 f'<text x="5" y="{Y(p) + 4:.1f}" fill="#888">{p:.0f}</text>')
    pts = " ".join(f"{X(i):.1f},{Y(c['c']):.1f}" for i, c in enumerate(cs))
    s.append(f'<polyline points="{pts}" fill="none" stroke="#555" stroke-width="1.2"/>')
    for f in figure:
        col = "#1a9850" if f["dir"] == "rialzo" else "#d73027"
        fp = " ".join(f"{X(x):.1f},{Y(p):.1f}" for x, p, _ in f["punti"]) + f" {X(f['i']):.1f},{Y(cs[f['i']]['c']):.1f}"
        s.append(f'<polyline points="{fp}" fill="none" stroke="{col}" stroke-width="2" opacity="0.8"/>')
        s.append(f'<circle cx="{X(f["i"]):.1f}" cy="{Y(cs[f["i"]]["c"]):.1f}" r="5" fill="{col}"/>')
        dy = -10 if f["dir"] == "rialzo" else 18
        s.append(f'<text x="{X(f["i"]):.1f}" y="{Y(cs[f["i"]]["c"]) + dy:.1f}" fill="{col}" '
                 f'text-anchor="middle">{f["nome"]}</text>')
    s.append("</svg>")
    with open(FILE_GRAFICO, "w") as fh:
        fh.write("\n".join(s))


# ------------------------------------------------------------------
# 6) SIMULAZIONE DAL VIVO
# ------------------------------------------------------------------
def registra(riga):
    nuovo = not os.path.exists(FILE_REGISTRO)
    with open(FILE_REGISTRO, "a") as f:
        if nuovo:
            f.write("data,azione,prezzo,motivo,valore_portafoglio\n")
        f.write(riga + "\n")


def paper():
    stato = stato_nuovo()
    if os.path.exists(FILE_STATO):
        with open(FILE_STATO) as f:
            stato.update(json.load(f))
        print("Ripreso portafoglio simulato salvato.")
    print(f"Simulazione su {COPPIA}, candele da {INTERVALLO_MIN} min. Ctrl+C per fermare.\n")
    while True:
        try:
            cs = scarica_candele(COPPIA, INTERVALLO_MIN, quante=FINESTRA + 60)
            i = len(cs) - 1
            c = cs[i]
            if c["t"] > stato["ultima_candela"]:
                azione, motivo, figura = decidi(i, cs, stato)
                if azione:
                    esegui(azione, c["c"], stato, figura)
                valore = stato["euro"] + stato["crypto"] * c["c"]
                stato["ultima_candela"] = c["t"]
                with open(FILE_STATO, "w") as f:
                    json.dump(stato, f, indent=2)
                msg = azione or "ATTENDI"
                print(f"{data(c['t'])}  prezzo {c['c']:.2f} €  -> {msg:7} "
                      f"| portafoglio {valore:.2f} € {('(' + motivo + ')') if motivo else ''}")
                if azione:
                    registra(f"{data(c['t'])},{azione},{c['c']:.2f},{motivo},{valore:.2f}")
        except Exception as e:
            print(f"Errore temporaneo: {e} - riprovo tra 5 minuti")
        time.sleep(300)


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else "confronta"  # senza argomenti: confronto intervalli
    if len(sys.argv) > 2:
        COPPIA = sys.argv[2].upper()
    if modo == "backtest":
        backtest(scarica_candele(COPPIA, INTERVALLO_MIN))
    elif modo == "confronta":
        confronta(COPPIA)
    elif modo == "pattern":
        scansione(scarica_candele(COPPIA, INTERVALLO_MIN))
    elif modo == "paper":
        paper()
    else:
        print(__doc__)
