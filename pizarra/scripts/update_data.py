#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pizarra de Mercado AR - Farías Finanzas
Actualizador de datos (solo biblioteca estándar de Python 3.11+).

Qué hace (ver README-INSTALACION.md):
  1. Baja cotizaciones de data912.com (BYMA) y arma data/market.json
     (bonos, letras, ONs, CEDEARs, acciones, indicadores, TIR/MD/TNA propias).
  2. Baja titulares por RSS (solo título, enlace, medio y fecha) -> data/news.json.
  3. Registra el estado de cada fuente en data/status.json.

Reglas de robustez:
  * Cada fuente va en try/except, con timeout y reintentos. Si una falla se
    CONSERVA el dato anterior y se marca stale=true.
  * Los JSON se escriben de forma atómica (archivo temporal + os.replace).
  * Nunca se publica un dato intradiario como cierre, ni un feed viejo como nuevo.
  * exit 0 salvo error fatal (data912 caído en todo el universo, sin datos previos
    utilizables o error inesperado del propio script).

Uso:
  python scripts/update_data.py                  # corrida normal
  python scripts/update_data.py --offline-test   # usa scripts/fixtures, escribe en un directorio temporal
  python scripts/update_data.py --now 2026-11-25T19:58:00-03:00 --data-dir /tmp/x
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent.parent          # carpeta pizarra/
SCRIPTS = Path(__file__).resolve().parent
TZ = ZoneInfo("America/Argentina/Buenos_Aires")

LOG = []


def log(msg):
    print(msg)
    LOG.append(msg)


# --------------------------------------------------------------------------
# utilidades generales
# --------------------------------------------------------------------------
def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def dump_pretty(obj):
    """JSON legible y diff-friendly: una línea por elemento de cada lista de primer nivel."""
    if not isinstance(obj, dict):
        return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
    parts = []
    for k, v in obj.items():
        key = json.dumps(k, ensure_ascii=False)
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            inner = ",\n".join("  " + json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in v)
            parts.append(" %s: [\n%s\n ]" % (key, inner))
        else:
            parts.append(" %s: %s" % (key, json.dumps(v, ensure_ascii=False, separators=(",", ":"))))
    return "{\n" + ",\n".join(parts) + "\n}\n"


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()


def iso(dtobj):
    return dtobj.astimezone(TZ).isoformat(timespec="seconds")


def num(x, default=None):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# HTTP con reintentos (o fixtures en modo --offline-test)
# --------------------------------------------------------------------------
class Fetcher:
    def __init__(self, cfg, offline, fixtures_dir):
        self.cfg = cfg["http"]
        self.offline = offline
        self.fixtures = fixtures_dir

    def _fixture_for(self, url):
        mapping = [
            ("arg_bonds", "arg_bonds.json"), ("arg_notes", "arg_notes.json"),
            ("arg_stocks", "arg_stocks.json"), ("arg_cedears", "arg_cedears.json"),
            ("arg_corp", "arg_corp.json"), ("dolarapi", "dolarapi.json"),
            ("riesgo-pais", "riesgo_pais.json"), ("/cer", "cer.json"),
            ("infobae", "rss_infobae.xml"), ("ambito.com/rss/pages/finanzas", "rss_atom.xml"),
            ("cronista", "rss_malo.xml"),
        ]
        for needle, name in mapping:
            if needle in url:
                return self.fixtures / name
        return None

    def get(self, url):
        if self.offline:
            fx = self._fixture_for(url)
            if fx is None or not fx.exists():
                raise RuntimeError("sin fixture para " + url)
            return fx.read_bytes()
        last = None
        tries = 1 + int(self.cfg.get("reintentos", 2))
        for i in range(tries):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": self.cfg.get("user_agent", "FariasFinanzas-Pizarra/1.0"),
                    "Accept": "application/json, application/xml, text/xml, */*",
                })
                with urllib.request.urlopen(req, timeout=self.cfg.get("timeout_s", 20)) as r:
                    data = r.read(int(self.cfg.get("max_bytes", 12000000)) + 1)
                    if len(data) > int(self.cfg.get("max_bytes", 12000000)):
                        raise RuntimeError("respuesta demasiado grande")
                    return data
            except Exception as e:  # noqa: BLE001 - queremos capturar todo y reintentar
                last = e
                if i < tries - 1:
                    time.sleep(float(self.cfg.get("espera_entre_reintentos_s", 2)))
        raise RuntimeError("%s: %s" % (type(last).__name__, last))

    def get_json(self, url):
        return json.loads(self.get(url).decode("utf-8"))


# --------------------------------------------------------------------------
# calendario de ruedas
# --------------------------------------------------------------------------
class Calendario:
    def __init__(self, cfg):
        self.feriados = {}
        for year, lst in cfg["feriados"].items():
            if not year.isdigit():
                continue
            for it in lst:
                self.feriados[it["fecha"]] = it
        self.fechas = set(self.feriados)

    def es_habil(self, d):
        return d.weekday() < 5 and d.isoformat() not in self.fechas

    def habil_anterior(self, d):
        d = d - dt.timedelta(days=1)
        while not self.es_habil(d):
            d -= dt.timedelta(days=1)
        return d

    def habil_siguiente(self, d):
        d = d + dt.timedelta(days=1)
        while not self.es_habil(d):
            d += dt.timedelta(days=1)
        return d

    def a_confirmar(self):
        return sorted(f for f, it in self.feriados.items() if it.get("estado") != "confirmado")


def modo_mercado(now, cal, cfg):
    """Devuelve (modo, rueda_objetivo). modo: 'cierre' | 'fuera_de_hora' | 'intradia'."""
    hh = cfg["horario_rueda"]
    ap = dt.time.fromisoformat(hh["apertura"])
    ci = dt.time.fromisoformat(hh["cierre"])
    hoy = now.date()
    if cal.es_habil(hoy):
        if now.time() >= ci:
            return "cierre", hoy
        if now.time() < ap:
            return "fuera_de_hora", cal.habil_anterior(hoy)
        return "intradia", None
    return "fuera_de_hora", cal.habil_anterior(hoy)


# --------------------------------------------------------------------------
# finanzas: XIRR, duración, TNA
# --------------------------------------------------------------------------
def _dias(d1, d0):
    return (d1 - d0).days


def valor_presente(flows, r, settle):
    return sum(cf / (1.0 + r) ** (_dias(f, settle) / 365.0) for f, cf in flows)


def xirr(flows, price, settle):
    """TIR efectiva anual, Actual/365, sobre flujos futuros (fecha, monto) y precio sucio."""
    if price is None or price <= 0 or not flows:
        return None
    if sum(cf for _, cf in flows) <= 0:
        return None
    lo, hi = -0.9999, 100.0
    flo = valor_presente(flows, lo, settle) - price
    fhi = valor_presente(flows, hi, settle) - price
    if flo * fhi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2.0
        fm = valor_presente(flows, mid, settle) - price
        if flo * fm <= 0:
            hi = mid
        else:
            lo, flo = mid, fm
        if hi - lo < 1e-12:
            break
    return (lo + hi) / 2.0


def duracion_modificada(flows, r, settle):
    pv = [(_dias(f, settle) / 365.0, cf / (1.0 + r) ** (_dias(f, settle) / 365.0)) for f, cf in flows]
    tot = sum(p for _, p in pv)
    if tot <= 0:
        return None
    mac = sum(t * p for t, p in pv) / tot
    return mac / (1.0 + r)


# --------------------------------------------------------------------------
# cotizaciones data912
# --------------------------------------------------------------------------
def indexar(rows):
    out = {}
    for r in rows or []:
        s = r.get("symbol")
        if isinstance(s, str) and s:
            out[s] = r
    return out


def opero(q):
    return bool(q) and (num(q.get("v"), 0) > 0 or num(q.get("q_op"), 0) > 0)


def huella(quotes, tickers):
    h = hashlib.sha1()
    n = 0
    for t in tickers:
        q = quotes.get(t)
        if q:
            n += 1
            h.update(("%s|%s|%s;" % (t, q.get("c"), q.get("pct_change"))).encode())
    return (h.hexdigest(), n)


def filtrar_cedears(rows, cfg):
    """Convenciones del tablero:
       - láminas D/C se excluyen cuando existe symbol[:-1];
       - láminas en dólares: símbolo terminado en C/D con cierre < 100 o < 1/5 del de su par en pesos;
       - solo especies con operaciones; se excluyen datos inconsistentes."""
    u = cfg["umbrales"]
    idx = indexar(rows)
    out = []
    excl = {"dup_dc": 0, "lamina_usd": 0, "sin_ops": 0, "inconsistente": 0}
    manual = set(u.get("cedears_excluir", []))
    for r in rows:
        s = r.get("symbol", "")
        c = num(r.get("c"), 0)
        base = s[:-1].rstrip(".") if s and s[-1] in "CD" else None
        if base and (base in idx or s[:-1] in idx):
            excl["dup_dc"] += 1
            continue
        if base is not None and (c < u["cedear_precio_minimo_lamina_usd"]):
            excl["lamina_usd"] += 1
            continue
        if not opero(r) or num(r.get("v"), 0) <= 0 or c <= 0:
            excl["sin_ops"] += 1
            continue
        pct = num(r.get("pct_change"), 0)
        if s in manual or abs(pct) > u["pct_maximo_creible"]:
            excl["inconsistente"] += 1
            continue
        out.append(r)
    return out, excl


def filtrar_acciones(rows, cfg):
    """Excluye láminas D/C (si existe una especie que empieza con symbol[:-1], p. ej. TECOD -> TECO2),
    símbolos con punto (BMA.D) y las especies de la lista manual."""
    manual = set(cfg["umbrales"].get("acciones_excluir", []))
    idx = indexar(rows)
    out = []
    for r in rows:
        s = r.get("symbol", "")
        if "." in s or s in manual:
            continue
        if s and s[-1] in "CD" and any(k != s and (k == s[:-1] or (len(k) == len(s) and k.startswith(s[:-1]) and k[-1].isdigit())) for k in idx):
            continue
        out.append(r)
    return out


# --------------------------------------------------------------------------
# CER
# --------------------------------------------------------------------------
def parse_cer(series, fecha_snapshot, fecha_ref):
    """series: lista de {fecha, valor}. Devuelve dict con factor = CER(ref)/CER(snapshot)."""
    pts = {}
    if isinstance(series, dict):  # API BCRA v4.0: {results:[{detalle:[{fecha, valor}]}]}
        det = []
        for r in series.get("results") or []:
            det.extend(r.get("detalle") or [])
        series = det
    for it in series:
        try:
            pts[it["fecha"][:10]] = float(it["valor"])
        except (KeyError, TypeError, ValueError):
            continue
    if not pts:
        raise ValueError("serie CER vacía")
    ks = sorted(pts)

    def en(fecha):
        cand = [k for k in ks if k <= fecha]
        if not cand:
            raise ValueError("CER sin dato para " + fecha)
        return cand[-1], pts[cand[-1]]
    f0, v0 = en(fecha_snapshot)
    f1, v1 = en(fecha_ref)
    if v0 <= 0:
        raise ValueError("CER inválido")
    return {"fecha_snapshot": f0, "valor_snapshot": v0, "fecha_hoy": f1, "valor_hoy": v1,
            "factor": round(v1 / v0, 6), "fuente": "BCRA (API Estadísticas Monetarias v4.0, variable 30)"}


# --------------------------------------------------------------------------
# armado de filas de instrumentos
# --------------------------------------------------------------------------
def fila_instrumento(ins, qs, ctx, previo):
    u = ctx["cfg"]["umbrales"]
    fam = ins["family"]
    flags = [f for f in (ins.get("flags") or [])]
    qp = qs.get(ins["quote"].get("pesos") or "")
    qd = qs.get(ins["quote"].get("usd_mep") or "") if ins["quote"].get("usd_mep") else None
    row = {"ticker": ins["ticker"], "name": ins["name"], "family": fam, "issuer": ins.get("issuer"),
           "currency_pay": ins["currency_pay"], "maturity": ins["maturity"]}
    escala = 1000.0 if fam == "letra_dlk" else 1.0

    if not qp and not qd:
        if previo:
            p = dict(previo)
            p["stale"] = True
            p.setdefault("stale_desde", ctx["rueda"])
            return p
        row.update({"price": None, "pct": None, "volume_ars": 0, "price_usd": None, "tir": None, "md": None,
                    "flags": sorted(set(flags + ["sin_cotizacion"]))})
        return row

    price = None
    pct = 0.0
    vol = 0.0
    if qp and num(qp.get("c"), 0) > 0:
        price = num(qp["c"]) / escala
        pct = num(qp.get("pct_change"), 0.0) if opero(qp) else 0.0
        vol = num(qp.get("v"), 0.0) / 100.0 * num(qp["c"])
        if not opero(qp):
            flags.append("sin_operaciones")
        elif vol < u["poco_operada_monto_ars"] and fam not in ("letra_lecap", "letra_lecer", "letra_dlk"):
            flags.append("poco_operada")
    elif qp is None:
        flags.append("sin_cotizacion_pesos")
    price_usd = None
    pct_usd = None
    if qd and num(qd.get("c"), 0) > 0 and ins["currency_pay"] == "USD":
        price_usd = num(qd["c"])
        pct_usd = num(qd.get("pct_change"), 0.0) if opero(qd) else 0.0
        if not opero(qd):
            flags.append("sin_operaciones_usd")
    row.update({"price": round(price, 4) if price is not None else None,
                "pct": round(pct, 2) if price is not None else None,
                "volume_ars": round(vol), "price_usd": price_usd, "pct_usd": pct_usd,
                "q_op": int(num(qp.get("q_op"), 0)) if qp else 0})
    if fam == "on" and pct_usd is not None and price is None:
        row["pct"] = pct_usd

    # ---- precio sucio para la TIR ----
    settle = ctx["liquidacion"]
    mep = ctx.get("mep")
    basis = None
    p_calc = None
    if ins["currency_pay"] == "USD":
        if price_usd is not None and "sin_operaciones_usd" not in flags:
            p_calc, basis = price_usd, "lamina_D"
        elif price is not None and mep:
            p_calc, basis = price / mep, "pesos_sobre_MEP"
            flags.append("usd_implicito")
        elif price_usd is not None:
            p_calc, basis = price_usd, "lamina_D"
    else:
        if price is not None:
            p_calc, basis = price, "pesos"

    # ---- flujos ----
    raw = ins.get("flows_per_100_vn") or []
    calculable = bool(raw) and "tasa_variable" not in flags and (ins.get("fixed_flows") or fam == "letra_dlk")
    tir = md = tna = tea = None
    dias = None
    vto = dt.date.fromisoformat(ins["maturity"])
    if vto > settle:
        dias = (vto - settle).days
    if calculable and p_calc:
        factor = 1.0
        if ins.get("adjust") == "CER":
            if ctx.get("cer"):
                factor = ctx["cer"]["factor"]
            else:
                flags.append("cer_snapshot")
        if fam == "on" and 0 < (ins.get("residual_pct") or 100) < 100:
            # las ONs que amortizan cotizan por cada 100 VN residuales; el cronograma viene por 100 VN originales
            factor *= 100.0 / ins["residual_pct"]
        if fam == "letra_dlk":
            # flujos por 100 VN en "escala tablero" (pesos/1000); se re-escalan por A3500 de hoy si hay dato
            ref = (raw[-1]["interest"] + raw[-1]["amortization"]) * 10.0
            a3500 = ctx.get("a3500")
            factor = (a3500 / ref) if (a3500 and ref) else 1.0
            flags.append("estimado")
        flows = []
        for f in raw:
            d = dt.date.fromisoformat(f["date"])
            if d > settle:
                flows.append((d, (f["interest"] + f["amortization"]) * factor))
        if flows:
            r = xirr(flows, p_calc, settle)
            if r is not None and abs(r * 100) <= u["tir_abs_maxima"]:
                tir = r * 100
                m = duracion_modificada(flows, r, settle)
                md = m
                if len(flows) == 1 and dias:
                    vf = flows[0][1]
                    tna = (vf / p_calc - 1.0) * 365.0 / dias * 100.0
                    tea = tir
            elif r is not None:
                flags.append("tir_no_confiable")
        if dias is not None and dias < u["dias_corto_plazo"] and tir is not None:
            flags.append("corto_plazo")
    elif not calculable:
        if "tasa_variable" in flags:
            pass
        elif not raw:
            flags.append("sin_cronograma")
    row.update({"tir": round(tir, 2) if tir is not None else None,
                "md": round(md, 2) if md is not None else None,
                "tna": round(tna, 2) if tna is not None else None,
                "tea": round(tea, 2) if tea is not None else None,
                "dias": dias, "base_precio": basis,
                "flags": sorted(set(flags))})
    if row["price"] is None and row["price_usd"] is None:
        row["flags"] = sorted(set(row["flags"] + ["sin_cotizacion"]))
    return row


# --------------------------------------------------------------------------
# noticias
# --------------------------------------------------------------------------
def _txt(el):
    return (el.text or "").strip() if el is not None else ""


def parse_feed(raw):
    """Devuelve lista de dicts {title, link, published(datetime utc)}.
    Acepta RSS 2.0 y Atom. Si el XML está roto usa un extractor tolerante."""
    text = raw.decode("utf-8", errors="replace")
    low = text[:4000].lower()
    if "<!doctype" in low or "<!entity" in text.lower():
        raise ValueError("DOCTYPE/ENTITY no permitido")
    items = []
    try:
        root = ET.fromstring(text.encode("utf-8"))
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            if tag in ("item", "entry"):
                title = link = date = ""
                for ch in el:
                    t = ch.tag.split("}")[-1]
                    if t == "title":
                        title = _txt(ch)
                    elif t == "link":
                        link = ch.attrib.get("href") or _txt(ch)
                    elif t in ("pubDate", "published", "updated", "date") and not date:
                        date = _txt(ch)
                items.append((title, link, date))
    except ET.ParseError:
        for m in re.finditer(r"<item\b.*?>(.*?)</item>", text, re.S | re.I):
            blk = m.group(1)

            def g(tag):
                mm = re.search(r"<%s\b[^>]*>(.*?)</%s>" % (tag, tag), blk, re.S | re.I)
                if not mm:
                    return ""
                v = mm.group(1).strip()
                cd = re.match(r"<!\[CDATA\[(.*?)\]\]>$", v, re.S)
                return cd.group(1).strip() if cd else v
            items.append((g("title"), g("link"), g("pubDate")))
    out = []
    for title, link, date in items:
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        title = re.sub(r"\s+", " ", title)
        link = link.strip()
        if not title or not re.match(r"^https?://", link, re.I):
            continue
        d = None
        try:
            d = parsedate_to_datetime(date)
        except (TypeError, ValueError):
            try:
                d = dt.datetime.fromisoformat(date.replace("Z", "+00:00"))
            except ValueError:
                d = None
        if d is None:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        out.append({"title": title[:300], "link": link, "published": d.astimezone(dt.timezone.utc)})
    return out


def _tokens(s):
    return set(w for w in re.findall(r"[a-z0-9]+", norm(s)) if len(w) > 3)


def _match(texto_norm, kws):
    for k in kws:
        k = norm(k).strip()
        if k and re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", texto_norm):
            return True
    return False


def clasificar(title, feed_region, ncfg):
    t = " " + norm(title) + " "
    region = feed_region or "ar"
    if _match(t, ncfg["region_us"]):
        region = "us"
    elif _match(t, ncfg["region_global"]) and not _match(t, ["argentina", "argentino", "bcra", "merval", "caputo", "milei"]):
        region = "global"
    tema = "general"
    for key, kws in ncfg["temas"]:
        if _match(t, kws):
            tema = key
            break
    return region, tema


def actualizar_noticias(fetcher, cfg, now, previo):
    ncfg = cfg["noticias"]
    desde = now.astimezone(dt.timezone.utc) - dt.timedelta(hours=ncfg["horas_maximas"])
    ahora_utc = now.astimezone(dt.timezone.utc)
    estados = []
    cand = []
    for fd in ncfg["feeds"]:
        try:
            items = parse_feed(fetcher.get(fd["url"]))
            n_ok = 0
            for it in items:
                if it["published"] < desde or it["published"] > ahora_utc + dt.timedelta(hours=2):
                    continue
                t = norm(it["title"])
                if _match(t, ncfg["excluir"]):
                    continue
                if not _match(t, ncfg["palabras_clave"]):
                    continue
                reg, tema = clasificar(it["title"], fd.get("region_default"), ncfg)
                cand.append({"title": it["title"], "link": it["link"], "source": fd["nombre"],
                             "published": it["published"], "region": reg, "topic": tema})
                n_ok += 1
            estados.append({"nombre": fd["nombre"], "url": fd["url"], "ok": True, "items": len(items), "relevantes": n_ok})
        except Exception as e:  # noqa: BLE001
            estados.append({"nombre": fd["nombre"], "url": fd["url"], "ok": False, "error": str(e)[:160]})
    cand.sort(key=lambda x: x["published"], reverse=True)
    elegidos = []
    por_medio = {}
    toks = []
    for c in cand:
        tk = _tokens(c["title"])
        dup = False
        for t2 in toks:
            inter = len(tk & t2)
            uni = len(tk | t2) or 1
            if inter / uni >= ncfg["similitud_duplicados"]:
                dup = True
                break
        if dup:
            continue
        if por_medio.get(c["source"], 0) >= ncfg["maximo_por_medio"]:
            continue
        por_medio[c["source"]] = por_medio.get(c["source"], 0) + 1
        toks.append(tk)
        elegidos.append(c)
        if len(elegidos) >= ncfg["maximo_titulares"]:
            break
    algun_ok = any(e["ok"] for e in estados)
    if not algun_ok and previo and previo.get("items"):
        return {"asof": previo.get("asof"), "stale": True, "items": previo["items"], "feeds": estados}, estados
    items = [{"title": c["title"], "link": c["link"], "source": c["source"],
              "published": iso(c["published"]), "region": c["region"], "topic": c["topic"]} for c in elegidos]
    return {"asof": iso(now), "stale": False, "items": items, "feeds": estados}, estados


# --------------------------------------------------------------------------
# indicadores
# --------------------------------------------------------------------------
def parse_dolarapi(data, ahora):
    out = {}
    for it in data:
        casa = (it.get("casa") or "").lower()
        if casa in ("oficial", "mayorista", "blue"):
            fecha = it.get("fechaActualizacion") or ""
            try:
                f = dt.datetime.fromisoformat(fecha.replace("Z", "+00:00"))
                fecha = iso(f)
            except ValueError:
                fecha = iso(ahora)
            out[casa] = {"compra": num(it.get("compra")), "venta": num(it.get("venta")),
                         "fuente": "dolarapi.com", "fecha": fecha}
    return out


# --------------------------------------------------------------------------
# programa principal
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Actualiza data/*.json de la Pizarra de Mercado AR")
    ap.add_argument("--offline-test", action="store_true", help="usa scripts/fixtures y un directorio de salida temporal")
    ap.add_argument("--force", action="store_true", help="recalcula y reescribe el mercado aunque el feed no haya cambiado (no aplica durante la rueda)")
    ap.add_argument("--now", help="fecha/hora ISO con zona (para pruebas), p. ej. 2026-11-25T19:58:00-03:00")
    ap.add_argument("--data-dir", help="directorio de salida (por defecto ../data)")
    ap.add_argument("--fixtures-dir", help="directorio de fixtures (por defecto scripts/fixtures)")
    args = ap.parse_args(argv)

    cfg = load_json(SCRIPTS / "config.json")
    if not cfg:
        print("ERROR FATAL: no se pudo leer scripts/config.json")
        return 1

    now = dt.datetime.fromisoformat(args.now).astimezone(TZ) if args.now else dt.datetime.now(TZ)
    data_dir = Path(args.data_dir) if args.data_dir else (ROOT / "data")
    if args.offline_test and not args.data_dir:
        data_dir = Path(tempfile.mkdtemp(prefix="pizarra_offline_"))
        for nm in ("instruments_main.json", "instruments_ons.json", "sectors.json"):
            (data_dir / nm).write_bytes((ROOT / "data" / nm).read_bytes())
    fixtures = Path(args.fixtures_dir) if args.fixtures_dir else SCRIPTS / "fixtures"
    fetcher = Fetcher(cfg, args.offline_test, fixtures)
    cal = Calendario(cfg)

    log("Pizarra update_data v%s - %s (hora de Buenos Aires)%s" % (VERSION, now.strftime("%d/%m/%Y %H:%M"), " [OFFLINE]" if args.offline_test else ""))
    log("Salida: %s" % data_dir)

    main_ins = load_json(data_dir / "instruments_main.json")
    ons_ins = load_json(data_dir / "instruments_ons.json")
    sectors = load_json(data_dir / "sectors.json", {"cedears": {}, "acciones": {}})
    if not main_ins or not ons_ins:
        print("ERROR FATAL: faltan instruments_main.json / instruments_ons.json")
        return 1
    instrumentos = main_ins["instruments"] + ons_ins["instruments"]
    snapshot = main_ins.get("snapshot")

    market_prev = load_json(data_dir / "market.json")
    news_prev = load_json(data_dir / "news.json")
    status_prev = load_json(data_dir / "status.json", {}) or {}
    fuentes = {}      # nombre -> {estado, error?}

    modo, rueda_obj = modo_mercado(now, cal, cfg)
    log("Modo de mercado: %s%s" % (modo, " (rueda objetivo %s)" % rueda_obj if rueda_obj else ""))

    # ---------------------------------------------------------------- noticias
    news_out = None
    try:
        news_out, estados_feeds = actualizar_noticias(fetcher, cfg, now, news_prev)
        ok_n = sum(1 for e in estados_feeds if e["ok"])
        log("Noticias: %d/%d feeds OK, %d titulares" % (ok_n, len(estados_feeds), len(news_out["items"])))
        for e in estados_feeds:
            if not e["ok"]:
                log("  - feed con error: %s (%s)" % (e["url"], e.get("error")))
        fuentes["rss"] = {"estado": "ok" if ok_n else "error",
                          "detalle": "%d de %d feeds" % (ok_n, len(estados_feeds)) + "".join(
                              " | falla: %s (%s)" % (e["url"].split("/")[2], (e.get("error") or "")[:60])
                              for e in estados_feeds if not e["ok"])}
    except Exception as e:  # noqa: BLE001
        log("Noticias: error inesperado: %s" % e)
        fuentes["rss"] = {"estado": "error", "detalle": str(e)[:160]}

    # --------------------------------------------------------------- indicadores externos
    prev_ind = (market_prev or {}).get("indicadores") or {}
    ind_ext = {}
    try:
        d = parse_dolarapi(fetcher.get_json(cfg["apis"]["dolarapi"]), now)
        if not d:
            raise ValueError("respuesta sin oficial/mayorista/blue")
        ind_ext.update(d)
        fuentes["dolarapi"] = {"estado": "ok"}
        log("dolarapi: OK (%s)" % ", ".join(sorted(d)))
    except Exception as e:  # noqa: BLE001
        fuentes["dolarapi"] = {"estado": "error", "detalle": str(e)[:160]}
        log("dolarapi: no disponible (%s) - se conserva el dato previo" % str(e)[:100])
        for k in ("oficial", "mayorista", "blue"):
            if prev_ind.get(k):
                v = dict(prev_ind[k])
                v["stale"] = True
                ind_ext[k] = v
    try:
        j = fetcher.get_json(cfg["apis"]["riesgo_pais"])
        val = num(j.get("valor"))
        if val is None:
            raise ValueError("sin valor")
        ind_ext["riesgo_pais"] = {"valor": val, "fuente": "argentinadatos.com", "fecha": str(j.get("fecha"))[:10]}
        fuentes["riesgo_pais"] = {"estado": "ok"}
        log("riesgo país: OK (%s)" % val)
    except Exception as e:  # noqa: BLE001
        fuentes["riesgo_pais"] = {"estado": "error", "detalle": str(e)[:160]}
        log("riesgo país: no disponible (%s)" % str(e)[:100])
        if prev_ind.get("riesgo_pais"):
            v = dict(prev_ind["riesgo_pais"])
            v["stale"] = True
            ind_ext["riesgo_pais"] = v

    # --------------------------------------------------------------- data912
    market_out = None
    rueda_nueva = None
    nueva_huella = None
    skip_msg = None
    quotes = {}
    d912 = {}
    if modo == "intradia":
        skip_msg = "rueda en curso: no se publican precios intradiarios"
        fuentes["data912"] = {"estado": "omitido", "detalle": skip_msg}
        log("data912: omitido (%s)" % skip_msg)
    else:
        errores = 0
        for clave, ep in cfg["data912"]["endpoints"].items():
            try:
                data = fetcher.get_json(cfg["data912"]["base"] + ep)
                if not isinstance(data, list) or not data:
                    raise ValueError("respuesta vacía")
                d912[clave] = data
            except Exception as e:  # noqa: BLE001
                errores += 1
                log("data912/%s: ERROR %s" % (ep, str(e)[:120]))
        fuentes["data912"] = {"estado": "ok" if errores == 0 else ("parcial" if errores < len(cfg["data912"]["endpoints"]) else "error"),
                              "detalle": "%d de %d endpoints" % (len(cfg["data912"]["endpoints"]) - errores, len(cfg["data912"]["endpoints"]))}
        if not d912:
            log("ERROR FATAL: data912 no respondió en ningún endpoint")
            if market_prev:
                log("Se conservan los datos previos (stale).")
            _guardar_estado(data_dir, status_prev, fuentes, market_prev, rueda_nueva, now, cal, news_out, news_prev, cfg, nueva_huella, args, escribir_news=True, ind_ext=ind_ext, modo=modo)
            return 1
        for clave in ("bonos", "notas", "corp"):
            quotes.update(indexar(d912.get(clave)))

        # sanidad: debe haber operaciones en acciones
        acc_rows = d912.get("acciones") or []
        if acc_rows:
            frac = sum(1 for r in acc_rows if num(r.get("v"), 0) > 0) / len(acc_rows)
            if frac < cfg["data912"]["min_fraccion_acciones_con_volumen"]:
                skip_msg = "feed incompleto (solo %.0f%% de las acciones con volumen)" % (frac * 100)
        todas = {}
        for clave in d912:
            todas.update(indexar(d912[clave]))
        h, nh = huella(todas, cfg["data912"]["huella_tickers"])
        nueva_huella = {"hash": h, "tickers": nh}
        hp = status_prev.get("huella") or {}
        if skip_msg:
            pass
        elif hp.get("hash") == h and (market_prev or {}).get("rueda") and not args.force:
            if (market_prev or {}).get("rueda") == rueda_obj.isoformat():
                skip_msg = "sin cambios respecto de la corrida previa (misma rueda)"
            else:
                skip_msg = "el feed no cambió desde la rueda %s: no se publica como rueda %s" % (market_prev.get("rueda"), rueda_obj.isoformat())
        if skip_msg:
            log("Mercado: no se actualiza - %s" % skip_msg)
        else:
            rueda_nueva = rueda_obj

    if rueda_nueva:
        market_out = construir_mercado(cfg, fetcher, cal, now, rueda_nueva, instrumentos, snapshot, sectors, d912, quotes,
                                       market_prev, ind_ext, fuentes, args)

    # ---------------------------------------------------------------- escritura
    return _guardar_estado(data_dir, status_prev, fuentes, market_prev, rueda_nueva, now, cal, news_out, news_prev, cfg,
                           nueva_huella if rueda_nueva else None, args, market_out=market_out, escribir_news=True,
                           ind_ext=ind_ext, modo=modo, skip_msg=skip_msg)


def construir_mercado(cfg, fetcher, cal, now, rueda, instrumentos, snapshot, sectors, d912, quotes, market_prev, ind_ext, fuentes, args):
    liquidacion = cal.habil_siguiente(rueda)
    prev_rows = {}
    for sec in ("bonos", "letras"):
        for r in (market_prev or {}).get(sec, []) or []:
            prev_rows[r["ticker"]] = r
    stale = {"bonos": False, "letras": False, "cedears": False, "acciones": False}

    # --- indicadores de dólar propios
    ind = {}
    todas = {}
    for k in d912:
        todas.update(indexar(d912[k]))
    tc = cfg["tc_referencia"]

    def par(pesos, usd):
        a, b = todas.get(pesos), todas.get(usd)
        if a and b and num(a.get("c"), 0) > 0 and num(b.get("c"), 0) > 0 and opero(a) and opero(b):
            return num(a["c"]) / num(b["c"])
        return None
    mep = par(tc["mep_ticker_pesos"], tc["mep_ticker_usd"])
    ccl = par(tc["mep_ticker_pesos"], tc["ccl_ticker_usd"])
    alt = tc.get("alternativa")
    if mep is None and alt:
        mep = par(alt, alt + "D")
        ccl = ccl if ccl is not None else par(alt, alt + "C")
    prev_ind = (market_prev or {}).get("indicadores") or {}
    for nombre, val, det in (("mep", mep, "AL30 / AL30D"), ("ccl", ccl, "AL30 / AL30C")):
        if val is not None:
            ind[nombre] = {"valor": round(val, 2), "fuente": "Cálculo propio: %s (data912/BYMA)" % det, "fecha": rueda.isoformat()}
        elif prev_ind.get(nombre):
            v = dict(prev_ind[nombre])
            v["stale"] = True
            ind[nombre] = v
        else:
            ind[nombre] = None
    for k in ("oficial", "mayorista", "blue", "riesgo_pais"):
        ind[k] = ind_ext.get(k)

    # --- CER
    cer = None
    try:
        d0 = (dt.date.fromisoformat(str(snapshot)) - dt.timedelta(days=10)).isoformat()
        d1 = (liquidacion + dt.timedelta(days=3)).isoformat()
        url_cer = "%s?desde=%s&hasta=%s&limit=100" % (cfg["apis"]["cer"], d0, d1)
        cer = parse_cer(fetcher.get_json(url_cer), snapshot, liquidacion.isoformat())
        fuentes["cer"] = {"estado": "ok"}
        log("CER: factor %.6f (%s / %s)" % (cer["factor"], cer["fecha_hoy"], cer["fecha_snapshot"]))
    except Exception as e:  # noqa: BLE001
        fuentes["cer"] = {"estado": "error", "detalle": str(e)[:160]}
        log("CER: no disponible (%s) - flujos CER al snapshot (%s)" % (str(e)[:100], snapshot))
        pc = (market_prev or {}).get("cer")
        if pc:
            cer = dict(pc)
            cer["stale"] = True

    a3500 = None
    if ind.get("mayorista") and ind["mayorista"].get("venta"):
        a3500 = ind["mayorista"]["venta"]
    ctx = {"cfg": cfg, "liquidacion": liquidacion, "rueda": rueda.isoformat(), "mep": mep or (ind["mep"] or {}).get("valor"),
           "cer": cer, "a3500": a3500}

    bonos, letras = [], []
    qs_all = dict(todas)
    hay_bonos = bool(d912.get("bonos")) or bool(d912.get("notas"))
    hay_corp = bool(d912.get("corp"))
    for ins in instrumentos:
        fam = ins["family"]
        es_on = fam == "on"
        fuente_ok = hay_corp if es_on else hay_bonos
        previo = prev_rows.get(ins["ticker"])
        if not fuente_ok and previo:
            r = dict(previo)
            r["stale"] = True
            r.setdefault("stale_desde", (market_prev or {}).get("rueda"))
            row = r
            stale["letras" if fam.startswith("letra") else "bonos"] = True
        else:
            row = fila_instrumento(ins, qs_all, ctx, previo)
        (letras if fam.startswith("letra_") else bonos).append(row)

    # --- CEDEARs / acciones
    cedears, acciones = [], []
    sc, sa = sectors.get("cedears", {}), sectors.get("acciones", {})
    if d912.get("cedears"):
        filas, excl = filtrar_cedears(d912["cedears"], cfg)
        for r in filas:
            info = sc.get(r["symbol"]) or {}
            c = num(r["c"])
            cedears.append({"tk": r["symbol"], "name": info.get("name") or r["symbol"],
                            "sector": info.get("sector") or "Sin clasificar",
                            "price": c, "pct": num(r.get("pct_change"), 0.0), "monto": round(num(r["v"], 0) * c)})
        log("CEDEARs: %d con operaciones (excluidas: %s)" % (len(cedears), excl))
        cedears.sort(key=lambda x: -x["monto"])
    else:
        cedears = (market_prev or {}).get("cedears", [])
        stale["cedears"] = True
    if d912.get("acciones"):
        for r in filtrar_acciones(d912["acciones"], cfg):
            info = sa.get(r["symbol"]) or {}
            c = num(r.get("c"), 0.0)
            v = num(r.get("v"), 0.0)
            opero_ = v > 0
            acciones.append({"tk": r["symbol"], "name": info.get("name") or r["symbol"],
                             "sector": info.get("sector") or "Sin clasificar",
                             "price": c, "pct": num(r.get("pct_change"), 0.0) if opero_ else 0.0,
                             "monto": round(v * c)})
        log("Acciones: %d especies" % len(acciones))
        acciones.sort(key=lambda x: -x["monto"])
    else:
        acciones = (market_prev or {}).get("acciones", [])
        stale["acciones"] = True

    feriados = sorted(cal.fechas)
    out = {
        "version": 1,
        "asof": iso(now),
        "rueda": rueda.isoformat(),
        "rueda_prev": cal.habil_anterior(rueda).isoformat(),
        "liquidacion": liquidacion.isoformat(),
        "snapshot_cronogramas": snapshot,
        "indicadores": ind,
        "cer": cer,
        "stale": stale,
        "bonos": bonos,
        "letras": letras,
        "cedears": cedears,
        "acciones": acciones,
        "feriados": feriados,
    }
    log("Mercado: rueda %s, liquidación %s, %d bonos/ONs, %d letras" % (out["rueda"], out["liquidacion"], len(bonos), len(letras)))
    return out


def _guardar_estado(data_dir, status_prev, fuentes, market_prev, rueda_nueva, now, cal, news_out, news_prev, cfg, nueva_huella, args,
                    market_out=None, escribir_news=True, ind_ext=None, modo=None, skip_msg=None):
    cambios = []
    # market.json
    if market_out is not None:
        txt = dump_pretty(market_out)
        old = None
        try:
            old = (data_dir / "market.json").read_text(encoding="utf-8")
        except OSError:
            pass
        if old != txt:
            atomic_write(data_dir / "market.json", txt)
            cambios.append("market.json")
    elif market_prev is not None and ind_ext:
        # solo indicadores (dólar/riesgo país) pueden haber cambiado
        mp = json.loads(json.dumps(market_prev))
        ind = mp.get("indicadores") or {}
        for k in ("oficial", "mayorista", "blue", "riesgo_pais"):
            if ind_ext.get(k) is not None:
                ind[k] = ind_ext[k]
        mp["indicadores"] = ind
        txt = dump_pretty(mp)
        old = None
        try:
            old = (data_dir / "market.json").read_text(encoding="utf-8")
        except OSError:
            pass
        if old != txt:
            atomic_write(data_dir / "market.json", txt)
            cambios.append("market.json (indicadores)")
    # news.json
    if news_out is not None:
        prev_items = (news_prev or {}).get("items")
        if news_out["items"] != prev_items or news_prev is None or news_out.get("stale") != (news_prev or {}).get("stale"):
            atomic_write(data_dir / "news.json", dump_pretty(news_out))
            cambios.append("news.json")
    # status.json: solo si cambió algo sustantivo
    estados_new = {k: v.get("estado") for k, v in fuentes.items() if v.get("estado") != "omitido"}
    estados_prev = {k: (status_prev.get("fuentes") or {}).get(k, {}).get("estado") for k in estados_new}
    rueda_pub = (market_out or market_prev or {}).get("rueda")
    sustantivo = bool(cambios) or estados_prev != estados_new or status_prev.get("rueda") != rueda_pub or not status_prev
    huella = nueva_huella or status_prev.get("huella")
    if sustantivo:
        hora = iso(now)
        fu = {}
        for k, v in fuentes.items():
            e = dict(v)
            e["hora"] = hora
            fu[k] = e
        for k, v in (status_prev.get("fuentes") or {}).items():
            if k not in fu:
                fu[k] = v
        st = {"version_script": VERSION, "ultima_corrida": hora, "modo": modo, "rueda": rueda_pub,
              "mercado_actualizado": iso(now) if market_out is not None else status_prev.get("mercado_actualizado"),
              "mensaje": skip_msg or ("mercado actualizado" if market_out is not None else None),
              "fuentes": fu, "huella": huella,
              "feriados_a_confirmar": cal.a_confirmar()}
        atomic_write(data_dir / "status.json", dump_pretty(st))
        cambios.append("status.json")
    log("Archivos escritos: %s" % (", ".join(cambios) if cambios else "ninguno (sin cambios)"))
    log("Resumen de fuentes: " + "; ".join("%s=%s" % (k, v.get("estado")) for k, v in fuentes.items()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("ERROR FATAL inesperado: %s" % exc)
        sys.exit(1)
